"""The closed answer vocabulary: the ONLY thing a rule, a human or an LLM may say about a target field.

Nobody (least of all a model) writes ops, regexes or code. A Binding says where one target field comes from; the
compiler (`compile.py`) turns bindings into spec ops, and every binding is validated against what is actually in the
file (source names, header values, cell references) and against the target schema (enums, types).

    constant    {value}                                  the same value on every row
    column      {source, mapping?, as_date?}             copy a source column / header dimension / group label,
                                                         optionally translating its values
    sheet_cell  {cell, mapping?, date?}                  a fact printed once on the sheet (title, unit, date)
    template    {template, where?}                       text built from sources / already bound fields, per condition
    scale       {source, factor}                         numeric source x factor (thousands -> units)
    row_scope   {conditions, mode}                       keep only some rows
    grid_scope  {dimension, include}                     keep only some value columns of a multi-level header
    unknown     {reason}                                 cannot be decided from the evidence (a legitimate answer)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

KINDS = ("constant", "column", "sheet_cell", "template", "scale", "row_scope", "grid_scope", "unknown", "occurrence", "group_flag", "flags", "keep_rows", "period", "arith")
COND_KEYS = ("equals", "in", "not_in", "same_as", "regex", "not_regex", "notnull")


class BindingError(ValueError):
    """The answer is not expressible in the vocabulary or contradicts the file / schema."""


@dataclass
class TargetField:
    name: str
    type: str = "string"                 # string | date | decimal
    enum: list[str] = field(default_factory=list)
    required: bool = True
    description: str = ""
    lexicon: dict[str, list[str]] = field(default_factory=dict)      # value -> words known to mean it (multilingual)


@dataclass
class Binding:
    kind: str
    target: str | None = None            # None for row_scope / grid_scope
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    origin: str = "rule"                 # rule | llm | human
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Binding":
        try:
            return Binding(kind=d["kind"], target=d.get("target"), args=dict(d.get("args", {})),
                           confidence=float(d.get("confidence", 1.0)), origin=d.get("origin", "llm"),
                           evidence=[str(e) for e in d.get("evidence", [])])
        except (KeyError, TypeError, ValueError) as exc:
            raise BindingError(f"malformed binding: {exc}") from exc


@dataclass
class ValidationContext:
    """What exists for ONE sheet frame: the only names / cells an answer may refer to."""
    targets: dict[str, TargetField]
    sources: set[str]                                     # neutral column names + dim1/dim2/g1/g2/cell_value
    dim_values: dict[str, list[str]] = field(default_factory=dict)      # dimension -> its distinct values
    cells: set[str] = field(default_factory=set)          # banner cell references with text
    bound: set[str] = field(default_factory=set)          # targets already bound (usable inside templates)


def _check_enum(t: TargetField, values: list[str], what: str) -> None:
    if t.enum:
        bad = [v for v in values if v not in t.enum]
        if bad:
            raise BindingError(f"{what} {bad} not allowed for {t.name}; allowed: {t.enum}")


def _placeholders(template: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:[+-]\d+)?(?::[^{}]*)?\}", template))      # {name} or {name-1}: a number shifted by a constant


def validate(b: Binding, ctx: ValidationContext) -> None:
    if b.kind not in KINDS:
        raise BindingError(f"unknown kind {b.kind!r}; allowed: {KINDS}")
    if not 0.0 <= b.confidence <= 1.0:
        raise BindingError("confidence must be within 0..1")
    a = b.args
    if b.kind in ("row_scope", "grid_scope", "keep_rows"):
        if b.target is not None:
            raise BindingError(f"{b.kind} has no target")
    else:
        if b.target not in ctx.targets:
            raise BindingError(f"target {b.target!r} is not a field of the schema")
    t = ctx.targets.get(b.target) if b.target else None

    if b.kind == "constant":
        if "value" not in a:
            raise BindingError("constant needs value")
        _check_enum(t, [str(a["value"])], "constant")
    elif b.kind == "column":
        if a.get("source") not in ctx.sources:
            raise BindingError(f"unknown source {a.get('source')!r}; available: {sorted(ctx.sources)}")
        mapping = a.get("mapping") or {}
        _check_enum(t, [str(v) for v in mapping.values()], "mapping value")
        if mapping and t.lexicon:             # a valid-looking answer can still be wrong: check it against known words
            for k, v in mapping.items():
                classes = {cls for cls, words in t.lexicon.items() if str(k).strip().lower() in words}
                if classes and str(v) not in classes:
                    raise BindingError(f"{k!r} is a known word for {sorted(classes)} but is mapped to {v!r}")
        if a.get("extract"):
            try:
                re.compile(a["extract"])
            except re.error as ex:
                raise BindingError(f"extract {a['extract']!r} is not a valid regular expression: {ex}")
        known = ctx.dim_values.get(a["source"])
        if mapping and known:
            ws = lambda x: re.sub(r"\s+", " ", str(x)).strip()      # noqa: E731
            known_ws = {ws(k) for k in known}
            miss = [k for k in mapping if ws(k) not in known_ws]
            if miss:
                raise BindingError(f"mapping keys {miss} do not occur in {a['source']!r}; values: {known[:12]}")
    elif b.kind == "sheet_cell":
        if a.get("cell") not in ctx.cells:
            raise BindingError(f"cell {a.get('cell')!r} is not a banner cell of this sheet; available: {sorted(ctx.cells)}")
        _check_enum(t, [str(v) for v in (a.get("mapping") or {}).values()], "mapping value")
    elif b.kind == "template":
        tpl = a.get("template")
        if not isinstance(tpl, str) or not tpl:
            raise BindingError("template needs a text")
        unknown = [p for p in _placeholders(tpl) if p not in ctx.sources and p not in ctx.bound]
        if unknown:
            raise BindingError(f"template refers to unknown names {unknown}")
        for cond_col in (a.get("where") or {}):
            if cond_col not in ctx.sources and cond_col not in ctx.bound:
                raise BindingError(f"where refers to unknown name {cond_col!r}")
        if t.enum and not _placeholders(tpl):
            _check_enum(t, [tpl], "template literal")
    elif b.kind == "scale":
        if a.get("source") not in ctx.sources:
            raise BindingError(f"unknown source {a.get('source')!r}")
        if not isinstance(a.get("factor"), (int, float)) or a["factor"] == 0:
            raise BindingError("factor must be a non-zero number")
    elif b.kind == "row_scope":
        conds = a.get("conditions")
        if not conds or a.get("mode", "all") not in ("all", "any"):
            raise BindingError("row_scope needs conditions and mode all|any")
        for c in conds:
            if c.get("column") not in ctx.sources:
                raise BindingError(f"unknown column {c.get('column')!r}")
            if not any(k in c for k in COND_KEYS):
                raise BindingError(f"condition needs one of {COND_KEYS}")
            for rk in ("regex", "not_regex"):
                if rk in c:
                    try:
                        re.compile(str(c[rk]))
                    except re.error as ex:
                        raise BindingError(f"{rk} {c[rk]!r} is not a valid regular expression: {ex}")
            if "same_as" in c and c["same_as"] not in ctx.sources:
                raise BindingError(f"unknown column {c['same_as']!r}")
    elif b.kind == "grid_scope":
        dim = a.get("dimension")
        if dim not in ctx.dim_values:
            raise BindingError(f"dimension {dim!r} not available; use one of {sorted(ctx.dim_values)}")
        known_ws = {re.sub(r"\s+", " ", str(k)).strip() for k in ctx.dim_values[dim]}
        miss = [v for v in a.get("include", []) if re.sub(r"\s+", " ", str(v)).strip() not in known_ws]
        if not a.get("include") or miss:
            raise BindingError(f"include must be values of {dim}: {ctx.dim_values[dim][:12]} (bad: {miss})")
    elif b.kind == "period":
        if a.get("source") not in ctx.sources and a.get("source") not in ctx.bound and a.get("source") not in ctx.targets:
            raise BindingError(f"period needs a known source; available: {sorted(ctx.sources)[:12]}")
        if a.get("part") not in ("start", "end", "type"):
            raise BindingError("period part must be start, end or type")
    elif b.kind == "arith":
        if a.get("op") not in ("mul", "add", "sub", "div"):
            raise BindingError("arith op must be mul, add, sub or div")
        for k in ("a", "b"):
            if a.get(k) not in ctx.sources and a.get(k) not in ctx.bound and a.get(k) not in ctx.targets:
                raise BindingError(f"arith {k} must be a known source or field; got {a.get(k)!r}")
    elif b.kind == "keep_rows":
        if b.target is not None or not a.get("labels"):
            raise BindingError("keep_rows has no target and needs labels: the row labels that are data, not totals or notes")
    elif b.kind == "flags":
        rules = a.get("rules")
        if not rules or any(not r.get("label") or not r.get("any_of") for r in rules):
            raise BindingError("flags needs rules: [{label, any_of: [where, ...]}]")
        for r in rules:
            for cond in r["any_of"]:
                for col in cond:
                    if col not in ctx.sources and col not in ctx.bound and col not in ctx.targets:
                        raise BindingError(f"flags refers to unknown name {col!r}")
    elif b.kind == "group_flag":
        missing = [c for c in a.get("by", []) if c not in ctx.sources and c not in ctx.bound and c not in ctx.targets]
        if not a.get("by") or missing or "repeated" not in a or "otherwise" not in a:
            raise BindingError(f"group_flag needs by (known names), repeated, otherwise; unknown: {missing}")
    elif b.kind == "unknown":
        pass
    for e in b.evidence:                     # cited cells must exist: no invented evidence
        if re.fullmatch(r"[A-Z]{1,3}\d+", e) and e not in ctx.cells and b.kind == "sheet_cell":
            raise BindingError(f"evidence cell {e} is not in the packet")
