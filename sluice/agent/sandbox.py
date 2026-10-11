"""Patch safety: whitelist paths, apply to a copy, re-validate, dry-run, regress.

The invariant that makes self-healing safe: a patch may change HOW the input is read and shaped
(`locate`, `map`, `shape`, `const`) but never WHAT counts as correct (`controls`, `rules`, `keys`,
`template`, `measures`, `healing`). The agent therefore cannot "heal" a failure by relaxing the check.
"""
from __future__ import annotations

import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sluice.agent.interface import SpecPatch
from sluice.spec.loader import spec_from_dict

ALLOWED_PREFIXES = ("/locate", "/map", "/shape", "/const")
MAX_OPS = 10


class PatchRejected(ValueError):
    pass


def _tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise PatchRejected(f"Bad path {path!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]


def _contains_python(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("op") == "hook" or "python" in value or any(_contains_python(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_python(v) for v in value)
    return False


def check_patch(patch: SpecPatch) -> None:
    if not patch.ops or len(patch.ops) > MAX_OPS:
        raise PatchRejected(f"Patch must have 1..{MAX_OPS} operations")
    for op in patch.ops:
        if op.get("op") not in ("add", "replace", "remove"):
            raise PatchRejected(f"Unsupported patch op {op.get('op')!r}")
        path = op.get("path", "")
        if not any(path == p or path.startswith(p + "/") for p in ALLOWED_PREFIXES):
            raise PatchRejected(f"Path {path!r} is outside the healable areas {ALLOWED_PREFIXES}")
        if _contains_python(op.get("value")):
            raise PatchRejected("Patches may not introduce hooks / python references")


def apply_patch(spec: dict[str, Any], patch: SpecPatch) -> dict[str, Any]:
    check_patch(patch)
    doc = deepcopy(spec)
    for op in patch.ops:
        toks = _tokens(op["path"])
        parent: Any = doc
        for i, t in enumerate(toks[:-1]):
            if isinstance(parent, dict) and t not in parent and toks[i + 1] == "-":
                parent[t] = []      # defaults are omitted from the canonical spec, e.g. empty `aliases`
            parent = parent[int(t)] if isinstance(parent, list) else parent[t]
        last = toks[-1]
        if isinstance(parent, list):
            if op["op"] == "add":
                parent.append(op["value"]) if last == "-" else parent.insert(int(last), op["value"])
            elif op["op"] == "replace":
                parent[int(last)] = op["value"]
            else:
                del parent[int(last)]
        else:
            if op["op"] == "remove":
                del parent[last]
            else:
                parent[last] = op["value"]
    return doc


@dataclass
class SandboxResult:
    ok: bool
    reason: str
    spec: dict[str, Any] | None = None


def validate_patch(
    spec: dict[str, Any], patch: SpecPatch, input_path: Path, template_path: Path | None,
    regression_inputs: list[Path] | None = None,
) -> SandboxResult:
    """Apply, schema-validate, then dry-run on the failing input and every regression input."""
    from sluice.engine.pipeline import run   # local import: engine does not depend on the agent

    try:
        new_spec = apply_patch(spec, patch)
        parsed = spec_from_dict(new_spec)
    except (PatchRejected, KeyError, IndexError, ValueError) as exc:
        return SandboxResult(False, f"Patch rejected: {exc}")
    with tempfile.TemporaryDirectory() as tmp:
        for path in [input_path, *(regression_inputs or [])]:
            res = run(parsed, path, tmp, template_path, write_output=False, write_logs=False)
            if not res.ok:
                why = res.diagnosis.message if res.diagnosis else "unknown"
                return SandboxResult(False, f"Dry-run on {Path(path).name} still fails: {why}")
    return SandboxResult(True, "Patch passes schema validation, dry-run and regression", new_spec)
