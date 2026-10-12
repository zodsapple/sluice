"""A local, reviewable drafting workflow for users' own files; no benchmark dependencies."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sluice.agent.blind_resolve import draft_with_responder
from sluice.agent.prompting import build_packet, render_single_prompt
from sluice.agent.review import review
from sluice.engine import run
from sluice.infer.bindings import Binding, TargetField
from sluice.infer.blind import Target, _profile, draft_blind
from sluice.infer.precedent_store import PrecedentStore
from sluice.spec import load_spec, spec_from_dict


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_target(path: str | Path) -> Target:
    doc = _read(path)
    fields = {}
    for f in doc["fields"]:
        if f["name"] in fields:
            raise ValueError(f"Duplicate schema field {f['name']!r}")
        kind = {"text": "string", "number": "decimal", "int": "integer"}.get(f["type"], f["type"])
        if kind not in ("string", "decimal", "integer", "date"):
            raise ValueError(f"Unsupported schema type {kind!r}")
        fields[f["name"]] = TargetField(f["name"], kind, f.get("enum", []), not f.get("nullable", True),
                                      f.get("description", ""), f.get("lexicon", {}))
    if not fields:
        raise ValueError("The schema needs at least one field")
    return Target(fields, doc.get("rules", []), mode=doc.get("mode", "record"), example=doc.get("example_row", {}))


def _build(config, replies=None):
    target = load_target(config["schema"])
    kwargs = dict(name=config["name"], filename=config["filename"],
                  store=PrecedentStore(config["store"]) if config.get("store") else None)
    if replies is None:
        bindings = {sheet: [Binding.from_dict(b) for b in values] for sheet, values in config.get("bindings", {}).items()}
        draft = draft_blind(config["input"], config["sheets"], target, config["params"], answers=bindings, **kwargs)
        items = review(draft.frames, draft.bindings, target.fields, example=target.example)
        responder = None
    else:
        class SavedReplies:
            def complete(self, messages):
                packet = json.loads(next(m["content"] for m in reversed(messages)
                                         if m["role"] == "user" and m["content"].startswith("{")))
                return replies.get(packet["sheet"], '{"answers": []}')
        draft, report, _ = draft_with_responder(config["input"], config["sheets"], target, config["params"],
                                              llm=SavedReplies(), verify=False, **kwargs)
        items, responder = report.review, asdict(report)
    schema = _read(config["schema"])
    for key in ("keys", "controls", "output_rules"):
        if key in schema:
            draft.spec["rules" if key == "output_rules" else key] = schema[key]
    for name, f in target.fields.items():
        expr = draft.spec["template"]["columns"][name]
        if isinstance(expr, str):
            expr = {"col": expr}
        draft.spec["template"]["columns"][name] = {**expr, "required": f.required}
    spec_from_dict(draft.spec)
    return draft, items, target, responder


def _save_draft(work, config, draft, items, target, responder):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "approval.json").unlink(missing_ok=True)
    _write(work / "context.json", config)
    (work / "spec.yaml").write_text(yaml.safe_dump(draft.spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write(work / "questions.json", [asdict(q) for q in draft.questions])
    _write(work / "review.json", {"items": [asdict(i) for i in items],
                                "rejected": [(sh, b.to_dict(), why) for sh, b, why in draft.rejected],
                                "responder": responder})
    _write(work / "bindings.json", [[b.to_dict() for b in bs] for bs in draft.bindings])
    prompts = work / "prompts"
    prompts.mkdir(exist_ok=True)
    for old in prompts.glob("*.txt"):
        old.unlink()
    for index, (fr, bs) in enumerate(zip(draft.frames, draft.bindings), 1):
        qs = [q for q in draft.questions if q.frame == fr.sheet]
        if not qs:
            continue
        asked = {t for q in qs for t in q.targets}
        bound = {b.target: f"{b.kind} {b.args}" for b in bs if b.target and b.target not in asked}
        packet = build_packet(fr, qs, target.fields, bound, conventions=target.notes, example=target.example)
        (prompts / f"{index:02}.txt").write_text(render_single_prompt(packet), encoding="utf-8")
    lines = ["# Review the draft", "", "Validation checks declared constraints; a person must confirm the mapping and scope.", "",
             f"Open questions: {len(draft.questions)}. Rejected bindings: {len(draft.rejected)}.", ""]
    lines += ["- " + i.line() for i in items]
    lines += ["", "## Open questions", ""] + [f"- {q.frame}: {q.reason} ({q.id})" for q in draft.questions]
    if not draft.spec.get("controls"):
        lines += ["", "No reconciliation controls declared. Add controls to spec.yaml before approval."]
    (work / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare(input_path, schema_path, work, *, sheets=None, params=None, filename="result.csv", store=None, bindings=None):
    config = {"input": str(Path(input_path).resolve()), "schema": str(Path(schema_path).resolve()),
              "sheets": sheets or list(_profile(input_path).sheets), "params": params or {},
              "name": Path(input_path).stem, "filename": filename,
              "store": str(Path(store).resolve()) if store else None}
    if bindings:
        config["bindings"] = bindings
    draft, items, target, responder = _build(config)
    _save_draft(work, config, draft, items, target, responder)
    return draft


def answer(work, replies):
    config = _read(Path(work) / "context.json")
    draft, items, target, responder = _build(config, replies)
    _save_draft(work, config, draft, items, target, responder)
    return draft


def fingerprint(work):
    work = Path(work)
    config = _read(work / "context.json")
    paths = {"input": Path(config["input"]), "schema": Path(config["schema"]),
             "spec": work / "spec.yaml", "context": work / "context.json"}
    parsed = load_spec(work / "spec.yaml")
    if parsed.template.path:
        paths["template"] = Path(parsed.template.path)
    for src in parsed.sources:
        if src.file:
            paths[f"source:{src.name}"] = Path(src.file)
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    return {name: digest(path) for name, path in paths.items()}


def inspect_work(work):
    work = Path(work)
    config = _read(work / "context.json")
    return run(load_spec(work / "spec.yaml"), config["input"], work / "output", write_output=False, write_logs=False)


def approve(work, *, by, reason):
    if not by.strip() or not reason.strip():
        raise ValueError("Approval needs a reviewer name and a written reason")
    work = Path(work)
    spec = load_spec(work / "spec.yaml")
    if not any(c.on_fail == "halt" for c in spec.controls):
        raise ValueError("Add at least one halting reconciliation control before approving")
    result = inspect_work(work)
    if not result.ok:
        raise ValueError(f"Draft fails validation: {result.diagnosis.message}")
    record = {"fingerprint": fingerprint(work), "by": by, "reason": reason,
              "approved_at": datetime.now(timezone.utc).isoformat()}
    _write(work / "approval.json", record)
    return result


def convert(work, out):
    work = Path(work)
    if not (work / "approval.json").exists():
        raise ValueError("Review and approve this workspace before converting")
    if _read(work / "approval.json")["fingerprint"] != fingerprint(work):
        raise ValueError("Input, schema or spec changed since approval; review and approve again")
    config = _read(work / "context.json")
    return run(load_spec(work / "spec.yaml"), config["input"], out)
