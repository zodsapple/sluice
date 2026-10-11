"""Key handling: validity (`require_key`) and duplicates (`dedupe`). Both run automatically from `keys:`."""
from __future__ import annotations

import re

import pandas as pd

from sluice.engine.context import LINEAGE, RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.ops import register_op
from sluice.engine.ops.clean import id_text


@register_op("require_key")
def require_key(df: pd.DataFrame, ctx: RunContext, col: str, pattern: str | None = None,
                on_invalid: str = "drop") -> pd.DataFrame:
    df = df.copy()
    rx = re.compile(pattern) if pattern else None

    def clean(v):
        t = id_text(v)
        if pd.isna(t) or rx is None:
            return t
        m = rx.fullmatch(str(t)) or rx.search(str(t))
        return m.group(0) if m else pd.NA

    cleaned = df[col].map(clean)
    bad = cleaned.isna()
    empty = df[col].map(id_text).isna()
    if bad.any() and on_invalid == "error":
        sample = df.loc[bad, col].head(5).tolist()
        raise EngineError("invalid_key", f"{int(bad.sum())} row(s) have an invalid {col!r}", stage="input_validation",
                          field=col, sample=sample, pattern=pattern)
    for flag, code, msg in ((empty, "missing_key", f"Row has no {col}"),
                            (bad & ~empty, "invalid_key",
                             f"{col} does not match the expected pattern {pattern!r}")):
        df = ctx.drop_rows(df, flag.reindex(df.index, fill_value=False), code, msg + "; row dropped", field=col,
                           stage="input_validation")
        cleaned = cleaned.reindex(df.index)
    if rx is not None:
        df[col] = cleaned
    return df


@register_op("dedupe")
def dedupe(df: pd.DataFrame, ctx: RunContext, key: list[str], exact: str = "keep", conflict: str = "error",
           order_by: str | None = None) -> pd.DataFrame:
    payload = [c for c in df.columns if c not in LINEAGE]
    if exact == "drop":
        dup = df.duplicated(subset=payload, keep="first")
        df = ctx.drop_rows(df, dup, "duplicate_row", "Exact duplicate of an earlier row; dropped",
                           field=key[0], severity="warning", stage="input_validation")
    clash = df.duplicated(subset=key, keep=False)
    if not clash.any():
        return df
    if conflict == "error":
        groups = df[clash].groupby(key, dropna=False).size().head(5)
        raise EngineError("duplicate_key", f"{int(df.duplicated(subset=key).sum())} row(s) repeat a key with "
                          "different values", stage="input_validation", key=key,
                          examples={str(k): int(v) for k, v in groups.items()})
    if conflict in ("latest", "earliest"):
        if not order_by or order_by not in df.columns:
            raise EngineError("duplicate_key", f"conflict={conflict} needs keys.on_duplicate.order_by to name a column",
                              stage="input_validation", available=list(df.columns))
        ordered = df.sort_values(order_by, kind="stable", ascending=(conflict == "earliest"))
        keep_idx = ordered.drop_duplicates(subset=key, keep="last" if conflict == "latest" else "first").index
        return ctx.drop_rows(df, ~df.index.isin(keep_idx), "duplicate_key",
                             f"Key repeated; kept the {conflict} row by {order_by}", field=key[0],
                             stage="input_validation")
    if conflict == "sum":
        num = [c for c in ctx.spec.measures if c in df.columns]
        agg = {c: ((lambda s: s.sum(min_count=1)) if c in num else "first") for c in df.columns if c not in key}
        out = df.groupby(key, sort=False, dropna=False, as_index=False).agg(agg)
        ctx.issue("duplicate_key", f"{len(df) - len(out)} conflicting duplicate row(s) summed", severity="warning",
                  action="updated", stage="input_validation")
        return out[df.columns]
    drop = df.duplicated(subset=key, keep="first" if conflict == "first" else "last")
    return ctx.drop_rows(df, drop, "duplicate_key", f"Key repeated with different values; kept the {conflict} row",
                         field=key[0], stage="input_validation")


def apply_keys(df: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    """Run the declared `keys:` block: validity first, then uniqueness on cols + grain."""
    k = ctx.spec.keys
    if not k.cols:
        return df
    d_rows, d_amt = ctx.dropped_rows, ctx.dropped_amount
    before = df
    for col in k.cols:
        if col not in df.columns:
            raise EngineError("missing_required_column", f"Key column {col!r} is missing", stage="input_validation",
                              available=list(df.columns))
        if col in k.nullable:
            continue
        df = require_key(df, ctx, col, pattern=k.pattern if col == k.cols[0] else None, on_invalid=k.on_invalid)
    ctx.traced("keys:valid", before, df, d_rows, d_amt)
    d_rows, d_amt = ctx.dropped_rows, ctx.dropped_amount
    before = df
    df = dedupe(df, ctx, key=[*k.cols, *[g for g in k.grain if g in df.columns]], exact=k.on_duplicate.exact,
                conflict=k.on_duplicate.conflict, order_by=k.on_duplicate.order_by)
    ctx.traced("keys:unique", before, df, d_rows, d_amt)
    return df
