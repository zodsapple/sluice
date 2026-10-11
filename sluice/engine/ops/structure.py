"""Structure ops for messy report layouts: column families, group header rows, split columns."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.ops import register_op


@register_op("melt_families")
def melt_families(df: pd.DataFrame, ctx: RunContext, families: list[dict[str, Any]], id: list[str],
                  index: str | list[str], index_type: str = "text", cell_col: str | None = None) -> pd.DataFrame:
    """Wide -> long for several column families at once.

    Each family is {pattern: regex, value: output column, type: number|text}. The regex captures the index: one
    group -> one `index` column; several groups -> `index` is a list with one output column per group (e.g. year and
    coverage from a two-level header). `cell_col` records the source cell address (B7) of every melted value.
    Blank cells stay blank (no zero fill); only `id` columns and lineage are carried over. Patterns are matched against
    header names as they appear after mapping - unmapped input headers keep their original text.
    """
    names = [index] if isinstance(index, str) else list(index)
    found: list[tuple[dict[str, Any], str, tuple[str, ...]]] = []
    for fam in families:
        rx = re.compile(fam["pattern"])
        for c in df.columns:
            m = rx.fullmatch(str(c))
            if m:
                found.append((fam, c, m.groups() if rx.groups else (str(c),)))
    if not found:
        raise EngineError("missing_required_column", "No column matches any melt family pattern",
                          stage="transformation", patterns=[f["pattern"] for f in families],
                          available=list(df.columns))
    if any(len(k) != len(names) for _, _, k in found):
        raise EngineError("processing_failed", f"melt_families: {len(names)} index name(s) but patterns capture a "
                          "different number of groups", stage="transformation")
    keys: list[tuple[str, ...]] = []
    for _, _, k in found:
        if k not in keys:
            keys.append(k)
    if index_type == "int" and len(names) == 1:
        keys.sort(key=lambda k: int(k[0]))
    carry = [c for c in ("_src_sheet", "_src_row", *id, *ctx.spec.const) if c in df.columns]      # constants survive the reshape
    base = df[carry].reset_index(drop=True)
    base["_ord"] = range(len(base))
    letters = ctx.colrefs.get(ctx.sheet_name, {})
    chunks = []
    for rank, k in enumerate(keys):
        chunk = base.copy()
        for n, v in zip(names, k):
            chunk[n] = int(v) if index_type == "int" and len(names) == 1 else v
        chunk["_rank"] = rank
        cell_letter = None
        for fam in families:
            col = next((c for f, c, kk in found if f is fam and kk == k), None)
            if col is None:
                chunk[fam["value"]] = pd.NA
                continue
            cell_letter = cell_letter or letters.get(col)
            raw = df[col].reset_index(drop=True)
            if cell_col and "_src_row" in base.columns:
                chunk[cell_col + "__" + fam["value"]] = [f"{letters.get(col, '')}{r}" for r in base["_src_row"]]
            if fam.get("type", "number") == "text":
                chunk[fam["value"]] = raw
                continue
            num = pd.to_numeric(raw, errors="coerce")
            for pos in raw[num.isna() & raw.notna()].head(50).index:
                ctx.issue("malformed_amount", f"Cannot parse {raw[pos]!r} as a number", stage="normalization",
                          field=col, row=base.at[pos, "_src_row"] if "_src_row" in base.columns else "", raw=raw[pos])
            chunk[fam["value"]] = num
        chunks.append(chunk)
    out = pd.concat(chunks, ignore_index=True).sort_values(["_ord", "_rank"], kind="stable")
    out = out.drop(columns=["_ord", "_rank"]).reset_index(drop=True)
    if cell_col:       # one address per melted row: the one of the first family that carries a value
        parts = [c for c in out.columns if c.startswith(cell_col + "__")]
        out[cell_col] = out[parts].bfill(axis=1).iloc[:, 0]
        out = out.drop(columns=parts)
    return out


@register_op("group_context")
def group_context(df: pd.DataFrame, ctx: RunContext, col: str, pattern: str) -> pd.DataFrame:
    """Group header rows (a department label on its own row) become columns on the rows below them.

    A group row is a row whose `col` matches `pattern` (named groups = new columns) while every other data
    column is empty. Group rows are consumed (logged as info); captured values are filled downwards.
    """
    rx = re.compile(pattern)
    if not rx.groupindex:
        raise EngineError("processing_failed", "group_context pattern needs named groups, e.g. (?P<dept>...)",
                          stage="transformation")
    data_cols = [c for c in df.columns if c not in ("_src_sheet", "_src_row", *ctx.spec.const) and c != col]
    cap = df[col].map(lambda v: rx.search(str(v)) if not pd.isna(v) else None)
    is_group = cap.notna() & df[data_cols].isna().all(axis=1)
    df = df.copy()
    for name in rx.groupindex:
        values = pd.Series([m.group(name).strip() if m else pd.NA for m in cap], index=df.index)
        df[name] = values.where(is_group).ffill()
    for _, r in df[is_group].iterrows():
        ctx.issue("skipped_total_row", "Group header row consumed as context", severity="info", action="skipped",
                  stage="transformation", row=r["_src_row"], raw=r[col])
    return df[~is_group].reset_index(drop=True)


@register_op("regex_replace")
def regex_replace(df: pd.DataFrame, ctx: RunContext, col: str, pattern: str, repl: str = "") -> pd.DataFrame:
    df = df.copy()
    df[col] = df[col].map(lambda v: v if pd.isna(v) else re.sub(pattern, repl, str(v)))
    return df


@register_op("block_heading")
def block_heading(df: pd.DataFrame, ctx: RunContext, col: str, out: str, marks: dict[str, str]) -> pd.DataFrame:
    """Rows whose cells in `marks` repeat their own column header ("2023/24" under "2023/24") are block headings: their `col`
    text becomes `out` on the rows below (empty above the first heading) and the heading rows are consumed."""
    ws = lambda v: re.sub(r"\s+", " ", str(v)).strip()                                       # noqa: E731
    hits = pd.Series(True, index=df.index)
    for c, header in marks.items():
        hits &= df[c].map(lambda v, h=header: not pd.isna(v) and ws(v) == ws(h))
    df = df.copy()
    df[out] = df[col].where(hits).ffill()
    for _, r in df[hits].iterrows():
        ctx.issue("skipped_total_row", "Block heading row consumed as context", severity="info", action="skipped",
                  stage="transformation", row=r["_src_row"], raw=r[col])
    return df[~hits].reset_index(drop=True)


@register_op("extract")
def extract(df: pd.DataFrame, ctx: RunContext, col: str, to: str, pattern: str) -> pd.DataFrame:
    """to = the first capture group of `pattern` found in `col` (empty when it does not match)."""
    rx = re.compile(pattern)
    df = df.copy()
    df[to] = df[col].map(lambda v: pd.NA if pd.isna(v) or not (m := rx.search(str(v))) else (m.group(1) if m.groups() else m.group(0)))
    return df


@register_op("split")
def split(df: pd.DataFrame, ctx: RunContext, col: str, into: list[str], sep: str = "|",
          strip: bool = True) -> pd.DataFrame:
    """Split one text column into several."""
    df = df.copy()
    parts = df[col].map(lambda v: [p.strip() if strip else p for p in str(v).split(sep, len(into) - 1)]
                        if not pd.isna(v) else [])
    for i, name in enumerate(into):
        df[name] = parts.map(lambda p, i=i: p[i] if i < len(p) else pd.NA)
    return df


@register_op("fill_down")
def fill_down(df: pd.DataFrame, ctx: RunContext, cols: list[str]) -> pd.DataFrame:
    """Label cells that only appear on the first row of a block (merged / grouped layouts) apply to the rows below."""
    df = df.copy()
    for c in cols:
        df[c] = df[c].ffill()
    return df
