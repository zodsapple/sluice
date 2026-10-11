"""Structural frame of one sheet: layout, neutral column names, reshaping - everything that needs no business meaning.

The frame is what is the same for a rule, a human and an LLM: it fixes WHERE the data is (header rows, group rows, merged
labels, multi-level headers, wide value columns) and names every available source neutrally. Business meaning (which
field is the measure, what 'Frauen' stands for) is added afterwards as bindings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from sluice.infer.profile import SheetProfile
from sluice.infer.bindings import ValidationContext, TargetField

ID_LIKE = re.compile(r"(?i)(^|_)(id|code|key|no|nr)(_|$)")


def snake(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z])([A-Z])", r"\1_\2", str(s)).lower()).strip("_")
    return out or "col"


@dataclass
class SourceInfo:
    name: str
    kind: str                       # column | group | dim | value | cell
    dtype: str = "text"
    null_rate: float = 0.0
    distinct: list[str] = field(default_factory=list)       # up to 8 sample values
    n_distinct: int = 0
    origin: str = ""                # header text / cell reference this came from


@dataclass
class Frame:
    sheet: str
    locate: dict[str, Any]
    map: dict[str, Any]
    shape: list[dict[str, Any]]                 # structural ops only
    sources: dict[str, SourceInfo]
    facts: dict[str, str]                       # banner cells above the header, by A1 reference
    layout: str                                 # tidy | grid | wide
    dims: dict[str, list[str]] = field(default_factory=dict)         # dim1 / dim2 -> distinct header values
    grid_pattern: str | None = None
    value_source: str | None = None             # name that holds the observed number (as text, X possible)
    notes: list[str] = field(default_factory=list)
    distinct_all: dict[str, list[str]] = field(default_factory=dict)    # every distinct value (<= 60) per source
    unit_texts: list[str] = field(default_factory=list)                 # text rows between header and data ("en milliers")
    value_tokens: list[str] = field(default_factory=list)               # non-numeric markers inside value cells (X)
    data: pd.DataFrame | None = field(default=None, repr=False)         # body rows under the neutral names (before reshaping)
    grid_cols: list[str] = field(default_factory=list)                  # raw 'top|sub' header names of the value columns
    wide_numeric: dict[str, str] = field(default_factory=dict)          # neutral name -> raw header of every melted-by-default column
    first_row: int = 0                                                  # physical (1-based) row of the first body row
    file_name: str = ""                                                 # name of the workbook (periods are often printed only there)

    def context(self, targets: dict[str, TargetField], bound: set[str] | None = None) -> ValidationContext:
        dim_values = {k: list(v) for k, v in self.dims.items()}
        for n, vals in self.distinct_all.items():
            dim_values.setdefault(n, list(vals))
        return ValidationContext(targets=targets, sources=set(self.sources), dim_values=dim_values,
                                 cells=set(self.facts), bound=bound or set())


def _block_marks_hit(data: pd.DataFrame, neutral: dict[str, str], marks: dict[str, str]) -> pd.Series:
    hit = pd.Series(True, index=data.index)
    inv = {n: h for h, n in neutral.items()}
    for n, header in marks.items():
        hit &= data[inv[n]].map(lambda v, h=header: not pd.isna(v) and re.sub(r"\s+", " ", str(v)).strip() == h)
    return hit


def _block_labels(data: pd.DataFrame, neutral: dict[str, str], marks: dict[str, str]) -> list[str]:
    first_raw = next(iter(neutral))
    return list(data.loc[_block_marks_hit(data, neutral, marks), first_raw].dropna())


def _block_heading_marks(data: pd.DataFrame, neutral: dict[str, str], prof_by: dict, flat: bool) -> dict[str, str]:
    """Columns whose cells, on some rows, repeat their own header: {neutral name: header text}. Needs >= 2 such rows and >= 2 columns."""
    ws = lambda x: re.sub(r"\s+", " ", str(x)).strip()                                       # noqa: E731
    cols = [h for h in list(neutral)[1:] if len(h) > 0 and not h.startswith("Unnamed")]
    cand = {neutral[h]: ws(h.split("|")[-1]) for h in cols if data[h].map(lambda v, hh=h: not pd.isna(v) and ws(v) == ws(hh.split("|")[-1])).sum() >= 2}
    if len(cand) < 2:
        return {}
    return cand if _block_marks_hit(data, neutral, cand).sum() >= 2 else {}


def _code_like(s: pd.Series) -> bool:
    """A year or a region code: whole numbers of one fixed width with few distinct values are labels, not measures."""
    vals = s.dropna().map(lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip())
    return bool(len(vals)) and vals.str.fullmatch(r"\d+").all() and vals.str.len().nunique() == 1 and vals.nunique() <= 30


def numeric_cols_for_tokens(p: SheetProfile, neutral: dict[str, str], wide_numeric: dict[str, str]) -> list[str]:
    return [h for h in wide_numeric.values() if h in p.data.columns]


def _distinct(s: pd.Series, limit: int = 60) -> tuple[list[str], int]:
    vals = s.dropna().map(lambda v: v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else
                          (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()))
    u = list(dict.fromkeys(vals))
    return u[:limit], len(u)


def _skip_patterns(p: SheetProfile) -> list[str]:
    pats = []
    for _, label, kind in p.skip_labels:
        pats.append(f"^{re.escape(label)}$" if kind == "repeated_header" else "^" + re.escape(label[:18]))
    return sorted(set(pats))


def build_frame(p: SheetProfile, flat: bool = False, fold: bool = False) -> Frame:
    locate: dict[str, Any] = {"sheet": {"name": p.name}}
    if len(p.header_rows) > 1:
        locate["header_rows"] = p.header_rows
    else:
        locate["header"] = p.header_rows[0]
    if p.data_start != max(p.header_rows) + 1:
        locate["data_start"] = p.data_start
    if p.synthetic is not None:
        locate = {"sheet": {"name": p.name}, "synthetic_header": list(p.synthetic), "data_start": p.data_start}
    pats = _skip_patterns(p)
    if pats:
        locate["skip_rows_matching"] = pats

    data = p.data
    grid = [] if flat else [c for c in p.grid_cols]      # a record table keeps band headers as plain columns (lowest level names)
    plain = [c for c in p.flat_names if c not in grid]
    neutral: dict[str, str] = {}              # raw header -> neutral
    used: set[str] = set()
    for h in plain:
        h_low = h.split("|")[-1] if (flat or h not in p.grid_cols) else h
        n = snake(h_low) if not h_low.startswith("Unnamed") else f"unnamed_{h_low.split(':')[-1].strip()}"
        base, k = n, 2
        while n in used or n in ("dim1", "dim2", "g1", "g2", "cell_value", "source_cell"):
            n, k = f"{base}_{k}", k + 1
        used.add(n)
        neutral[h] = n
    cmap = {h: {"to": n, "required": False} for h, n in neutral.items()}
    prof_early = {c.name: c for c in p.columns}
    if fold and grid:          # a plain numeric column beside a header band (a base year next to the estimates) is a value column too
        for h in list(plain):
            c = prof_early.get(h)
            if c is not None and c.dtype in ("int", "decimal") and "|" not in h and not ID_LIKE.search(neutral[h]):
                new = f"{h}|{h}"
                cmap[h] = {"to": new, "required": False}
                plain.remove(h)
                neutral.pop(h)
                grid.append(new)
                data = data.rename(columns={h: new})
    composite: dict[str, list[str]] = {}
    for h in plain:            # "freq,age,unit,sex,geo\\TIME_PERIOD": several keys written in one column
        if h.count(",") >= 2 and p.data[h].dropna().astype(str).str.count(",").nunique() == 1:
            parts = [x.strip() for x in re.split(r"[,\\]", h)]
            keys = parts[:-1] if "\\" in h else parts
            if p.data[h].dropna().astype(str).str.count(",").iloc[0] == len(keys) - 1:
                composite[h] = keys

    shape: list[dict[str, Any]] = []
    sources: dict[str, SourceInfo] = {}
    distinct_all: dict[str, list[str]] = {}
    prof_by = {c.name: c for c in p.columns}
    norm_ops: list[dict[str, Any]] = []
    dtype_over: dict[str, str] = {}
    extra_cols: dict[str, pd.Series] = {}
    for h, keys in composite.items():
        data = data.copy()
        pieces = data[h].map(lambda v: [x.strip() for x in str(v).split(",", len(keys) - 1)] if not pd.isna(v) else [None] * len(keys))
        names = []
        for i, k in enumerate(keys):
            n0 = snake(k)
            n, kk = n0, 2
            while n in used or n in ("dim1", "dim2", "g1", "g2", "cell_value", "source_cell"):
                n, kk = f"{n0}_{kk}", kk + 1
            used.add(n)
            names.append(n)
            extra_cols[n] = pieces.map(lambda v, i=i: v[i])
        norm_ops.append({"op": "split", "col": neutral[h], "into": names, "sep": ","})
        neutral.pop(h)
        cmap[h] = {"to": f"__composite_{snake(h)}", "required": False}
        norm_ops[-1]["col"] = cmap[h]["to"]

    if flat:                                          # cell-level clean-up every downstream rule should see through
        data = data.copy()
        for h, n in neutral.items():
            col = data[h].dropna()
            if prof_by[h].dtype not in ("text", "id_text") or col.empty or not col.map(lambda v: isinstance(v, str)).all():
                continue
            if col.str.fullmatch(r'\s*".*"\s*').mean() >= 0.9:                      # every cell carries literal quote marks
                data[h] = data[h].map(lambda v: v if pd.isna(v) else re.sub(r'^\s*"(.*)"\s*$', r"\1", v))
                norm_ops.append({"op": "regex_replace", "col": n, "pattern": r'^\s*"(.*)"\s*$', "repl": r"\1"})
                col = data[h].dropna()
            if col.str.fullmatch(r"-?\d+(\.\d{3})*(,\d+)?").all() and (col.str.contains(",").any() or col.str.count(r"\.").ge(2).any()):          # decimal comma
                data[h] = data[h].map(lambda v: v if pd.isna(v) else float(v.replace(".", "").replace(",", ".")))
                norm_ops.append({"op": "parse_number", "cols": [n], "decimal": ","})
                dtype_over[n] = "decimal"
    for n, ser in extra_cols.items():
        d, nd = _distinct(ser)
        sources[n] = SourceInfo(n, "column", "text", float(ser.isna().mean()), d[:8], nd, origin="part of a composite key column")
        distinct_all[n] = d
    for h, n in neutral.items():
        col = prof_by[h]
        d, nd = _distinct(data[h])
        sources[n] = SourceInfo(n, "column", dtype_over.get(n, col.dtype), col.null_rate, d[:8], nd,
                                origin=(h.split("|")[-1] if (flat or h not in p.grid_cols) else h).replace("\n", " "))
        distinct_all[n] = d

    # block labels given only on the first row of a block
    fills = [neutral[c] for c in p.fill_cols if c in neutral]
    first_col = (neutral[plain[0]] if plain[0] in neutral else cmap[plain[0]]["to"]) if plain else None
    if fills:
        shape.append({"op": "strip", "cols": fills})
        shape.append({"op": "fill_down", "cols": fills})
    # group header rows become context columns
    if p.group_rows and first_col:
        shape.append({"op": "strip", "cols": [first_col]})
        shape.append({"op": "group_context", "col": first_col, "pattern": p.group_pattern or r"^(?P<g1>.+)$"})
        labels = [g[1] for g in p.group_rows]
        two = "g2" in (p.group_pattern or "")
        parts1 = list(dict.fromkeys(re.split(r"\s*\|\s*", l)[0] for l in labels))
        sources["g1"] = SourceInfo("g1", "group", "text", 0.0, parts1[:8], len(parts1), origin="group header rows")
        distinct_all["g1"] = parts1
        if two:
            parts2 = list(dict.fromkeys(re.split(r"\s*\|\s*", l, maxsplit=1)[1] for l in labels if "|" in l))
            sources["g2"] = SourceInfo("g2", "group", "text", 0.0, parts2[:8], len(parts2), origin="group header rows")
            distinct_all["g2"] = parts2
    text_ids = [n for h, n in neutral.items() if prof_by[h].dtype in ("text", "id_text") or ID_LIKE.search(n)]
    shape[0:0] = norm_ops
    head_marks = _block_heading_marks(data, neutral, prof_by, flat) if len(plain) >= 3 else {}
    if head_marks:
        first = next(iter(neutral.values()))
        shape.append({"op": "block_heading", "col": first, "out": "b1", "marks": head_marks})
        labels = list(dict.fromkeys(str(v).strip() for v in _block_labels(data, neutral, head_marks)))
        sources["b1"] = SourceInfo("b1", "group", "text", 0.0, labels[:8], len(labels), origin="block heading rows")
        distinct_all["b1"] = labels
    if text_ids:
        shape.insert(0, {"op": "strip", "cols": text_ids})

    carry = [n for n in neutral.values()] + list(extra_cols) + [g for g in ("g1", "g2", "b1") if g in sources]
    dims: dict[str, list[str]] = {}
    layout, pattern, value_source = "tidy", None, None
    wide_numeric: dict[str, str] = {}

    if grid:                                           # multi-level header: top|sub
        layout = "grid"
        pattern = r"^([^|]+)\|([\s\S]+)$"
        ws = lambda x: re.sub(r"\s+", " ", x).strip()      # noqa: E731
        tops = list(dict.fromkeys(ws(g.split("|", 1)[0]) for g in grid))
        subs = list(dict.fromkeys(ws(g.split("|", 1)[1]) for g in grid))
        dims = {"dim1": tops, "dim2": subs}
        shape.append({"op": "melt_families", "families": [{"pattern": pattern, "value": "cell_value", "type": "text"}],
                      "id": carry, "index": ["dim1", "dim2"], "cell_col": "source_cell"})
        sources["dim1"] = SourceInfo("dim1", "dim", "text", 0.0, tops[:8], len(tops), origin="upper header level")
        sources["dim2"] = SourceInfo("dim2", "dim", "text", 0.0, subs[:8], len(subs), origin="lower header level")
        sources["cell_value"] = SourceInfo("cell_value", "value", "text", 0.0, [], 0, origin="grid cells")
        value_source = "cell_value"
        for h in grid:                                 # a text / date column inside the band is a label, not a value column
            if prof_by.get(h) is not None and prof_by[h].dtype in ("text", "date"):
                n0 = snake(h.split("|", 1)[1])
                n, k = n0, 2
                while n in sources or n in ("dim1", "dim2", "g1", "g2", "cell_value", "source_cell"):
                    n, k = f"{n0}_{k}", k + 1
                d, nd = _distinct(data[h])
                sources[n] = SourceInfo(n, "column", prof_by[h].dtype, prof_by[h].null_rate, d[:8], nd, origin=ws(h.split("|", 1)[1]))
                distinct_all[n] = d
                wide_numeric[n] = h
    else:
        numeric = [h for h in plain if prof_by[h].dtype in ("int", "decimal") and prof_by[h].null_rate < 0.5
                   and not ID_LIKE.search(neutral[h])]
        if len(numeric) >= 2 and not flat:             # wide value columns: header labels are a dimension
            layout = "wide"
            for h in numeric:
                cmap.pop(h, None)
            pattern = "^(" + "|".join(re.escape(h) for h in numeric) + ")$"
            dims = {"dim1": numeric}
            carry = [n for h, n in neutral.items() if h not in numeric] + list(extra_cols) + [g for g in ("g1", "g2", "b1") if g in sources]
            shape.append({"op": "melt_families", "families": [{"pattern": pattern, "value": "cell_value", "type": "text"}],
                          "id": carry, "index": ["dim1"], "cell_col": "source_cell"})
            for h in numeric:                                           # these columns keep their raw header until the melt
                if neutral[h] in head_marks:
                    head_marks[h] = head_marks.pop(neutral[h])
            wide_numeric = {neutral[h]: h for h in numeric}              # still addressable: a year or a code is a field, not a value
            sources["dim1"] = SourceInfo("dim1", "dim", "text", 0.0, [x.replace("\n", " ") for x in numeric][:8], len(numeric),
                                         origin="value column headers")
            sources["cell_value"] = SourceInfo("cell_value", "value", "text", 0.0, [], 0, origin="melted cells")
            value_source = "cell_value"
        elif len(numeric) == 1 and not flat:
            n = neutral[numeric[0]]
            shape.append({"op": "cell_ref", "header": numeric[0], "out": "source_cell"})
            sources["source_cell"] = SourceInfo("source_cell", "cell", "text", 0.0, [], 0, origin="A1 address")
            value_source = n
    sources.setdefault("source_cell", SourceInfo("source_cell", "cell", "text", 0.0, [], 0, origin="A1 address"))
    sources["_src_sheet"] = SourceInfo("_src_sheet", "cell", "text", 0.0, [p.name], 1, origin="sheet name")
    sources["_src_row"] = SourceInfo("_src_row", "cell", "text", 0.0, [], 0, origin="row number")
    if flat:
        sources.pop("source_cell", None)
    for dn, vals in dims.items():
        distinct_all[dn] = vals
    tokens: list[str] = []
    if layout == "wide":
        cells = pd.concat([data[h] for h in numeric_cols_for_tokens(p, neutral, wide_numeric)]).dropna().map(lambda v: str(v).strip())             if wide_numeric else pd.Series([], dtype=object)
        tokens = sorted({v for v in cells if not re.fullmatch(r"-?\d+([.,]\d+)?([eE][-+]?\d+)?", v)})[:6]
    if grid:
        cells = pd.concat([data[g] for g in grid]).dropna().map(lambda v: str(v).strip())
        tokens = sorted({v for v in cells if not re.fullmatch(r"-?\d+([.,]\d+)?([eE][-+]?\d+)?", v)})[:6]
    units = [u[1] for u in p.unit_rows] + [g[1] for g in p.group_rows]
    return Frame(p.name, locate, cmap, shape, sources, p.facts, layout, dims, pattern, value_source,
                 distinct_all=distinct_all, unit_texts=units, value_tokens=tokens,
                 data=pd.concat([data[[h for h in neutral]].rename(columns=neutral), pd.DataFrame(extra_cols, index=data.index)], axis=1),
                 grid_cols=list(grid),
                 first_row=int(data.index[0]) + 1 if len(data) else 0,
                 wide_numeric=wide_numeric)
