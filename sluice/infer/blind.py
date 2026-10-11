"""Source-blind drafting for a shared target schema: structure -> rule bindings -> open questions -> spec.

    target schema (fields, enums)  +  a few human parameters  +  an unseen workbook
        -> Frame per sheet (layout, neutral sources)          [deterministic, no business meaning]
        -> rule bindings (what can be decided from evidence)   [deterministic]
        -> open questions (what cannot)                        [answered by a human / an LLM in the SAME vocabulary]
        -> spec -> run
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sluice.infer.bindings import Binding, BindingError, TargetField, validate
from sluice.infer.columns import record_bindings
from sluice.infer.compile import compile_spec
from sluice.infer.frame import Frame, build_frame
from sluice.infer.profile import profile_file

DESCRIPTIONS = {
    "source_id": "identifier of the publication / dataset the rows come from",
    "country": "ISO alpha-2 country of the publisher",
    "reference_date": "date the observation refers to (ISO); empty for annual work volumes",
    "period_start": "first day of the observed period (ISO)",
    "period_end": "last day of the observed period (ISO)",
    "period_basis": "whether the value is a point in time or an annual work volume",
    "population_scope": "who / what is counted (the reporting population and coverage)",
    "breakdown_dimension": "which classification the category field belongs to",
    "category": "label of the category (as printed in the source)",
    "category_code": "code of the category (text, keep leading zeros)",
    "category_level": "level of the category in its hierarchy (total, major group, sub group...)",
    "employment_area": "employer sector / area, if the source has one (else empty)",
    "employment_relationship": "employment relationship type, if the source has one (else empty)",
    "sex": "all / male / female as defined by the source",
    "measure": "what is measured: headcount or full-time equivalents",
    "value": "the observed number in base units (not thousands)",
    "unit": "unit of value: persons or fte",
    "value_status": "reported number, or not applicable when the source prints a marker instead",
    "entity_level": "kind of organisational entity the row describes (country, region ...), if any",
    "entity_code": "code of that entity, if any",
    "entity_name": "name of that entity, if any",
    "aggregation_level": "how the row aggregates (national total, regional total, group total ...)",
    "source_value": "the value exactly as printed in the source cell",
    "source_unit": "unit text printed by the source (e.g. thousands)",
    "source_scale": "factor from printed value to base units",
    "source_sheet": "sheet the value comes from",
    "source_cell": "A1 address of the value cell",
}
DATE_FIELDS = {"reference_date", "period_start", "period_end"}
LEXICONS = {"sex": {"male": ["male", "males", "m", "men", "man", "männer", "mann", "männlich", "homme", "hommes", "masculin"],
                    "female": ["female", "females", "f", "women", "woman", "frauen", "frau", "weiblich", "femme", "femmes", "féminin"],
                    "all": ["all", "total", "insgesamt", "zusammen", "ensemble", "all genders", "both"]}}
CONVENTIONS = [
    "A dimension the source does not provide stays empty; never infer it from another source or country.",
    "Point-in-time observations: reference_date = period_start = period_end. Annual work volumes: reference_date empty, "
    "period_start/period_end cover the whole year.",
]
THOUSANDS = re.compile(r"(?i)(in\s+1\s?000|milliers|thousands?|tsd|1\s?000\s+personen)")


@dataclass
class Target:
    fields: dict[str, TargetField]
    notes: list[str]
    mode: str = "stats"                  # stats: dims x value | record: one row per record, one column per field
    example: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_schema(path: str | Path) -> "Target":
        js = json.loads(Path(path).read_text(encoding="utf-8"))
        enums = {"measure": js.get("measure_values", []), "unit": js.get("unit_values", []),
                 "value_status": js.get("value_status_values", []), "period_basis": js.get("period_basis_values", [])}
        required = set(js.get("required_for_checker", []))
        flds = {}
        for name in js["fields"]:
            if name in ("source_value", "source_unit", "source_scale", "source_number_format"):
                continue                                    # audit-only columns are not asked for
            typ = "date" if name in DATE_FIELDS else ("decimal" if name == "value" else "string")
            flds[name] = TargetField(name, typ, enums.get(name, []), name in required, DESCRIPTIONS.get(name, ""),
                                     LEXICONS.get(name, {}))
        return Target(flds, list(CONVENTIONS))


@dataclass
class Question:
    id: str
    frame: str                       # sheet
    targets: list[str]
    kind: str                        # field | scope
    reason: str
    options: list[str] = field(default_factory=list)       # binding kinds that can answer it


@dataclass
class BlindResult:
    spec: dict[str, Any]
    frames: list[Frame]
    bindings: list[list[Binding]]
    questions: list[Question]
    rejected: list[tuple[str, Binding, str]]
    notes: list[str] = field(default_factory=list)
    precedents: list[tuple[str, Any]] = field(default_factory=list)       # (sheet, Match) reused from the precedent store


# ---- rules (deterministic, generic) ------------------------------------------------------------------------------------

def _date_fact(frame: Frame) -> tuple[str, str] | None:
    for ref, text in frame.facts.items():
        if re.search(r"\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2}", text):
            return ref, text
    return None


def rule_bindings(frame: Frame, target: Target, params: dict[str, str]) -> list[Binding]:
    out: list[Binding] = []
    have: set[str] = set()

    def add(b: Binding) -> None:
        if b.target and b.target in have:
            return
        out.append(b)
        if b.target:
            have.add(b.target)

    for k, v in params.items():                                         # human parameters
        if k in target.fields:
            add(Binding("constant", k, {"value": v}, 1.0, "human", ["parameter"]))
    # enum-valued fields: a source column whose values are (a subset of) the allowed values
    for tname, t in target.fields.items():
        if not t.enum or tname in have:
            continue
        allowed = {e.lower(): e for e in t.enum}
        for sname, s in frame.sources.items():
            if s.kind not in ("column", "group", "dim") or not s.n_distinct or s.n_distinct > len(allowed) + 1:
                continue
            vals = frame.distinct_all.get(sname, s.distinct)
            if vals and all(str(v).lower() in allowed for v in vals):
                add(Binding("column", tname, {"source": sname, "mapping": {v: allowed[str(v).lower()] for v in vals}}, 0.9,
                            "rule", [f"values of {s.origin or sname} are {vals}"]))
                break
    # dates
    date_src = next((n for n, s in frame.sources.items() if s.kind == "column" and s.dtype == "date"), None)
    if date_src and "reference_date" not in have:
        add(Binding("column", "reference_date", {"source": date_src, "as_date": True}, 0.8, "rule", [frame.sources[date_src].origin]))
    elif "reference_date" not in have and (fact := _date_fact(frame)):
        add(Binding("sheet_cell", "reference_date", {"cell": fact[0], "date": True}, 0.6, "rule", [fact[0]]))
    if "reference_date" in have:
        for pt in ("period_start", "period_end"):
            add(Binding("template", pt, {"template": "{reference_date}"}, 0.5, "rule",
                        ["assumes a point-in-time observation: start = end = reference date"]))
    # the number
    if frame.value_source:
        scale_cell = next((r for r, t in frame.facts.items() if THOUSANDS.search(t)), None)
        unit_hit = next((v for v in frame.unit_texts if THOUSANDS.search(v)), None)
        factor = 1000 if (scale_cell or unit_hit) else 1
        add(Binding("scale", "value", {"source": frame.value_source, "factor": factor}, 0.7 if factor != 1 else 0.8, "rule",
                    [scale_cell or unit_hit or "no scale printed"]))
        tokens = {v for v in frame.value_tokens}
        if tokens and tokens <= {"X", "x", "n/a", "N/A", "NA"}:
            add(Binding("constant", "value_status", {"value": "reported"}, 0.6, "rule", ["numbers are reported"]))
            out.append(Binding("template", "value_status", {"template": "not_applicable",
                                                           "where": {frame.value_source: sorted(tokens)[0]}}, 0.6, "rule",
                               [f"marker {sorted(tokens)} instead of a number"]))
        elif "value_status" not in have and not tokens:
            add(Binding("constant", "value_status", {"value": "reported"}, 0.7, "rule", ["every cell is a number"]))
    for tname, (kind, args) in {"source_sheet": ("column", {"source": "_src_sheet"}), "source_cell": ("column", {"source": "source_cell"})}.items():
        add(Binding(kind, tname, args, 1.0, "rule", ["lineage"]))
    return out


def open_questions(frame: Frame, target: Target, bound: set[str], answered: set[str] | None = None) -> list[Question]:
    answered = answered or set()
    qs = [Question(f"{frame.sheet}:{t}", frame.sheet, [t], "field", f"no evidence binds {t}",
                   ["constant", "column", "sheet_cell", "template"])
          for t, f in target.fields.items() if (f.required or str(target.example.get(t, "")).strip()) and t not in bound]
    if frame.layout in ("grid", "wide") and "grid_scope" not in answered:
        qs.append(Question(f"{frame.sheet}:grid_scope", frame.sheet, [], "scope",
                           f"{len(frame.dims.get('dim1', []))} x {len(frame.dims.get('dim2', [])) or 1} value columns: which belong to the extraction?",
                           ["grid_scope"]))
    totalish = [n for n, s in frame.sources.items() if s.kind == "column" and any(re.match(r"(?i)^(all\b|total|insgesamt|ensemble)", v) for v in s.distinct)]
    if totalish and "row_scope" not in answered:
        qs.append(Question(f"{frame.sheet}:row_scope", frame.sheet, [], "scope",
                           f"columns {totalish} mix totals with detail rows: which rows belong to the extraction?", ["row_scope"]))
    return qs


def _period_wide(sp: Any) -> bool:
    """Three or more header columns that are years / quarters: the periods run across, the template wants them as rows."""
    rx = re.compile(r"(19|20)\d{2}(\s*[Qq][1-4])?|(19|20)\d{2}[-/]\d{2}")
    return sum(bool(rx.fullmatch(str(n).split("|")[-1].strip())) for n in sp.flat_names) >= 3


def _is_sheet_number(b: Binding) -> bool:
    return b.kind == "column" and b.args.get("source") == "_src_sheet" and bool(b.args.get("mapping"))


def _record_family(fr: Frame, target: Target, params: dict[str, str], families: dict[tuple, list[Binding]]) -> list[Binding]:
    """Sheets with the same columns are one table in several pieces: they get the same column decisions (made once, on the
    first piece); only what depends on the sheet itself - a number in its name - is read again for each."""
    fresh = record_bindings(fr, target, params, target.example)
    sig = tuple(sorted(n for n, s_ in fr.sources.items() if s_.kind == "column"))
    if sig not in families:
        families[sig] = fresh
        return fresh
    return [b for b in families[sig] if not _is_sheet_number(b)] + [b for b in fresh if _is_sheet_number(b)]


_PROFILES: dict[tuple[str, float], Any] = {}


def _profile(path: str | Path, structure: dict[str, dict[str, Any]] | None = None) -> Any:
    """Profiling a 7 MB workbook takes seconds; the same file is drafted many times per session."""
    p = Path(path)
    key = (str(p.resolve()), p.stat().st_mtime, json.dumps(structure, sort_keys=True, default=str) if structure else "")
    if key not in _PROFILES:
        _PROFILES[key] = profile_file(p, structure)
    return _PROFILES[key]


# ---- orchestration ----------------------------------------------------------------------------------------------------------------------

def draft_blind(path: str | Path, sheets: list[str], target: Target, params: dict[str, str], *, name: str, filename: str,
                answers: dict[str, list[Binding]] | None = None, use_rules: bool = True, store: Any = None,
                structure: dict[str, dict[str, Any]] | None = None) -> BlindResult:
    prof = _profile(path, structure)
    frames, per_frame, rejected, questions = [], [], [], []
    families: dict[tuple, list[Binding]] = {}
    reused: list[tuple[str, Any]] = []
    for sh in sheets:
        wide = target.mode == "record" and _period_wide(prof.sheets[sh])
        fr = build_frame(prof.sheets[sh], flat=target.mode == "record" and not wide, fold=wide)
        fr.file_name = Path(path).name
        bs = (_record_family(fr, target, params, families) if target.mode == "record" else rule_bindings(fr, target, params)) if use_rules else [
            Binding("column", "source_sheet", {"source": "_src_sheet"}, 1.0, "rule", ["lineage"]),
            Binding("column", "source_cell", {"source": "source_cell"}, 1.0, "rule", ["lineage"])]
        extra = list((answers or {}).get(sh, []))
        if store is not None and (m := store.lookup(fr, list(target.fields))):          # explicit answers win over a precedent
            extra += m.bindings
            reused.append((sh, m))
        merged: list[Binding] = []
        bound: set[str] = set()
        ctx = fr.context(target.fields)
        for b in [*extra, *bs]:                     # explicit answers win over rules
            if b.target and b.target in bound and b.kind != "template" and not (b.kind == "scale" and b.args.get("where")):
                continue
            try:
                validate(b, ctx)
            except BindingError as exc:
                rejected.append((sh, b, str(exc)))
                continue
            merged.append(b)
            if b.target:
                bound.add(b.target)
                ctx.bound.add(b.target)
        frames.append(fr)
        per_frame.append(merged)
        firm = bound if target.mode != "record" else {b.target for b in merged if b.target and (b.origin in ("human", "precedent") or b.confidence >= 0.7)}
        questions += open_questions(fr, target, firm, {b.kind for b in merged})      # a record-table guess (< 0.7) is still asked
    spec = compile_spec(frames, per_frame, target.fields, name=name, filename=filename)
    return BlindResult(spec, frames, per_frame, questions, rejected, precedents=reused)
