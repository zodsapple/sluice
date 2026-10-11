"""Lookups and the python escape hatch."""
from __future__ import annotations

import importlib
from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.ops import register_op


@register_op("lookup")
def lookup(df: pd.DataFrame, ctx: RunContext, sheet: str, by: dict[str, str], take: dict[str, str],
           header: int = 0, required: bool = False) -> pd.DataFrame:
    """Left-join columns from another sheet of the same workbook (e.g. a rate card)."""
    sheets = ctx.workbook_sheets or {}
    if sheet not in sheets:
        raise EngineError("sheet_not_found", f"Lookup sheet {sheet!r} not found", stage="transformation",
                          available_sheets=list(sheets))
    raw = sheets[sheet]
    ref = raw.iloc[header + 1:].copy()
    ref.columns = [str(c).strip() for c in raw.iloc[header].tolist()]
    right_keys = list(by.values())
    ref = ref[[*right_keys, *take.values()]].drop_duplicates(right_keys)
    ref = ref.rename(columns={v: k for k, v in take.items()})
    left = df.copy()
    for lk, rk in by.items():
        left[lk] = left[lk].astype(str).str.strip()
        ref[rk] = ref[rk].astype(str).str.strip()
    out = left.merge(ref, how="left", left_on=list(by), right_on=right_keys, suffixes=("", "_ref"))
    miss = out[list(take)[0]].isna()
    if miss.any():
        if required:
            out = ctx.drop_rows(out, miss, "missing_lookup", f"No match in {sheet!r}; row dropped",
                                field=list(by)[0], stage="transformation")
        else:
            for _, r in out[miss].head(50).iterrows():
                ctx.issue("missing_lookup", f"No match in {sheet!r}", stage="transformation",
                          row=r.get("_src_row", ""), field=list(by)[0], raw=r[list(by)[0]], action="retained")
    return out.drop(columns=[c for c in right_keys if c in out.columns and c not in df.columns])


@register_op("hook")
def hook(df: pd.DataFrame, ctx: RunContext, python: str, **params: Any) -> pd.DataFrame:
    """Escape hatch: `python: "package.module:function"` with signature (df, ctx, **params) -> df."""
    mod_name, _, fn_name = python.partition(":")
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
        return fn(df, ctx, **params)
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError("hook_failed", f"Hook {python} failed: {exc}", stage="transformation", hook=python) from exc
