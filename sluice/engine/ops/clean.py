"""Cell-level cleaning ops."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.ops import register_op


def id_text(value: Any) -> Any:
    """Identifier -> clean string (no '.0' float artefacts, no padding)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return pd.NA
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip(" \t\r\n")                  # ASCII whitespace only: a leading no-break space is part of the cell
    text = re.sub(r"^(\d+)\.0$", r"\1", text)
    return text or pd.NA


@register_op("strip")
def strip(df: pd.DataFrame, ctx: RunContext, cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = df[c].map(id_text)
    return df


@register_op("set")
def set_col(df: pd.DataFrame, ctx: RunContext, col: str, value: Any = None, from_col: str | None = None,
            only_if_empty: bool = False) -> pd.DataFrame:
    df = df.copy()
    new = df[from_col] if from_col else pd.Series([value] * len(df), index=df.index, dtype=object)
    if only_if_empty and col in df.columns:
        df[col] = df[col].where(df[col].notna(), new)
    else:
        df[col] = new
    return df


@register_op("map_values")
def map_values(df: pd.DataFrame, ctx: RunContext, col: str, mapping: dict[str, Any],
               case_insensitive: bool = True, into: str | None = None) -> pd.DataFrame:
    df = df.copy()
    def ws(x: Any) -> str:
        return re.sub(r"\s+", " ", str(x)).strip()

    table = {(ws(k).upper() if case_insensitive else ws(k)): v for k, v in mapping.items()}

    def conv(v: Any) -> Any:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return v
        key = ws(v)
        return table.get(key.upper() if case_insensitive else key, v)

    df[into or col] = df[col].map(conv)
    return df


def _iso(value: Any, dayfirst: bool, formats: list[str], serial: bool, sentinels: dict[str, str]) -> Any:
    """One cell -> 'YYYY-MM-DD' text (python datetime: safe for 9999-01-01), or NA when unparseable."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return pd.NA
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip().removesuffix(".0")
    if text.upper() in sentinels:
        return sentinels[text.upper()]
    if serial:
        try:
            f = float(text)
            if 30000 <= f <= 100000:
                return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(f))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    ts = pd.to_datetime(text, errors="coerce", dayfirst=dayfirst)
    return pd.NA if pd.isna(ts) else ts.strftime("%Y-%m-%d")


@register_op("parse_date")
def parse_date(df: pd.DataFrame, ctx: RunContext, cols: list[str], dayfirst: bool = False,
               formats: list[str] | None = None, excel_serial: bool = True, as_text: bool = False,
               sentinels: dict[str, str] | None = None) -> pd.DataFrame:
    """Mixed date representations -> datetime64 (default) or ISO text (`as_text`, needed for 9999-01-01 sentinels)."""
    df = df.copy()
    formats = formats or ["%Y%m%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
    sent = {str(k).upper(): str(v) for k, v in (sentinels or {}).items()}
    for c in cols:
        if c not in df.columns:
            continue
        col = df[c]
        if as_text or sent:
            parsed = col.map(lambda v: _iso(v, dayfirst, formats, excel_serial, sent))
        else:
            # fast path: cells that already are datetimes convert in one vectorised call
            is_dt = col.map(lambda v: isinstance(v, (datetime, date, pd.Timestamp)))
            parsed = pd.Series(pd.NaT, index=col.index, dtype="datetime64[ns]")
            if is_dt.any():
                parsed[is_dt] = pd.to_datetime(col[is_dt].astype(object), errors="coerce")
            rest = ~is_dt & col.notna()
            if rest.any():
                parsed[rest] = pd.to_datetime(
                    col[rest].map(lambda v: _iso(v, dayfirst, formats, excel_serial, {})), errors="coerce")
        bad = parsed.isna() & col.notna()
        for _, r in df[bad].head(50).iterrows():
            ctx.issue("malformed_date", f"Cannot parse {r[c]!r} as a date", stage="normalization", field=c,
                      row=r.get("_src_row", ""), raw=r[c], action="flagged")
        df[c] = parsed
    return df


@register_op("parse_number")
def parse_number(df: pd.DataFrame, ctx: RunContext, cols: list[str], decimal: str = ".", decimals: int | None = None,
                 integer: bool = False) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            continue

        def conv(v: Any) -> Any:
            if v is None or (not isinstance(v, str) and pd.isna(v)):
                return float("nan")
            if isinstance(v, (int, float)):
                return float(v)
            t = re.sub(r"[^\d,.\-]", "", str(v))
            t = t.replace(".", "").replace(",", ".") if decimal == "," else t.replace(",", "")
            try:
                return float(t)
            except ValueError:
                return float("nan")

        num = df[c].map(conv)
        bad = num.isna() & df[c].notna()
        for _, r in df[bad].head(50).iterrows():
            ctx.issue("malformed_amount", f"Cannot parse {r[c]!r} as a number", stage="normalization", field=c,
                      row=r.get("_src_row", ""), raw=r[c])
        if integer:
            num = num.round().astype("Int64")                                  # whole numbers print without ".0"
        df[c] = num.round(decimals) if decimals is not None and not integer else num          # decimals: removes float residue (103.17999999999999)
    return df


@register_op("filter")
def filter_rows(df: pd.DataFrame, ctx: RunContext, col: str, action: str = "keep", equals: Any = None,
                in_: list[Any] | None = None, regex: str | None = None, not_null: bool = False,
                reason: str = "Row excluded by filter") -> pd.DataFrame:
    s = df[col]
    if equals is not None:
        hit = s.astype(str).str.strip().str.upper() == str(equals).upper()
    elif in_ is not None:
        hit = s.astype(str).str.strip().str.upper().isin([str(x).upper() for x in in_])
    elif regex is not None:
        hit = s.astype(str).str.contains(regex, regex=True, na=False)
    elif not_null:
        hit = s.notna()
    else:
        raise TypeError("filter needs one of equals / in_ / regex / not_null")
    drop = ~hit if action == "keep" else hit
    return ctx.drop_rows(df, drop, "filtered_source_row", reason, field=col, severity="info", action="excluded",
                         stage="input_validation")


@register_op("format_date")
def format_date(df: pd.DataFrame, ctx: RunContext, cols: list[str], fmt: str = "%Y-%m-%d") -> pd.DataFrame:
    """Render date columns (datetime or ISO text) in the target format, e.g. %d.%m.%Y or %Y%m."""
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            continue

        def conv(v: Any) -> Any:
            if v is None or (not isinstance(v, str) and pd.isna(v)):
                return pd.NA
            if hasattr(v, "strftime"):
                return v.strftime(fmt)
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime(fmt)

        df[c] = df[c].map(conv)
    return df
