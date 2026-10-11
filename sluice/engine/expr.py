"""Small, safe row-condition language used by controls (`where:`). No eval."""
from __future__ import annotations

from typing import Any

import pandas as pd

from sluice.engine.diagnosis import EngineError

OPS = ("lt", "le", "gt", "ge", "eq", "ne", "isnull", "notnull", "startswith", "regex", "in", "same_as", "not_in", "not_regex",
       "lt_col", "le_col", "gt_col", "ge_col")


def _text(s: pd.Series) -> pd.Series:
    def one(v):
        if pd.isna(v):
            return ""
        if hasattr(v, "strftime"):                                   # a date cell compares as its ISO date
            return v.strftime("%Y-%m-%d") if (getattr(v, "hour", 0), getattr(v, "minute", 0), getattr(v, "second", 0)) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M:%S")
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()

    return s.map(one)


def where_mask(df: pd.DataFrame, where: dict[str, Any] | None) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, cond in (where or {}).items():
        if col not in df.columns:
            raise EngineError("control_column_missing", f"`where` column {col!r} does not exist",
                              stage="output_validation", available=list(df.columns))
        s = df[col]
        if not isinstance(cond, dict):
            cond = {"in": cond} if isinstance(cond, list) else {"eq": cond}
        for op, val in cond.items():
            if op == "isnull":
                m = s.isna() if val else s.notna()
            elif op == "notnull":
                m = s.notna() if val else s.isna()
            elif op == "startswith":
                m = _text(s).str.startswith(str(val))
            elif op == "regex":
                m = _text(s).str.contains(str(val), regex=True)
            elif op == "same_as":          # equal to the text of another column
                m = _text(s) == _text(df[val])
            elif op == "in":
                m = _text(s).isin([str(v) for v in val])
            elif op == "not_in":
                m = ~_text(s).isin([str(v) for v in val]) & s.notna()
            elif op == "not_regex":
                m = ~_text(s).str.contains(str(val), regex=True) & s.notna()
            elif op in ("eq", "ne") and not isinstance(val, (int, float)):
                m = (_text(s) == str(val)) if op == "eq" else (_text(s) != str(val))
            elif op in ("lt_col", "le_col", "gt_col", "ge_col"):             # numeric comparison with another column
                if val not in df.columns:
                    raise EngineError("control_column_missing", f"`where` compares with {val!r}, which does not exist",
                                      stage="output_validation", available=list(df.columns))
                x, y = pd.to_numeric(s, errors="coerce"), pd.to_numeric(df[val], errors="coerce")
                m = {"lt_col": x < y, "le_col": x <= y, "gt_col": x > y, "ge_col": x >= y}[op]
            elif op in ("lt", "le", "gt", "ge") and isinstance(val, str):        # ISO dates and other text compare as text
                t = _text(s)
                m = {"lt": t < val, "le": t <= val, "gt": t > val, "ge": t >= val}[op] & s.notna()
            elif op in ("lt", "le", "gt", "ge", "eq", "ne"):
                n = pd.to_numeric(s, errors="coerce")
                m = {"lt": n < val, "le": n <= val, "gt": n > val, "ge": n >= val, "eq": n == val, "ne": n != val}[op]
            else:
                raise EngineError("control_column_missing", f"Unknown `where` operator {op!r}; known: {OPS}",
                                  stage="output_validation")
            mask &= m.fillna(False)
    return mask
