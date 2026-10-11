"""Self-healing loop: run -> diagnose -> (deterministic fix, then LLM) -> sandbox -> propose or apply."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sluice.agent.interface import Healer, LLMUnavailable, SpecPatch
from sluice.agent.llm_client import LLMHealer, OpenAICompatClient
from sluice.agent.sandbox import validate_patch
from sluice.engine.pipeline import RunResult, run
from sluice.spec.loader import load_spec_dict, spec_from_dict, spec_to_dict


def _esc(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


class DeterministicHealer:
    """Cheap, explainable fixes that need no model: renamed headers and wrong sheets."""

    def propose(self, diagnosis: dict[str, Any], spec: dict[str, Any], context: dict[str, Any]) -> SpecPatch | None:
        code = diagnosis.get("code")
        if code == "missing_required_column":
            ops = []
            for want, cands in diagnosis.get("candidates", {}).items():
                if want in spec.get("map", {}) and cands and cands[0][1] >= 0.6:
                    ops.append({"op": "add", "path": f"/map/{_esc(want)}/aliases/-", "value": cands[0][0]})
            if ops:
                return SpecPatch(ops, "Closest input header added as alias for each missing column", "deterministic", 0.6)
        if code == "sheet_not_found":
            return SpecPatch([{"op": "replace", "path": "/locate/sheet", "value": {"strategy": "best_header"}}],
                             "Named sheet is gone; fall back to picking the sheet whose header best matches",
                             "deterministic", 0.5)
        return None


class CompositeHealer:
    def __init__(self, *healers: Healer) -> None:
        self.healers = healers

    def propose(self, diagnosis: dict[str, Any], spec: dict[str, Any], context: dict[str, Any]) -> SpecPatch | None:
        for h in self.healers:
            patch = h.propose(diagnosis, spec, context)
            if patch and patch.ops:
                return patch
        return None


def default_healer() -> Healer:
    """Deterministic fixes always; the LLM healer only when an API key is configured."""
    healers: list[Healer] = [DeterministicHealer()]
    try:
        healers.append(LLMHealer(OpenAICompatClient()))
    except LLMUnavailable:
        pass
    return CompositeHealer(*healers)


@dataclass
class HealOutcome:
    result: RunResult
    status: str                     # clean | healed | proposed | unhealed
    attempts: list[dict[str, Any]] = field(default_factory=list)
    patch_path: str | None = None


def heal(
    spec_path: str | Path, input_path: str | Path, output_dir: str | Path, template_path: str | Path | None = None,
    *, healer: Healer | None = None, mode: str | None = None, max_attempts: int | None = None,
    regression_inputs: list[Path] | None = None, expect: dict[str, float] | None = None,
) -> HealOutcome:
    spec_path = Path(spec_path)
    spec_dict = spec_to_dict(spec_from_dict(load_spec_dict(spec_path)))
    cfg = spec_dict.get("healing", {})
    mode = mode or cfg.get("mode", "propose-only")
    limit = max_attempts if max_attempts is not None else cfg.get("max_attempts", 2)
    healer = healer or default_healer()

    result = run(spec_from_dict(spec_dict), input_path, output_dir, template_path, expect=expect)
    outcome = HealOutcome(result, "clean")
    rejected: list[dict[str, Any]] = []
    for attempt in range(1, limit + 1):
        if result.ok:
            return outcome
        diag = result.diagnosis.to_dict() if result.diagnosis else {}
        if diag.get("code") in ("validation_failed", "reconcile_failed", "declared_mismatch"):
            outcome.status = "unhealed"          # the checks are the source of truth; never patch around them
            outcome.attempts.append({"attempt": attempt, "skipped": "validation failures are not auto-healable"})
            return outcome
        patch = healer.propose(diag, spec_dict, {"rejected": rejected})
        if patch is None:
            outcome.status = "unhealed"
            outcome.attempts.append({"attempt": attempt, "skipped": "no healer had a proposal"})
            return outcome
        verdict = validate_patch(spec_dict, patch, Path(input_path), Path(template_path) if template_path else None,
                                 regression_inputs)
        outcome.attempts.append({"attempt": attempt, "patch": patch.to_dict(), "accepted": verdict.ok,
                                 "reason": verdict.reason})
        if not verdict.ok:
            rejected.append({"patch": patch.ops, "reason": verdict.reason})
            continue
        if mode == "propose-only":
            pp = spec_path.with_suffix(".proposed_patch.json")
            pp.write_text(json.dumps({"input": str(input_path), "patch": patch.to_dict(),
                                      "verified": verdict.reason}, indent=2), encoding="utf-8")
            outcome.status, outcome.patch_path = "proposed", str(pp)
            return outcome
        shutil.copy2(spec_path, spec_path.with_suffix(spec_path.suffix + ".bak"))
        spec_path.write_text(yaml.safe_dump(verdict.spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        spec_dict = verdict.spec
        result = run(spec_from_dict(spec_dict), input_path, output_dir, template_path, expect=expect)
        outcome.result, outcome.status = result, "healed"
        if result.ok:
            return outcome
    outcome.result = result
    if not result.ok:
        outcome.status = "unhealed"
    return outcome
