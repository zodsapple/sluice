"""Derived columns: templated text and exact decimal scaling."""
from __future__ import annotations

import re
import string
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.expr import where_mask
from sluice.engine.ops import register_op


def _s(v: Any) -> str:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


class _Fmt(string.Formatter):
    """{cell:%Y-%m-%dT%H:%M:%S} renders a date cell with that pattern; any other value is used as printed."""

    def format_field(self, value: Any, spec: str) -> str:
        if spec.startswith("%"):
            return value.strftime(spec) if hasattr(value, "strftime") else _s(value)
        return super().format_field(value, spec)


def _has_spec(template: str, name: str) -> bool:
    return any(f == name and (spec or "").startswith("%") for _, f, spec, _ in string.Formatter().parse(template))


@register_op("derive")
def derive(df: pd.DataFrame, ctx: RunContext, col: str, template: str, where: dict[str, Any] | None = None) -> pd.DataFrame:
    """col = template.format(**row) for rows matching `where`; other rows keep their value (or stay empty)."""
    need = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise EngineError("missing_required_column", f"derive {col!r}: columns {missing} do not exist",
                          stage="transformation", available=list(df.columns))
    df = df.copy()
    mask = where_mask(df, where) if where else pd.Series(True, index=df.index)
    if col not in df.columns:
        df[col] = pd.NA
    if not need:                                  # constant template
        df.loc[mask, col] = template
        return df
    rows = df.loc[mask, need].to_dict("records")
    df.loc[mask, col] = [_Fmt().vformat(template, (), {k: (v if _has_spec(template, k) else _s(v)) for k, v in r.items()}) for r in rows]
    return df


@register_op("scale")
def scale(df: pd.DataFrame, ctx: RunContext, col: str, factor: float | int | str, out: str | None = None,
          offset: float | int | str = 0, where: dict[str, Any] | None = None) -> pd.DataFrame:
    """out = col * factor with decimal arithmetic on the printed value (2536.28723216103 * 1000 stays exact).
    `where`: only those rows are computed (different factors per measure: one scale per case)."""
    df = df.copy()
    f = Decimal(str(factor))
    off = Decimal(str(offset))

    def conv(v: Any) -> Any:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return pd.NA
        try:
            return float(Decimal(repr(float(v))) * f + off)
        except (TypeError, ValueError):       # markers such as "X" (not applicable) carry no number
            return pd.NA

    target = out or col
    if where:
        mask = where_mask(df, where)
        if target not in df.columns:
            df[target] = pd.NA
        df.loc[mask, target] = df.loc[mask, col].map(conv)
        return df
    df[target] = df[col].map(conv)
    return df


@register_op("flag")
def flag(df: pd.DataFrame, ctx: RunContext, out: str, any_of: list[dict[str, Any]]) -> pd.DataFrame:
    """out = True when ANY listed condition holds for the row. Conditions use the control `where` language."""
    df = df.copy()
    hit = pd.Series(False, index=df.index)
    for cond in any_of:
        hit |= where_mask(df, cond)
    df[out] = hit
    return df


@register_op("occurrence")
def occurrence(df: pd.DataFrame, ctx: RunContext, by: list[str], out: str) -> pd.DataFrame:
    """out = 1-based count of equal records (same values in `by`) in source order: repeated rows are kept, visibly."""
    df = df.copy()
    key = df[by].fillna("").astype(str).agg("\x1f".join, axis=1) if by else pd.Series("", index=df.index)
    df[out] = (df.groupby(key, sort=False).cumcount() + 1).astype(str)
    return df


@register_op("group_flag")
def group_flag(df: pd.DataFrame, ctx: RunContext, by: list[str], out: str, repeated: str, otherwise: str) -> pd.DataFrame:
    """out = `repeated` when the row shares its `by` values with another row, else `otherwise`. Nothing is dropped."""
    df = df.copy()
    key = df[by].fillna("").astype(str).agg("\x1f".join, axis=1)
    size = key.groupby(key).transform("size")
    df[out] = size.map(lambda n: repeated if n > 1 else otherwise)
    return df


@register_op("flag_list")
def flag_list(df: pd.DataFrame, ctx: RunContext, out: str, rules: list[dict[str, Any]], sep: str = ";") -> pd.DataFrame:
    """out = the labels of every rule that holds for the row, sorted and joined (empty when none). Rows are never dropped."""
    df = df.copy()
    hits = []
    for r in rules:
        m = pd.Series(False, index=df.index)
        for cond in r["any_of"]:
            m |= where_mask(df, cond)
        hits.append((r["label"], m))
    df[out] = [sep.join(sorted(lab for lab, m in hits if m.iloc[i])) for i in range(len(df))]
    return df


@register_op("copy_col")
def copy_col(df: pd.DataFrame, ctx: RunContext, source: str, to: str) -> pd.DataFrame:
    """to = the cells of `source` (a header with spaces or brackets cannot be a template placeholder)."""
    if source not in df.columns:
        raise EngineError("missing_required_column", f"copy_col: column {source!r} not found", stage="transformation",
                          available=list(df.columns))
    df = df.copy()
    df[to] = df[source]
    return df


_MONTHS = {m.lower(): i for i, m in enumerate(__import__("calendar").month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(__import__("calendar").month_abbr) if m})


