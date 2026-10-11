"""Shape-changing ops: unpivot, explode (with cent-exact allocation), pivot."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.ops import register_op
from sluice.engine.ops.clean import id_text


@register_op("unpivot")
def unpivot(df: pd.DataFrame, ctx: RunContext, cols: dict[str, str], type_col: str = "cost_type",
            value_col: str = "cost", skip_zero: bool = True) -> pd.DataFrame:
    """Wide -> long: each `cols` entry (column -> label) becomes one row per input row."""
    present = {c: label for c, label in cols.items() if c in df.columns}
    for c in cols:
        if c not in present:
            ctx.issue("missing_configured_column", f"Unpivot column {c!r} not present", severity="warning",
                      stage="transformation", field=c, action="skipped")
    if not present:
        raise EngineError("missing_required_column", "None of the unpivot columns exist", stage="transformation",
                          wanted=list(cols), available=list(df.columns))
    keep = [c for c in df.columns if c not in present]
    chunks = []
    for c, label in present.items():
        raw = df[c]
        num = pd.to_numeric(raw, errors="coerce")
        for _, r in df[num.isna() & raw.notna()].head(50).iterrows():
            ctx.issue("malformed_amount", f"Cannot parse {r[c]!r} as an amount", stage="normalization", field=c,
                      row=r.get("_src_row", ""), raw=r[c])
        chunk = df[keep].copy()
        chunk[type_col] = label
        chunk[value_col] = num.round(2).fillna(0.0)
        if skip_zero:
            chunk = chunk[chunk[value_col] != 0]
        chunks.append(chunk)
    out = pd.concat(chunks, ignore_index=True)
    return out.sort_values("_src_row", kind="stable").reset_index(drop=True) if "_src_row" in out.columns else out


def split_cents(total: float, parts: int) -> list[float]:
    """Even split to the cent; sum(result) == round(total, 2)."""
    cents = int(round(float(total) * 100))
    base, rem = divmod(cents, parts)
    return [(base + (1 if i < rem else 0)) / 100 for i in range(parts)]


@register_op("explode")
def explode(df: pd.DataFrame, ctx: RunContext, col: str, sep: list[str] | str = (";",), pattern: str | None = None,
            allocate: list[str] | None = None) -> pd.DataFrame:
    """One row per token of a multi-value cell; `allocate` columns are split evenly (to the cent) across tokens."""
    seps = [sep] if isinstance(sep, str) else list(sep)
    splitter = re.compile("|".join(re.escape(s) for s in seps)) if seps else None
    rx = re.compile(pattern) if pattern else None
    allocate = [c for c in (allocate or []) if c in df.columns]
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        value = id_text(r[col])
        tokens = [value] if pd.isna(value) else [t.strip() for t in (splitter.split(str(value)) if splitter else [str(value)])]
        tokens = [t for t in tokens if pd.isna(t) or t != ""] or [value]
        if rx:   # keep unmatched tokens so require_key can report them
            tokens = [(m.group(0) if (m := rx.search(str(t))) else t) if not pd.isna(t) else t for t in tokens]
        shares = {c: split_cents(pd.to_numeric(r[c], errors="coerce") if pd.notna(r[c]) else 0.0, len(tokens))
                  for c in allocate}
        for i, tok in enumerate(tokens):
            rec = r.to_dict()
            rec[col] = tok
            for c in allocate:
                rec[c] = shares[c][i]
            rows.append(rec)
    return pd.DataFrame(rows, columns=df.columns)


@register_op("pivot")
def pivot(df: pd.DataFrame, ctx: RunContext, index: list[str], columns: str, values: str,
          agg: str = "sum", prefix: str = "") -> pd.DataFrame:
    wide = df.pivot_table(index=index, columns=columns, values=values, aggfunc=agg, fill_value=0, dropna=False)
    wide.columns = [f"{prefix}{c}" for c in wide.columns]
    firsts = df.drop(columns=[columns, values]).drop_duplicates(index).set_index(index)
    return firsts.join(wide).reset_index()
