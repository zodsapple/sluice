"""Relating tables: joins between declared sources, validity intervals, as-of snapshots, presence flags."""
from __future__ import annotations

from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.ops import register_op


def _lineage_merge(left: pd.DataFrame, right: pd.DataFrame, **kw: Any) -> pd.DataFrame:
    """Merge keeping the left lineage, falling back to the right one for right-only rows."""
    right = right.rename(columns={"_src_row": "_r_src_row", "_src_sheet": "_r_src_sheet"})
    out = left.merge(right, **kw)
    for c in ("_src_row", "_src_sheet"):
        if f"_r{c}" in out.columns:
            out[c] = out[c].where(out[c].notna(), out[f"_r{c}"])
            out = out.drop(columns=[f"_r{c}"])
    return out


def _source(ctx: RunContext, name: str) -> pd.DataFrame:
    if name not in ctx.sources:
        raise EngineError("sheet_not_found", f"Source {name!r} is not declared under `sources`", stage="transformation",
                          available_sheets=list(ctx.sources))
    return ctx.sources[name]


@register_op("join_source")
def join_source(df: pd.DataFrame, ctx: RunContext, source: str, by: list[str], how: str = "outer") -> pd.DataFrame:
    """Merge another declared source on key columns (keys compared as text)."""
    right = _source(ctx, source).copy()
    left = df.copy()
    for k in by:
        left[k] = left[k].astype("string")
        right[k] = right[k].astype("string")
    shared = [c for c in right.columns if c in left.columns and c not in by and c not in ("_src_row", "_src_sheet")]
    right = right.drop(columns=shared)       # left side wins on same-named non-key columns
    return _lineage_merge(left, right, on=by, how=how, sort=False)


@register_op("concat_source")
def concat_source(df: pd.DataFrame, ctx: RunContext, source: str) -> pd.DataFrame:
    return pd.concat([df, _source(ctx, source)], ignore_index=True)


@register_op("interval_join")
def interval_join(df: pd.DataFrame, ctx: RunContext, source: str, by: list[str], left_from: str, left_to: str,
                  right_from: str, right_to: str, out_from: str = "effective_from", out_to: str = "effective_to",
                  bring: list[str] | None = None) -> pd.DataFrame:
    """Half-open [from, to) interval intersection join. Dates must be ISO text (parse_date with as_text)."""
    right = _source(ctx, source)
    bring = bring or [c for c in right.columns if c not in (*by, right_from, right_to, "_src_row", "_src_sheet")]
    right = right[[*by, right_from, right_to, *bring]]
    left = df.copy()
    for k in by:
        left[k] = left[k].astype("string")
        right[k] = right[k].astype("string")
    both = left.merge(right, on=by, how="inner")
    both = both[(both[left_from] < both[right_to]) & (both[right_from] < both[left_to])].copy()
    both[out_from] = both[[left_from, right_from]].max(axis=1)
    both[out_to] = both[[left_to, right_to]].min(axis=1)
    both = both[both[out_from] < both[out_to]]
    return both.reset_index(drop=True)


@register_op("asof_filter")
def asof_filter(df: pd.DataFrame, ctx: RunContext, from_col: str, to_col: str, as_of: str,
                out_col: str = "as_of") -> pd.DataFrame:
    """Keep rows valid at `as_of` under [from, to). Dates are ISO text."""
    out = df[(df[from_col] <= as_of) & (as_of < df[to_col])].copy()
    out[out_col] = as_of
    return out.reset_index(drop=True)


@register_op("flag_present")
def flag_present(df: pd.DataFrame, ctx: RunContext, cols: list[str], out: str = "source_present") -> pd.DataFrame:
    """1 when any of `cols` has a value in the source, else 0 (blank is not zero)."""
    df = df.copy()
    df[out] = df[cols].notna().any(axis=1).astype(int)
    return df


@register_op("rename")
def rename(df: pd.DataFrame, ctx: RunContext, columns: dict[str, str]) -> pd.DataFrame:
    return df.rename(columns=columns)