def period_of(label: Any) -> tuple[str, date, date] | None:
    """A period label -> (type, first day, last day): 2023 | 2023 Q2 | Q2 2023 | 2023 JAN | January 2023 | 2023-04 | 2023/04."""
    import calendar


    t = re.sub(r"\s+", " ", str(label)).strip()
    if m := re.fullmatch(r"((?:19|20)\d{2})", t):
        y = int(m[1])
        return "year", date(y, 1, 1), date(y, 12, 31)
    if m := re.fullmatch(r"((?:19|20)\d{2}) ?Q([1-4])|Q([1-4]) ?((?:19|20)\d{2})", t, re.I):
        y, q = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        return "quarter", date(y, 3 * q - 2, 1), date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
    mon = None
    if m := re.fullmatch(r"((?:19|20)\d{2}) ?([A-Za-z]{3,9})", t):
        y, mon = int(m[1]), _MONTHS.get(m[2].lower())
    elif m := re.fullmatch(r"([A-Za-z]{3,9}) ?((?:19|20)\d{2})", t):
        y, mon = int(m[2]), _MONTHS.get(m[1].lower())
    elif m := re.fullmatch(r"((?:19|20)\d{2})[-/](\d{2})", t):
        y, mon = int(m[1]), int(m[2])
    if mon and 1 <= mon <= 12:
        return "month", date(y, mon, 1), date(y, mon, calendar.monthrange(y, mon)[1])
    return None


@register_op("period_bounds")
def period_bounds(df: pd.DataFrame, ctx: RunContext, col: str, out: str, part: str) -> pd.DataFrame:
    """out = type / first day / last day of the period named in `col` ("2023 Q2" -> quarter, 2023-04-01, 2023-06-30)."""
    df = df.copy()

    def one(v: Any) -> Any:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return pd.NA
        p = period_of(v)
        if p is None:
            ctx.issue("malformed_date", f"Cannot read {v!r} as a period", stage="normalization", field=out, raw=v, action="flagged")
            return pd.NA
        return p[0] if part == "type" else (p[1] if part == "start" else p[2]).isoformat()

    df[out] = df[col].map(one)
    return df


@register_op("arith")
def arith(df: pd.DataFrame, ctx: RunContext, out: str, op_name: str, a: str, b: str, decimals: int | None = None) -> pd.DataFrame:
    """out = a (+ - * /) b on two columns, with decimal arithmetic on the printed values."""
    df = df.copy()

    def one(x: Any, y: Any) -> Any:
        try:
            dx, dy = Decimal(str(x).replace(",", "")), Decimal(str(y).replace(",", ""))
            r = {"mul": dx * dy, "add": dx + dy, "sub": dx - dy, "div": (dx / dy if dy != 0 else None)}[op_name]
        except (InvalidOperation, ValueError, TypeError, KeyError):
            return pd.NA
        if r is None:
            return pd.NA
        return float(round(r, decimals)) if decimals is not None else float(r)

    df[out] = [one(x, y) for x, y in zip(df[a], df[b])]
    return df


@register_op("cell_ref")
def cell_ref(df: pd.DataFrame, ctx: RunContext, header: str, out: str = "source_cell") -> pd.DataFrame:
    """out = A1 address (column letter of the source header + source row) of every row's value in `header`."""
    letters = ctx.colrefs.get(ctx.sheet_name, {})
    if header not in letters:
        raise EngineError("missing_required_column", f"cell_ref: header {header!r} not found in sheet {ctx.sheet_name!r}",
                          stage="transformation", available=list(letters))
    df = df.copy()
    df[out] = letters[header] + df["_src_row"].astype(int).astype(str)
    return df


@register_op("sheet_cell")
def sheet_cell(df: pd.DataFrame, ctx: RunContext, cell: str, out: str, mapping: dict[str, str] | None = None,
               date: bool = False) -> pd.DataFrame:
    """out = a fact printed once on the sheet (title, unit, reporting date), read from the raw grid at run time.

    `mapping` translates by substring (case-insensitive); `date` extracts dd.mm.yyyy / yyyy-mm-dd as ISO text."""
    import re
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    raw = ctx.raw_sheets.get(ctx.sheet_name)
    if raw is None:
        raise EngineError("sheet_not_found", f"sheet_cell: no raw grid for {ctx.sheet_name!r}", stage="transformation")
    col, row = coordinate_from_string(cell)
    try:
        text = raw.iat[row - 1, column_index_from_string(col) - 1]
    except IndexError as exc:
        raise EngineError("missing_required_column", f"sheet_cell: {cell} is outside the sheet", stage="transformation") from exc
    text = "" if text is None or (not isinstance(text, str) and pd.isna(text)) else str(text)
    value: Any = pd.NA
    if mapping:
        low = text.lower()
        value = next((v for k, v in mapping.items() if k.lower() in low), pd.NA)
    elif date:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        n = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        value = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else (n.group(0) if n else pd.NA)
    else:
        value = text.strip() or pd.NA
    if pd.isna(value):
        ctx.issue("malformed_date" if date else "missing_lookup", f"sheet_cell {cell}: nothing usable in {text[:60]!r}",
                  stage="transformation", field=out)
    df = df.copy()
    df[out] = value
    return df
