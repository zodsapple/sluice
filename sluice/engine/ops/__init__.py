"""Operation registry. An op is `fn(df, ctx, **params) -> df`, registered under a name used in `shape:`."""
from __future__ import annotations

import inspect
from typing import Any, Callable

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError

OPS: dict[str, Callable[..., pd.DataFrame]] = {}


def register_op(name: str) -> Callable[[Callable[..., pd.DataFrame]], Callable[..., pd.DataFrame]]:
    def deco(fn: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
        OPS[name] = fn
        return fn
    return deco


def run_op(name: str, params: dict[str, Any], df: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    fn = OPS.get(name)
    if fn is None:
        raise EngineError("unknown_op", f"Unknown shape op {name!r}; known: {sorted(OPS)}", stage="transformation")
    d_rows, d_amt = ctx.dropped_rows, ctx.dropped_amount
    try:
        inspect.signature(fn).bind(df, ctx, **params)
    except TypeError as exc:   # wrong/missing op parameters are a spec problem; real bugs keep their traceback
        raise EngineError("processing_failed", f"Bad parameters for op {name!r}: {exc}", stage="transformation",
                          op=name, params=params) from exc
    out = fn(df, ctx, **params)
    ctx.traced(name, df, out, d_rows, d_amt)
    return out


from sluice.engine.ops import clean, compute, derive, keys, relate, reshape, structure  # noqa: E402,F401  (registers ops)
