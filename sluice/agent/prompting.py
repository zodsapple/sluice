"""What an LLM (or a human) gets to see, what it must return, and what happens when it fails.

Principles
  * Minimal: structure and distinct values, never data rows; only sources that could answer the question; every list is
    capped, the whole packet has a character budget; sensitive columns are redacted.
  * Sufficient: a mapping question gets ALL distinct values of the mapped source (a partial mapping silently leaves rows
    untranslated), banner cells that carry facts (title, unit, date), what is already bound, the schema rules that apply.
  * Closed: the answer is JSON in the binding vocabulary (bindings.py). No code, no regexes, no ops.
  * Verifiable: every binding is validated against the file and the schema; the draft must still run; low confidence is
    deferred to a human. Nothing the model says is trusted because the model said it.
  * Optional: no key / no network / bad answers -> the deterministic draft plus human questions (same as without LLM).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sluice.agent.interface import LLMUnavailable
from sluice.infer.bindings import Binding, BindingError, TargetField, validate
from sluice.infer.frame import Frame

PROMPT_VERSION = "blind-binding-v1"

SYSTEM_PROMPT = """You complete a data-mapping draft. A workbook sheet was already parsed (header rows, group rows,
multi-level headers). You get the target fields that are still unbound, and the facts available on that sheet.
Answer ONLY with JSON: {"answers":[{"question_id": str, "bindings":[...], "status": "answered"|"unknown"}]}.

A binding is {"kind","target","args","confidence","evidence"}; kinds and args (nothing else exists):
  constant   {value}                       same value on every row
  column     {source, mapping?, verbatim?, extract?} copy a listed source; mapping translates its values (list EVERY value you can);
                                           verbatim:true keeps the text untrimmed; extract = regex whose first group is the value
  period     {source, part}                part = start | end | type of the period a label names (2023, 2023 Q2, JAN 2023 ...)
  arith      {op, a, b, decimals?}         op = mul | add | sub | div of two sources or fields
  sheet_cell {cell, mapping?|date?}        a fact printed once on the sheet; mapping = substring -> value
  template   {template, where?, verbatim?} text with {source} placeholders; {date_source:%Y-%m-%dT%H:%M:%S} formats date cells;
                                           verbatim:[source_names] preserves their printed whitespace;
                                           where = {source: value | {regex|not_regex|in|not_in|isnull|notnull: ..}}
  scale      {source, factor}              numeric source x factor
  group_flag {by:[names], repeated, otherwise}   repeated when another row has the same values in `by` (names = sources or fields), else otherwise
  row_scope  {conditions:[{column, equals|in|not_in|regex|not_regex|same_as}], mode:"all"|"any"}    (no target)
  grid_scope {dimension, include:[...]}    (no target)
