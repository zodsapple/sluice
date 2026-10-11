"""Rule bindings for RECORD tables (one row per record, one column per target field): which source column is which field.

Evidence, in order of strength - nothing here knows a domain:
  * the one example row of the template: its value occurs in a source column (after normalising dates and numbers)
  * the field name and the header share words (exclusive: a column serves one field)
  * type compatibility (a date field needs a date column, a decimal field a numeric one)
What is left stays a question; a string field with an example value and no column falls back to that value as a constant,
flagged as an assumption.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

from sluice.infer.bindings import Binding
from sluice.infer.frame import Frame

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y%m%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"]
STOP = {"the", "of", "and", "raw", "no"}
LINEAGE = {"source_sheet": "_src_sheet", "source_row": "_src_row", "source_record": "_src_row"}


def tokens(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(s))
    out = set()
    for t in re.split(r"[^a-z0-9]+", s.lower()):
        if t and t not in STOP:
            out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


def name_score(field: str, header: str) -> float:
    f, h = tokens(field), tokens(header)
    if not f or not h:
        return 0.0
    both = len(f & h)
    return 0.0 if both == 0 else (0.6 * both / len(f) + 0.4 * both / len(h))


def date_format(values: list[Any]) -> str | None:
    """The format that reads every value; 'iso' when the cells already are dates. Day-first wins an unresolvable d/m tie."""
    vals = [v for v in values if v is not None and str(v).strip() != "" and not (isinstance(v, float) and pd.isna(v))]
    if not vals:
        return None
    if all(hasattr(v, "strftime") for v in vals):
        return "iso"
    ok = []
    for fmt in DATE_FORMATS:
        try:
            [datetime.strptime(str(v).strip(), fmt) for v in vals]
            ok.append(fmt)
        except ValueError:
            continue
    return ok[0] if ok else None


def to_iso(v: Any, fmt: str | None) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    if fmt and fmt != "iso":
        try:
            return datetime.strptime(str(v).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return str(v)


def _num(x: Any) -> float | None:
    t = re.sub(r"[£\$€\s]", "", str(x)).replace(",", "")             # "£52,733" -> 52733 ; "(0,300)" is not a number
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def numeric_text(s: pd.Series) -> bool:
    vals = s.dropna().head(300)
    return len(vals) > 0 and all(_num(v) is not None for v in vals) and vals.astype(str).str.contains(r"\d").all()


def has_value(series: pd.Series, example: Any, typ: str, fmt: str | None) -> bool:
    ex = str(example).strip()
    if ex == "":
        return False
    vals = series.dropna()
    if typ == "date":
        return any(to_iso(v, fmt) == ex for v in vals.head(2000))
    if typ in ("decimal", "integer"):
        e = _num(ex)
        return e is not None and any((n := _num(v)) is not None and abs(n - e) < 1e-6 for v in vals.head(2000))
    return any(str(v).strip() == ex for v in vals.head(2000))


def record_bindings(frame: Frame, target, params: dict[str, str], example: dict[str, Any]) -> list[Binding]:
    out: list[Binding] = []
    have: set[str] = set()

    def add(b: Binding) -> None:
        if b.target and b.target not in have:
            out.append(b)
            have.add(b.target)

    for k, v in params.items():
        if k in target.fields:
            add(Binding("constant", k, {"value": v}, 1.0, "human", ["parameter"]))
    for tname, src in LINEAGE.items():
        if tname not in target.fields or src not in frame.sources:
            continue
        ex = str(example.get(tname, "")).strip()
        if src == "_src_row" and re.fullmatch(r"\d+", ex) and frame.first_row and int(ex) != frame.first_row:
            add(Binding("scale", tname, {"source": src, "factor": 1, "offset": int(ex) - frame.first_row}, 0.7, "rule",
                        ["row numbering differs from the physical row: taken from the example row"]))
        else:
            add(Binding("column", tname, {"source": src}, 1.0, "rule", ["lineage"]))
    data = frame.data if frame.data is not None else pd.DataFrame()
    cols = {n: s for n, s in frame.sources.items() if s.kind == "column" and n in data.columns and not (frame.layout == "wide" and n in frame.wide_numeric)}
    fmts = {n: (date_format(list(data[n].dropna().head(5000))) if s.dtype == "date" else None) for n, s in cols.items()}
    cands: list[tuple[float, str, str, list[str], list[str]]] = []
    src_row = str(example.get("source_row", example.get("source_record", ""))).strip()       # the row the example was taken from
    pos = int(src_row) - frame.first_row if re.fullmatch(r"\d+", src_row) and frame.first_row else 0
    for fname, f in target.fields.items():
        if fname in have or fname in LINEAGE or "occurrence" in tokens(fname):
            continue
        ex = example.get(fname, "")
        row = pos if 0 <= pos < len(data) else None
        holders = [n for n in cols if has_value(data[n].iloc[[row]] if row is not None else data[n], ex, f.type, fmts[n])]
        for n, s in cols.items():
            if f.type == "date" and s.dtype != "date":
                continue
            if f.type in ("decimal", "integer") and s.dtype not in ("int", "decimal") and not numeric_text(data[n]):
                continue
            ns = name_score(fname, s.origin or n)
            vs = 1.0 if n in holders and (len(holders) == 1 or ns >= 0.45) else 0.0      # a value found in many columns proves nothing
            if row is not None and n in holders and len(holders) > 1 and ns < 0.45 and example.get("source_row") is not None:
                vs = 0.5                                                                    # same cell as the example row, but not alone
            score = 0.6 * ns + 0.6 * vs
            if ns >= 0.45 or vs:
                ev = [f"header {s.origin!r} ~ {fname}" if ns else "", "the example value occurs in it" if vs else ""]
                cands.append((score + (0.001 if ns else 0), fname, n, [e for e in ev if e], list(holders)))
    used_cols: set[str] = set()
    for score, fname, n, ev, held in sorted(cands, key=lambda c: c[:4], reverse=True):
        if fname in have or n in used_cols:
            continue
        used_cols.add(n)
        args: dict[str, Any] = {"source": n}
        if target.fields[fname].type == "date":
            args.update(as_date=True, date_format=fmts[n] or "iso")
        sole = held == [n]                                    # the example value occurs in this column and nowhere else
        by_name = name_score(fname, cols[n].origin or n)
        conf = 0.35 + 0.55 * by_name + (0.3 if sole else 0.1 if score > 0 and n in held else 0.0)
        add(Binding("column", fname, args, round(min(0.95, conf), 2), "rule", ev))
    if frame.layout in ("wide", "grid") and "cell_value" in frame.sources:           # periods across the header, unpivoted by the frame
        yearish = next((d for d in ("dim2", "dim1") if frame.dims.get(d) and all(re.fullmatch(r"(19|20)\d{2}", str(v).strip()) for v in frame.dims[d])), None)
        for fname, f in target.fields.items():
            if fname not in have and yearish and tokens(fname) & {"year", "period"}:
                add(Binding("column", fname, {"source": yearish}, 0.8, "rule", [f"the header values {frame.dims[yearish][:3]} are years"]))
        left = [fn for fn, f in target.fields.items() if fn not in have and f.type in ("decimal", "integer") and fn not in LINEAGE
                and "occurrence" not in tokens(fn)]
        if len(left) == 1:
            add(Binding("column", left[0], {"source": "cell_value"}, 0.6, "rule", ["the only numeric field left takes the unpivoted cell values"]))
    sheet_nums = re.findall(r"\d+", frame.sheet)
    ex_sheet_nums = re.findall(r"\d+", str(example.get("source_sheet", "")))
    for fname, f in target.fields.items():                # a number printed in the sheet name (Day 3 -> 3)
        ex = str(example.get(fname, "")).strip()
        if fname in have or fname in LINEAGE or ex not in ex_sheet_nums or f.type not in ("string", "decimal", "integer"):
            continue
        k = ex_sheet_nums.index(ex)
        if k < len(sheet_nums):
            add(Binding("column", fname, {"source": "_src_sheet", "mapping": {frame.sheet: sheet_nums[k]}}, 0.7, "rule",
                        [f"the number in the sheet name {frame.sheet!r}, as in the example row"]))
    for fname, f in target.fields.items():              # example value as a constant: an assumption, strings only
        ex = str(example.get(fname, "")).strip()
        if fname in have or fname in LINEAGE or "occurrence" in tokens(fname):
            continue
        if f.type == "string" and ex and any(has_value(data[n], ex, "string", None) and cols[n].n_distinct > 1 for n in cols):
            continue                                       # the value occurs in a varying column: probably that column, not a constant
        if f.type == "string" and ex and not re.fullmatch(r"-?[\d.,]+", ex):          # a bare number is rarely a constant
            add(Binding("constant", fname, {"value": ex}, 0.5, "rule",
                        ["no column matches; value taken from the example row of the template"]))
    sig = [t for t in target.fields if t in have and t not in LINEAGE and t not in params]
    for fname in target.fields:
        if "occurrence" in tokens(fname) and fname not in have:
            add(Binding("occurrence", fname, {"by": sig}, 0.8, "rule", ["counts equal records in source order"]))
    return out
