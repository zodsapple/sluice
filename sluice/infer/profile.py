"""Deterministic file profiler: what is in this file and how is it laid out? No spec, no template needed."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from sluice.engine.locate import _blank, flatten_header, read_workbook
from sluice.spec.models import LocateSpec

TOTAL_RX = re.compile(r"(?i)\b(sub)?total\b|^sum\b|^grand\b")
DATE_PATTERNS = {
    "%Y-%m-%d": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "%d.%m.%Y": re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
    "%d/%m/%Y": re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    "%Y%m%d": re.compile(r"^\d{8}$"),
    "%Y-%m-%d %H:%M:%S": re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"),
    "%Y/%m/%d %H:%M:%S": re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$"),
    "%Y/%m/%d": re.compile(r"^\d{4}/\d{2}/\d{2}$"),
}


@dataclass
class ColumnProfile:
    name: str
    dtype: str                    # int | decimal | date | text | id_text | empty
    null_rate: float
    unique_ratio: float
    samples: list[str]
    shape: str = ""               # value shape, digits->9 letters->a (run-length compressed)
    date_formats: list[str] = field(default_factory=list)
    non_date_tokens: list[str] = field(default_factory=list)   # e.g. OPEN inside a date column
    far_future: bool = False                                     # 9999-.. style sentinel dates
    leading_zero: bool = False


@dataclass
class Family:
    pattern: str                  # regex with ONE capture group (the index)
    prefix: str
    members: list[str]
    index_values: list[str]
    index_kind: str               # int | month | text


@dataclass
class SheetProfile:
    name: str
    nrows: int
    header_rows: list[int]
    data_start: int
    columns: list[ColumnProfile]
    families: list[Family]
    skip_labels: list[tuple[int, str, str]]          # (row_idx, label, kind) total / note / repeated_header
    group_rows: list[tuple[int, str]]
    group_pattern: str | None
    banner_rows: list[int]
    flat_names: list[str]
    data: pd.DataFrame = field(repr=False, default=None)    # cleaned data rows (group / skip rows removed)
    unit_rows: list[tuple[int, str]] = field(default_factory=list)      # text rows between header and data ("en milliers")
    fill_cols: list[str] = field(default_factory=list)                  # block labels given on the first row of a block only
    grid_cols: list[str] = field(default_factory=list)                  # value columns of a multi-level header ("top|sub")
    facts: dict[str, str] = field(default_factory=dict)                 # banner cells above the header: {"A2": "title ..."}
    synthetic: list[str] | None = None                                  # no header row in the file: names given by a person


@dataclass
class FileProfile:
    path: str
    sheets: dict[str, SheetProfile]


def shape_of(v: Any) -> str:
    s = re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", str(v).strip()))
    return re.sub(r"(.)\1+", r"\1+", s)[:30]


def _only_numbers_after_label(cells: list) -> bool:
    """A footer such as 'Total | 6831.96 | 446.44 | 7278.40': one text label, everything else a number."""
    vals = [v for v in cells if not _blank(v)]
    if len(vals) < 2:
        return False
    rest = vals[1:]
    return all(isinstance(v, (int, float)) or re.fullmatch(r"-?[\d.,]+", str(v).strip() or "x") for v in rest)


def coarse_shape(v: Any) -> str:
    """Shape that ignores word length and spaces: 'd003 | Human Resources' == 'd001 | Marketing'."""
    t = re.sub(r"\d+", "9", str(v).strip())
    return re.sub(r"a+", "a", re.sub(r"[A-Za-z][A-Za-z &/\-]*", "a", t))


def _s(v: Any) -> str:
    return "" if _blank(v) else str(v).strip()


def _is_text_cell(v: Any) -> bool:
    """A header-like label: a non-blank string that is not a plain number (month labels like 2026-06 count)."""
    if _blank(v) or not isinstance(v, str):
        return False
    try:
        float(v.replace(",", ""))
        return False
    except ValueError:
        return True


def _nonblank(row: list[Any]) -> int:
    return sum(0 if _blank(v) else 1 for v in row)


def _numlike(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False


_VALUE_STR = re.compile(r"^([\d.,\-/:\s]*\d[\d.,\-/:\s]*|-?\d+(\.\d+)?\s*[a-z]{1,3}|:\s*[a-z]{0,3})$")        # numbers, dates, "10.5 b" (flag), ":" (missing)


def _data_like(row: list[Any], is_csv: bool = False) -> bool:
    """A body row: at least two value-like cells (numbers, dates, or ids / dates written as digit strings)."""
    cells = [v for v in row if not _blank(v)]
    if len(cells) < 2:
        return False
    if all(re.fullmatch(r"(19|20)\d{2}", str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()) for v in cells):
        return False                                       # a row of years is a header level ("2020 2021 2022"), not a body row
    years = sum(1 for v in cells if _is_year(v))
    drop_years = years >= 3                                    # three or more years in one row: the header of a period-wide table
    vals = sum(1 for v in cells if not (drop_years and _is_year(v)) and
               ((isinstance(v, (int, float, datetime, date)) and not isinstance(v, bool))
                or (isinstance(v, str) and bool(_VALUE_STR.match(v.strip())))))
    return vals >= 2 and vals / len(cells) >= 0.25


def _is_year(v: Any) -> bool:
    return not _blank(v) and bool(re.fullmatch(r"(19|20)\d{2}", str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()))


def _fills_blank_below(upper: list[Any], lower: list[Any]) -> bool:
    return any(not _blank(lower[j]) and _blank(upper[j]) for j in range(1, min(len(upper), len(lower))))      # column A holds row labels


def detect_header(raw: pd.DataFrame, scan: int = 30, is_csv: bool = False) -> tuple[list[int], list[int]]:
    """Return (header_rows, banner_rows).

    Start from the first body row (numeric-looking, two rows in a row); the header is the block of multi-cell text rows
    right above it. Single-cell rows (units, group labels) and unit rows that only repeat one label per span are skipped;
    a lower level row counts when it fills cells its upper row leaves blank (merged group headers)."""
    n = len(raw)
    rows_ = [raw.iloc[i].tolist() for i in range(min(n, scan + 5))]
    width = max((_nonblank(r_) for r_ in rows_ if _data_like(r_, is_csv)), default=0)          # the widest body row
    start = next((r for r in range(len(rows_) - 1)
                  if _data_like(rows_[r], is_csv) and _nonblank(rows_[r]) >= 0.25 * width        # a two-cell banner with a date is not a body
                  and (_data_like(rows_[r + 1], is_csv) or _nonblank(rows_[r + 1]) <= 1)), None)
    if start is not None and start <= scan:
        r = start - 1
        while r >= 0 and _nonblank(rows_[r]) <= 1:
            r -= 1
        header: list[int] = []
        while r >= 0 and _nonblank(rows_[r]) >= 2 and sum(_is_text_cell(v) or _is_year(v) for v in rows_[r]) >= 0.5 * _nonblank(rows_[r]):
            below = rows_[header[0]] if header else rows_[start]
            if _repeats_below(rows_[r], below):
                break                        # the same labels again in the same columns: that is data, not a header level
            upper_blank_fill = r >= 1 and _fills_blank_below(rows_[r - 1], rows_[r])
            lower = rows_[header[0]] if header else None
            is_level = _nonblank(rows_[r]) >= 0.5 * _nonblank(rows_[start]) or upper_blank_fill or (lower is not None and _fills_blank_below(rows_[r], lower))
            if not is_level:
                r -= 1                       # a unit row ("en milliers"): not part of the header
                continue
            header.insert(0, r)
            r -= 1
        if header:
            return header, [i for i in range(header[0]) if _nonblank(rows_[i]) >= 1]
    return _detect_header_simple(raw, scan)


def _repeats_below(row: list, below: list) -> bool:
    """Most non-blank cells equal the cell under them (a repeated entity / department column): a body row."""
    pairs = [(_s(a), _s(b)) for a, b in zip(row, below) if not _blank(a)]
    return len(pairs) >= 2 and sum(1 for a, b in pairs if a == b) >= 0.3 * len(pairs)


def _detect_header_simple(raw: pd.DataFrame, scan: int = 30) -> tuple[list[int], list[int]]:
    """Fallback for files without numeric bodies: first row that is mostly distinct text."""
    width = max((_nonblank(raw.iloc[i].tolist()) for i in range(min(scan, len(raw)))), default=0)
    for i in range(min(scan, len(raw))):
        row = raw.iloc[i].tolist()
        texts = [_s(v) for v in row if _is_text_cell(v)]
        if len(texts) >= max(2, int(0.6 * width)) and len(set(texts)) == len(texts):
            rows = [i]
            if i + 1 < len(raw):
                nxt = raw.iloc[i + 1].tolist()
                if _fills_blank_below(row, nxt) and all(_is_text_cell(v) for v in nxt if not _blank(v)) and _nonblank(nxt) >= 1:
                    rows.append(i + 1)
            return rows, [r for r in range(i) if _nonblank(raw.iloc[r].tolist()) >= 1]
    return [0], []


def _column_profile(name: str, s: pd.Series) -> ColumnProfile:
    vals = s.dropna()
    n = len(s)
    if vals.empty:
        return ColumnProfile(name, "empty", 1.0, 0.0, [])
    texts = vals.map(lambda v: str(v).strip())
    samples = list(texts.head(3))
    base = dict(name=name, null_rate=round(1 - len(vals) / max(n, 1), 4), unique_ratio=round(texts.nunique() / len(vals), 4),
                samples=samples, shape=Counter(texts.head(500).map(shape_of)).most_common(1)[0][0])
    dt_objs = vals.map(lambda v: isinstance(v, (datetime, date, pd.Timestamp)))
    fmt_hits: Counter = Counter()
    other: Counter = Counter()
    far = False
    for v, is_dt in zip(texts.head(2000), dt_objs.head(2000)):
        if is_dt:
            fmt_hits["excel_date"] += 1
            continue
        for fmt, rx in DATE_PATTERNS.items():
            if rx.match(v):
                fmt_hits[fmt] += 1
                far = far or v.startswith("9999")
                break
        else:
            other[v] += 1
    dated = sum(fmt_hits.values())
    if dated >= 0.2 * min(len(vals), 2000) and (fmt_hits.keys() - {"%Y%m%d"}) and len(other) <= 3:
        return ColumnProfile(**base, dtype="date", date_formats=[f for f in fmt_hits if f != "excel_date"],
                             non_date_tokens=[t for t, _ in other.most_common(5)], far_future=far)
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals.head(2000)) or texts.head(2000).str.fullmatch(r"-?\d+").all():
        zero = bool(texts.str.match(r"^0\d+").any())
        long = texts.str.len().max() > 15
        return ColumnProfile(**base, dtype="id_text" if (zero or long) else "int", leading_zero=zero)
    if texts.head(2000).str.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?").all():
        return ColumnProfile(**base, dtype="decimal")
    isnum = texts.str.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")
    marks = texts[~isnum]
    if (isnum.mean() >= 0.3 and marks.nunique() <= 3 and marks.str.len().max() <= 4) or (isnum.mean() >= 0.8 and marks.nunique() <= 3):
        return ColumnProfile(**base, dtype="decimal", non_date_tokens=[t for t, _ in Counter(marks).most_common(3)])   # numbers with markers (s, X)
    flagged = texts.str.fullmatch(r"-?\d+(\.\d+)?(\s*[a-z]{1,3})?|:\s*[a-z]{0,3}")
    if flagged.mean() >= 0.8 and texts.str.contains(r"\d").any() and texts.str.contains(r"^:|\s[a-z]{1,3}$").any():
        return ColumnProfile(**base, dtype="decimal", non_date_tokens=[":"])                   # statistical flags next to the numbers
    return ColumnProfile(**base, dtype="text")


def _families(names: list[str]) -> list[Family]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for n in names:
        m = re.search(r"\d{4}-\d{2}", n)
        if m:
            key = n[: m.start()] + "{M}" + n[m.end():]
            idx = m.group(0)
        else:
            ms = list(re.finditer(r"\d+", n))
            if not ms or n.startswith("Unnamed"):
                continue
            m = ms[-1]
            key, idx = n[: m.start()] + "{N}" + n[m.end():], m.group(0)
        groups.setdefault(key, []).append((n, idx))
    fams = []
    for key, mem in groups.items():
        is_month = "{M}" in key
        if len(mem) < (2 if is_month else 6):
            continue
        token = "{M}" if is_month else "{N}"
        left, right = key.split(token)
        pat = "^" + re.escape(left) + (r"(\d{4}-\d{2})" if is_month else r"(\d+)") + re.escape(right) + "$"
        fams.append(Family(pat, (left + right).strip(" |") or key, [m for m, _ in mem], [i for _, i in mem],
                           "month" if is_month else "int"))
    return fams


def profile_sheet(name: str, raw: pd.DataFrame, is_csv: bool = False, hint: dict[str, Any] | None = None) -> SheetProfile:
    from openpyxl.utils import get_column_letter

    synthetic = None
    if hint and hint.get("synthetic_header") is not None:          # a person says: no header row, these names, data starts here
        ds = int(hint["data_start"])
        synthetic = list(hint["synthetic_header"])
        raw = raw.copy()
        raw.iloc[ds - 1] = [synthetic[i] if i < len(synthetic) else None for i in range(raw.shape[1])]
        hrows, banners = [ds - 1], [i for i in range(ds - 1) if _nonblank(raw.iloc[i].tolist()) >= 1]
    elif hint and hint.get("header_rows"):
        hrows = list(hint["header_rows"])
        banners = [i for i in range(min(hrows)) if _nonblank(raw.iloc[i].tolist()) >= 1]
    else:
        hrows, banners = detect_header(raw, is_csv=is_csv)
    flat = flatten_header(raw, hrows)
    hend = max(hrows)
    grid = raw.iloc[hend + 1:].copy()
    labels = grid.apply(lambda r: next((_s(v) for v in r if not _blank(v)), ""), axis=1)
    nb = grid.apply(lambda r: _nonblank(r.tolist()), axis=1)
    data_like = grid.apply(lambda r: _data_like(r.tolist(), is_csv), axis=1)
    first_body = next((i for i in grid.index if data_like[i]), None)

    skip: list[tuple[int, str, str]] = []
    unit_rows: list[tuple[int, str]] = []
    group: list[tuple[int, str]] = []
    drop_idx = set()
    head_texts = {n for r in hrows for n in map(_s, raw.iloc[r].tolist()) if n}
    cand_group = [(i, labels[i]) for i in grid.index if nb[i] == 1 and labels[i] and not TOTAL_RX.search(labels[i])]
    shapes = Counter(coarse_shape(l) for _, l in cand_group)
    group_shape = next((s for s, c in shapes.most_common() if c >= 2 and "|" in s), None)

    def run_after(i: int) -> int:
        """Body rows following a single-cell row before the next single-cell row."""
        n_ = 0
        for j in grid.index[grid.index > i]:
            if nb[j] == 0:
                continue
            if nb[j] == 1:
                break
            n_ += 1
        return n_

    for i in grid.index:
        lab, count = labels[i], nb[i]
        if count == 0:
            drop_idx.add(i)
        elif lab in head_texts and count <= len(head_texts):
            skip.append((i, lab, "repeated_header")), drop_idx.add(i)
        elif TOTAL_RX.search(lab) and (count <= 3 or (i >= grid.index[-3] and _only_numbers_after_label(grid.loc[i].tolist()))):
            skip.append((i, lab, "total")), drop_idx.add(i)
        elif first_body is not None and i < first_body and 2 <= count < 0.7 * len(flat) and not data_like[i]:
            unit_rows.append((i, " | ".join(_s(v) for v in grid.loc[i].tolist() if not _blank(v)))), skip.append((i, lab, "unit")), drop_idx.add(i)
        elif count == 1 and _blank(grid.loc[i].iloc[0]) and first_body is not None and i < first_body:
            unit_rows.append((i, lab)), skip.append((i, lab, "unit")), drop_idx.add(i)      # a unit printed above the numbers ("in 1 000")
        elif count == 1 and group_shape and coarse_shape(lab) == group_shape:
            group.append((i, lab)), drop_idx.add(i)
        elif count == 1 and run_after(i) >= 2 and len(lab) <= 80 and not _blank(grid.loc[i].iloc[0]):
            group.append((i, lab)), drop_idx.add(i)
        elif count == 1 and (run_after(i) == 0 or (len(lab) > 25 and " " in lab)):
            skip.append((i, lab, "note")), drop_idx.add(i)             # trailing marker ("Ende der Tabelle.") or long note
    # a group row directly above the header belongs to the first block
    if hrows[0] > 0 and group_shape and nb.size and _nonblank(raw.iloc[hrows[0] - 1].tolist()) == 1 \
            and coarse_shape(_s(next(v for v in raw.iloc[hrows[0] - 1] if not _blank(v)))) == group_shape:
        group.insert(0, (hrows[0] - 1, _s(next(v for v in raw.iloc[hrows[0] - 1] if not _blank(v)))))
    group.sort()
    data_start = group[0][0] if group and group[0][0] < hrows[0] else hend + 1
    gpat = None
    if group:
        gpat = r"^(?P<g1>[^|]+?)\s*\|\s*(?P<g2>.+)$" if any("|" in g for _, g in group) else r"^(?P<g1>.+)$"

    seen_names: dict[str, int] = {}
    flat = [n if (seen_names.update({n: seen_names.get(n, 0) + 1}) or seen_names[n] == 1) else f"{n}.{seen_names[n] - 1}" for n in flat]
    data = grid.drop(index=list(drop_idx))
    data.columns = flat
    data = data.apply(lambda c: c.map(lambda v: pd.NA if _blank(v) else v))      # CSV blanks are empty strings
    data = data[[c for c in flat if not (c.startswith("Unnamed") and data[c].isna().all())]]
    cols = [_column_profile(c, data[c]) for c in data.columns]
    fills = [c.name for c in cols if c.dtype in ("text", "id_text") and c.null_rate >= 0.6 and len(data) >= 8
             and not pd.isna(data[c.name].iloc[0]) and not c.name.startswith("Unnamed")] if len(data) else []
    dtype_of = {c.name: c.dtype for c in cols}
    gcols = [c for c in data.columns if "|" in c and dtype_of.get(c) != "date"] if len(hrows) > 1 else []      # a date column is a label, not a value column
    facts = {}
    for r in banners:
        for j, v in enumerate(raw.iloc[r].tolist()):
            if not _blank(v):
                facts[f"{get_column_letter(j + 1)}{r + 1}"] = _s(v)[:200]
    return SheetProfile(name, len(raw), hrows, data_start, cols, _families(list(data.columns)), skip, group, gpat, banners,
                        list(data.columns), data, unit_rows, fills, gcols, facts, synthetic)


def profile_file(path: str | Path, structure: dict[str, dict[str, Any]] | None = None) -> FileProfile:
    path = Path(path)
    sheets = read_workbook(path, LocateSpec())
    csv = path.suffix.lower() in (".csv", ".tsv", ".tab", ".txt")
    return FileProfile(str(path), {n: profile_sheet(n, raw, csv, (structure or {}).get(n)) for n, raw in sheets.items() if len(raw)})


def duplicate_diagnostics(df: pd.DataFrame, key: list[str]) -> dict[str, Any]:
    """What a human needs to know before choosing a dedup policy."""
    payload = [c for c in df.columns if c not in ("_src_row", "_src_sheet")]
    exact = int(df.duplicated(subset=payload).sum())
    clash = df[df.duplicated(subset=key, keep=False)] if key else df.iloc[0:0]
    groups = int(clash.groupby(key, dropna=False).ngroups) if len(clash) else 0
    differing = Counter()
    if groups:
        for _, g in list(clash.groupby(key, dropna=False))[:200]:
            for c in payload:
                if c not in key and g[c].astype(str).nunique() > 1:
                    differing[c] += 1
    return {"exact_duplicate_rows": exact, "key_clash_groups": groups, "columns_that_differ": dict(differing.most_common(5))}