Questions: {"id","targets"} = bind these fields (any kind except row_scope/grid_scope); a question with "kind":"scope" is
answered with row_scope / grid_scope bindings only (the id ends with the kind). One answer entry per question id.
Rules: use only names, values and cells that appear in the packet. For fields with allowed values use only those values.
For a free-text field ("precedents" lists values other, already approved sources use for it) write the value in the same
style, via "mapping" when it depends on a source column. Copying source labels unchanged into such a field is almost always
wrong: translate them with "mapping" or answer "unknown". row_scope / grid_scope: list EVERY row / column group that belongs to
the extraction - detail rows and totals are usually both wanted unless the packet says otherwise. "example_row" is the
template's own example for the fields asked: a value that occurs in a listed source, or equals one of its mapped values, is
strong evidence which source the field is. It is ONE row: never copy its value as a constant for a field that may differ
between rows (flags, statuses, types, counts) - answer "unknown" unless a listed source or cell decides it. A source marked
"partial" shows only a sample of its values: never list the sample as an "in" list; state the rule instead (equals, not_in
for the few values to drop, regex / not_regex for a naming pattern such as a total label) or answer "unknown".
Date fields are ISO text YYYY-MM-DD. When the date follows from a listed source (a year or month label), build it with a
template, e.g. "{year}-12-31"; when it differs between cases, give one template binding per case, each with its own
"where" (see example 3). Use "sheet_cell" with date for a date printed once on the sheet.
A field defined for only some rows ("filled on X rows only"): one "template" with a "where" for those rows; rows outside the
"where" stay empty. A two-valued field defined by a condition: one template per value, the second with "not_in" / "not_regex"
(see example 4). Several bindings for one field are only allowed in this template + where form.
If the evidence does not decide it, return status "unknown" - do not guess. confidence is 0..1; evidence lists the
source names / cells you relied on. Several bindings for one target are allowed only as template + where cases."""

RESPONSE_SCHEMA = {
    "type": "object", "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "required": ["question_id", "status"],
        "properties": {"question_id": {"type": "string"}, "status": {"enum": ["answered", "unknown"]},
                       "bindings": {"type": "array", "items": {
                           "type": "object", "required": ["kind", "args"],
                           "properties": {"kind": {"enum": ["constant", "column", "sheet_cell", "template", "scale",
                                                            "row_scope", "grid_scope", "group_flag", "period", "arith", "unknown"]},
                                          "target": {"type": ["string", "null"]}, "args": {"type": "object"},
                                          "confidence": {"type": "number"}, "evidence": {"type": "array", "items": {"type": "string"}}}}}}}}}}

FEW_SHOT = [  # tiny, from an unrelated domain, only to show the shape
    {"packet_excerpt": {"questions": [{"id": "s1:status", "targets": ["status"]}],
                        "sources": [{"name": "state", "kind": "column", "n": 2, "from": "State", "distinct": ["OPEN", "CLOSED"]}],
                        "schema": [{"name": "status", "allowed": ["open", "closed"], "meaning": "ticket status"}]},
     "answer": {"answers": [{"question_id": "s1:status", "status": "answered", "bindings": [
         {"kind": "column", "target": "status", "args": {"source": "state", "mapping": {"OPEN": "open", "CLOSED": "closed"}},
          "confidence": 0.95, "evidence": ["state"]}]}]}},
    {"packet_excerpt": {"questions": [{"id": "s1:currency", "targets": ["currency"]}], "schema": [{"name": "currency", "meaning": "ISO code"}],
                        "cells": {"A2": "Orders in thousand"}},
     "answer": {"answers": [{"question_id": "s1:currency", "status": "unknown", "bindings": []}]}},
    {"packet_excerpt": {"questions": [{"id": "s1:day", "targets": ["day"]}],
                        "sources": [{"name": "yr", "kind": "dim", "n": 2, "from": "upper header level", "distinct": ["2020", "2021"]},
                                    {"name": "kind", "kind": "group", "n": 2, "from": "group header rows", "distinct": ["Stock at 1 July", "Sales over the year"]}],
                        "schema": [{"name": "day", "type": "date", "meaning": "date the value refers to (ISO)"}]},
     "answer": {"answers": [{"question_id": "s1:day", "status": "answered", "bindings": [
         {"kind": "template", "target": "day", "args": {"template": "{yr}-07-01", "where": {"kind": "Stock at 1 July"}},
          "confidence": 0.9, "evidence": ["yr", "kind"]},
         {"kind": "template", "target": "day", "args": {"template": "{yr}-12-31", "where": {"kind": "Sales over the year"}},
          "confidence": 0.8, "evidence": ["yr", "kind"]}]}]}},
    {"packet_excerpt": {"questions": [{"id": "s1:row_kind", "targets": ["row_kind"]}, {"id": "s1:units", "targets": ["units"]},
                                      {"id": "s1:is_gift", "targets": ["is_gift"]}],
                        "sources": [{"name": "item_code", "kind": "column", "n": 3, "from": "Item", "distinct": ["NA", "A1", "B7"]},
                                    {"name": "qty", "kind": "column", "n": 40, "from": "Qty"},
                                    {"name": "note", "kind": "column", "n": 3, "from": "Note", "distinct": ["GIFT", "RETURN", "-"]}],
                        "schema": [{"name": "row_kind", "meaning": "'summary' for a total line (item NA), 'line' for an item line"},
                                   {"name": "units", "meaning": "quantity; filled on item lines only, empty on summary lines"},
                                   {"name": "is_gift", "meaning": "'yes' when the note is exactly GIFT, otherwise 'no'"}]},
     "answer": {"answers": [
         {"question_id": "s1:row_kind", "status": "answered", "bindings": [
             {"kind": "template", "target": "row_kind", "args": {"template": "summary", "where": {"item_code": "NA"}}, "confidence": 0.9, "evidence": ["item_code"]},
             {"kind": "template", "target": "row_kind", "args": {"template": "line", "where": {"item_code": {"not_in": ["NA"]}}}, "confidence": 0.9, "evidence": ["item_code"]}]},
         {"question_id": "s1:units", "status": "answered", "bindings": [
             {"kind": "template", "target": "units", "args": {"template": "{qty}", "where": {"item_code": {"not_in": ["NA"]}}}, "confidence": 0.85, "evidence": ["qty", "item_code"]}]},
         {"question_id": "s1:is_gift", "status": "answered", "bindings": [
             {"kind": "template", "target": "is_gift", "args": {"template": "yes", "where": {"note": "GIFT"}}, "confidence": 0.9, "evidence": ["note"]},
             {"kind": "template", "target": "is_gift", "args": {"template": "no", "where": {"note": {"not_in": ["GIFT"]}}}, "confidence": 0.9, "evidence": ["note"]}]}]}},
]


class LLM(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


def est_tokens(obj: Any) -> int:
    return max(1, len(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) // 4)


# ---- packet ------------------------------------------------------------------------------------------------------------------------------

def _rank(source_name: str, info: Any, target: TargetField) -> float:
    from sluice.infer.autospec import similarity
    score = similarity(target.name, source_name) + 0.2 * similarity(target.description, getattr(info, "origin", "") or source_name)
    if target.enum and info.distinct and {str(v).lower() for v in info.distinct} & {e.lower() for e in target.enum}:
        score += 1.0
    if info.kind in ("dim", "group"):
        score += 0.3
    return score


def build_packet(frame: Frame, questions: list[Any], targets: dict[str, TargetField], bound: dict[str, str],
                 *, redact: set[str] | None = None, budget_chars: int = 6000, conventions: list[str] | None = None,
                 precedents: dict[str, list[str]] | None = None, example: dict[str, Any] | None = None) -> dict[str, Any]:
    redact = redact or set()
    qs = [({"id": q.id, "targets": q.targets} if q.kind == "field" else {"id": q.id, "kind": "scope", "ask": q.reason})
          for q in questions]
    asked = [t for q in questions for t in q.targets]
    schema = [{"name": t, **({"type": targets[t].type} if targets[t].type != "string" else {}),
               **({"allowed": targets[t].enum} if targets[t].enum else {}), "meaning": targets[t].description[:240]}
              for t in dict.fromkeys(asked)]
    scored: dict[str, float] = {}
    for n, s in frame.sources.items():
        if n.startswith("_") or n in ("source_cell",):
            continue
        scored[n] = max((_rank(n, s, targets[t]) for t in asked), default=0.0) + (0.5 if s.kind in ("dim", "group", "value") else 0)
    keep = sorted(scored, key=lambda n: -scored[n])[:7]
    scope_q = any(q.kind == "scope" for q in questions)

    def src_entry(n: str, full: bool) -> dict[str, Any]:
        s = frame.sources[n]
        vals = [] if n in redact else (frame.distinct_all.get(n, s.distinct))
        cap = 40 if scope_q else 12                                   # a scope answer must be able to see every value
        limit = len(vals) if (full and len(vals) <= cap) else 8
        e = {"name": n, "kind": s.kind, "n": s.n_distinct or len(vals), "from": s.origin[:40]}
        if vals:
            shown = vals[:limit] if limit >= len(vals) else list(vals[:5]) + list(vals[-3:])     # totals sit at either end
            e["distinct"] = [str(v)[:120] for v in shown]
            if limit < len(vals):
                e["partial"] = True                                   # only a sample: never enumerate a scope from it
        return e

    def assemble(full: bool, cells_n: int) -> dict[str, Any]:
        facts = {k: v[:160] for k, v in list(frame.facts.items())[:cells_n]}
        pk: dict[str, Any] = {
            "sheet": frame.sheet, "layout": frame.layout, "schema": schema, "questions": qs,
            "sources": [src_entry(n, full) for n in keep],
            "cells": facts,
        }
        if frame.dims and (scope_q or any(t for t in asked)):
            pk["header_dimensions"] = {k: [str(x)[:120] for x in v][:40] for k, v in frame.dims.items()}
        labels = {str(x) for k in ("g1", "g2") for x in frame.distinct_all.get(k, [])}
        units = [u[:80] for u in frame.unit_texts if u not in labels][:3]          # group labels are not units
        if units:
            pk["unit_texts"] = units
        prec = {t: [v[:160] for v in (precedents or {}).get(t, [])[:4]] for t in dict.fromkeys(asked)
                if (precedents or {}).get(t) and not targets[t].enum}
        if frame.file_name:
            pk["file_name"] = frame.file_name[:100]
        if prec:
            pk["precedents"] = prec
        if example:                                   # the one filled-in row of the template: what the answer should look like
            ex = {t: str(example[t])[:80] for t in dict.fromkeys(asked) if t in example}
            if ex:
                pk["example_row"] = ex
        if frame.value_tokens:
            pk["non_numeric_value_tokens"] = frame.value_tokens
        if bound:
            pk["already_bound"] = bound
        if conventions:
            pk["conventions"] = [c[:200] for c in conventions if c]
        return pk

    packet = assemble(True, 6)
    for full, cells_n in ((True, 4), (False, 4), (False, 2)):         # shrink until it fits the budget
        if len(json.dumps(packet, ensure_ascii=False)) <= budget_chars:
            break
        packet = assemble(full, cells_n)
    packet["_hash"] = hashlib.sha256(json.dumps({k: v for k, v in packet.items() if k != "_hash"}, sort_keys=True,
                                                ensure_ascii=False).encode()).hexdigest()[:16]
    return packet


def collect_precedents(binding_sets: list[list[Binding]], targets: dict[str, TargetField], limit: int = 4) -> dict[str, list[str]]:
    """Values already-approved sources use for free-text fields (naming style the target expects). Values only, no data."""
    out: dict[str, list[str]] = {}
    for bs in binding_sets:
        for b in bs:
            if not b.target or b.target in ("source_sheet", "source_cell") or targets[b.target].enum or targets[b.target].type != "string":
                continue
            vals: list[Any] = []
            if b.kind == "constant":
                vals = [b.args["value"]]
            elif b.kind == "column" and b.args.get("mapping"):
                vals = list(b.args["mapping"].values())
            elif b.kind == "template" and "{" not in b.args.get("template", "{"):
                vals = [b.args["template"]]
            for v in vals:
                v = str(v)
                bucket = out.setdefault(b.target, [])
                if v and len(v) <= 160 and v not in bucket and len(bucket) < limit:
                    bucket.append(v)
    return out


def render_messages(packet: dict[str, Any], *, examples: bool = True) -> list[dict[str, str]]:
    body = {k: v for k, v in packet.items() if not k.startswith("_")}
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if examples:
        for ex in FEW_SHOT:
            msgs.append({"role": "user", "content": json.dumps(ex["packet_excerpt"], ensure_ascii=False)})
            msgs.append({"role": "assistant", "content": json.dumps(ex["answer"], ensure_ascii=False)})
    msgs.append({"role": "user", "content": json.dumps(body, ensure_ascii=False)})
    return msgs


def render_single_prompt(packet: dict[str, Any], *, examples: bool = True) -> str:
    """The same content as `render_messages`, as ONE text block to paste into a chat window (no system role, no API)."""
    body = {k: v for k, v in packet.items() if not k.startswith("_")}
    parts = ["### INSTRUCTIONS", SYSTEM_PROMPT]
    if examples:
        parts.append("### EXAMPLES (unrelated domain; only to show the reply shape)")
        for i, ex in enumerate(FEW_SHOT, 1):
            parts += [f"Example {i} input:", json.dumps(ex["packet_excerpt"], ensure_ascii=False),
                      f"Example {i} reply:", json.dumps(ex["answer"], ensure_ascii=False)]
    parts += ["### PACKET (answer this one)", json.dumps(body, ensure_ascii=False, indent=1),
              "### YOUR REPLY\nOne JSON object only, no prose, no code fence. It must contain one entry in \"answers\" for EVERY "
              "question id in the packet (use status \"unknown\" when you cannot decide). Do not return an empty list."]
    return "\n\n".join(parts)


# ---- parsing + resolution ladder ----------------------------------------------------------------------------------------------------------

def parse_response(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise BindingError("no JSON object in the reply")
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise BindingError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("answers"), list):
        raise BindingError("reply must be {\"answers\": [...]}")
    return doc["answers"]


@dataclass
class ResolveReport:
    calls: int = 0
    repaired: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    accepted: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)       # (target/kind, reason)
    deferred_low_confidence: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    reverted_by_verification: list[str] = field(default_factory=list)
    llm_unavailable: str = ""
    review: list[Any] = field(default_factory=list)            # what a human must look at (see review.py)
    unanswered: list[str] = field(default_factory=list)        # question ids the reply did not mention at all


class Resolver:
    """Rules first; the model only for what is still open; everything it returns is checked before it is used."""

    def __init__(self, llm: LLM | None, *, min_confidence: float = 0.6, max_calls: int = 6, max_tokens: int = 40000,
                 cache: dict[str, str] | None = None, redact: set[str] | None = None) -> None:
        self.llm, self.min_conf, self.max_calls, self.max_tokens = llm, min_confidence, max_calls, max_tokens
        self.cache = cache if cache is not None else {}
        self.redact = redact or set()
        self.report = ResolveReport()
        self.down = False                    # circuit breaker: once the responder is unavailable, stop calling it

    # one frame -> accepted bindings
    def answer_frame(self, frame: Frame, questions: list[Any], targets: dict[str, TargetField], bound_desc: dict[str, str],
                     ctx_factory: Callable[[], Any], conventions: list[str] | None = None,
                     precedents: dict[str, list[str]] | None = None, example: dict[str, Any] | None = None) -> list[Binding]:
        r = self.report
        if self.llm is None or not questions or self.down:
            return []
        packet = build_packet(frame, questions, targets, bound_desc, redact=self.redact, conventions=conventions,
                              precedents=precedents, example=example)
        msgs = render_messages(packet)
        r.tokens_in += est_tokens(msgs)
        if r.calls >= self.max_calls or r.tokens_in > self.max_tokens:
            r.llm_unavailable = "budget exhausted"
            return []
        text = self.cache.get(packet["_hash"])
        if text is not None:
            r.cache_hits += 1
        else:
            try:
                r.calls += 1
                text = self.llm.complete(msgs)
                self.cache[packet["_hash"]] = text
            except LLMUnavailable as exc:
                r.llm_unavailable, self.down = str(exc), True
                return []
            except Exception as exc:      # noqa: BLE001 - timeouts, HTTP errors, anything: fall back to the human
                r.llm_unavailable, self.down = f"{type(exc).__name__}: {exc}"[:160], True
                return []
        r.tokens_out += est_tokens(text)
        try:
            answers = parse_response(text)
        except BindingError as exc:       # one repair attempt, with the error only (cheap)
            if r.calls >= self.max_calls or "{" not in (text or ""):      # a refusal is not a broken attempt: do not pay twice
                r.rejected.append((frame.sheet, f"unparseable reply: {exc}"))
                return []
            try:
                r.calls += 1
                r.repaired += 1
                text = self.llm.complete([*msgs, {"role": "assistant", "content": (text or "")[:1500]},
                                          {"role": "user", "content": f"Invalid reply: {exc}. Return only the JSON object."}])
                answers = parse_response(text)
            except Exception as exc2:     # noqa: BLE001
                r.rejected.append((frame.sheet, f"unparseable reply after repair: {exc2}"))
                return []
        asked_ids = [q.id for q in questions]

        def missing_of(ans: list[dict[str, Any]]) -> list[str]:
            given = {a.get("question_id") for a in ans if isinstance(a, dict)}
            return [i for i in asked_ids if i not in given]

        missing = missing_of(answers)
        if len(missing) >= 0.5 * len(asked_ids) and r.calls < self.max_calls and not r.llm_unavailable:
            try:                              # one nudge: name the ids, allow "unknown"
                r.calls += 1
                r.repaired += 1
                nudge = (f"Your reply answers {len(asked_ids) - len(missing)} of {len(asked_ids)} questions. "
                         f"Missing ids: {missing[:40]}. Return one entry per question id; use status \"unknown\" "
                         "when the packet does not decide it. Return only the JSON object.")
                text2 = self.llm.complete([*msgs, {"role": "assistant", "content": (text or "")[:1500]},
                                           {"role": "user", "content": nudge}])
                r.tokens_out += est_tokens(text2)
                second = parse_response(text2)
                answers = [*answers, *[a for a in second if isinstance(a, dict) and a.get("question_id") in missing]]
            except Exception:                 # noqa: BLE001 - keep what we have
                pass
            missing = missing_of(answers)
        r.unanswered += missing
        accepted: list[Binding] = []
        ctx = ctx_factory()
        asked = {q.id for q in questions}
        for ans in answers:
            if ans.get("question_id") not in asked:
                r.rejected.append((str(ans.get("question_id")), "answer to a question that was not asked"))
                continue
            if ans.get("status") == "unknown" or not ans.get("bindings"):
                r.unknown.append(ans["question_id"])
                continue
            for raw in ans["bindings"]:
                try:
                    b = Binding.from_dict({**raw, "origin": "llm"})
                    validate(b, ctx)
                except (BindingError, TypeError, KeyError, AttributeError) as exc:
                    r.rejected.append((f"{raw.get('target') or raw.get('kind')}", str(exc)[:200]))
                    continue
                if b.kind == "unknown":
                    r.unknown.append(ans["question_id"])
                    continue
                if b.confidence < self.min_conf:
                    r.deferred_low_confidence.append(f"{b.target or b.kind} ({b.confidence})")
                    continue
                if b.target:
                    ctx.bound.add(b.target)
                accepted.append(b)
        r.accepted += len(accepted)
        return accepted
