"""Rules -> open questions -> responder (LLM) -> validation -> verification run -> what stays for the human."""
from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from sluice.agent.prompting import LLM, ResolveReport, Resolver
from sluice.agent.review import review
from sluice.engine import run
from sluice.infer.bindings import Binding
from sluice.infer.blind import BlindResult, Question, Target, draft_blind
from sluice.spec import spec_from_dict


def _describe(b: Binding) -> str:
    a = b.args
    return f"{b.kind} {a.get('source') or a.get('value') or a.get('template') or a.get('cell') or ''}"[:70]


def runs_ok(spec: dict[str, Any], path: Path) -> tuple[bool, str]:
    try:
        r = run(spec_from_dict(spec), path, tempfile.mkdtemp(), write_logs=False)
    except Exception as exc:       # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    if not r.ok:
        return False, (r.diagnosis.message if r.diagnosis else "failed")[:200]
    if r.rows_out == 0:
        return False, "the draft produces no rows"
    return True, ""


def draft_with_responder(path: str | Path, sheets: list[str], target: Target, params: dict[str, str], *, name: str,
                         filename: str, llm: LLM | None, resolver: Resolver | None = None, verify: bool = True,
                         precedents: dict[str, list[str]] | None = None, store: Any = None
                         ) -> tuple[BlindResult, ResolveReport, dict[str, list[Binding]]]:
    path = Path(path)
    resolver = resolver or Resolver(llm)
    base = draft_blind(path, sheets, target, params, name=name, filename=filename, store=store)
    by_sheet: dict[str, list[Question]] = defaultdict(list)
    for q in base.questions:
        by_sheet[q.frame].append(q)
    answers: dict[str, list[Binding]] = {}
    asked_family: dict[tuple, str] = {}
    for fr, bs in zip(base.frames, base.bindings):
        if target.mode == "record":                      # sheets with the same columns are one table in pieces: ask once
            sig = tuple(sorted(n for n, s_ in fr.sources.items() if s_.kind == "column"))
            if sig in asked_family and asked_family[sig] in answers:
                answers[fr.sheet] = list(answers[asked_family[sig]])
                continue
            asked_family.setdefault(sig, fr.sheet)
        bound = {b.target for b in bs if b.target}
        desc = {b.target: _describe(b) for b in bs if b.target and b.target not in ("source_sheet", "source_cell")}
        got = resolver.answer_frame(fr, by_sheet.get(fr.sheet, []), target.fields, desc,
                                    lambda fr=fr, bound=bound: fr.context(target.fields, set(bound)), target.notes, precedents,
                                    target.example or None)
        if got:
            answers[fr.sheet] = got

    def build() -> BlindResult:
        return draft_blind(path, sheets, target, params, name=name, filename=filename, answers=answers, store=store)

    def every_sheet_has_rows(current: dict[str, list[Binding]]) -> tuple[bool, str]:
        for sh in sheets:
            one = draft_blind(path, [sh], target, params, name=name, filename=filename, answers={sh: current.get(sh, [])}, store=store)
            ok1, why1 = runs_ok(one.spec, path)
            if not ok1:
                return False, f"sheet {sh}: {why1}"
        return True, ""

    res = build()
    resolver.report.rejected += [(f"{sh}:{b.target or b.kind}", why) for sh, b, why in res.rejected]
    if verify and answers:
        ok, why = runs_ok(res.spec, path)
        if ok:
            ok, why = every_sheet_has_rows(answers)
        guard = 0
        while not ok and guard < 8:           # revert the smallest set of answers that breaks the run
            guard += 1
            culprit = None
            for sh, bs in answers.items():
                for b in list(bs):
                    trial = {s: [x for x in v if x is not b] for s, v in answers.items()}
                    t = draft_blind(path, sheets, target, params, name=name, filename=filename, answers=trial, store=store)
                    if runs_ok(t.spec, path)[0] and every_sheet_has_rows(trial)[0]:
                        culprit = (sh, b)
                        break
                if culprit:
                    break
            if not culprit:
                culprit = next(((sh, bs[-1]) for sh, bs in answers.items() if bs), None)
            if not culprit:
                break
            sh, b = culprit
            answers[sh] = [x for x in answers[sh] if x is not b]
            resolver.report.reverted_by_verification.append(f"{sh}:{b.target or b.kind} ({why[:80]})")
            res = build()
            ok, why = runs_ok(res.spec, path)
            if ok:
                ok, why = every_sheet_has_rows(answers)
    resolver.report.review = review(res.frames, res.bindings, target.fields, example=target.example or None)
    return res, resolver.report, answers
