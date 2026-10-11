"""YAML loading with `extends` inheritance; canonical dict round-trip for patching."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from sluice.spec.models import PipelineSpec


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)   # lists and scalars are replaced, not concatenated
    return out


def load_spec_dict(path: str | Path, _seen: tuple[Path, ...] = ()) -> dict[str, Any]:
    path = Path(path).resolve()
    if path in _seen:
        raise ValueError(f"Circular 'extends' at {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = doc.pop("extends", None)
    if parent:
        base = load_spec_dict(path.parent / parent, (*_seen, path))
        doc = _merge(base, doc)
    return doc


def spec_from_dict(doc: dict[str, Any]) -> PipelineSpec:
    return PipelineSpec.model_validate(doc)


def load_spec(path: str | Path) -> PipelineSpec:
    return spec_from_dict(load_spec_dict(path))


def spec_to_dict(spec: PipelineSpec) -> dict[str, Any]:
    """Canonical dict (shorthands expanded, defaults dropped) - the form the healer patches."""
    return spec.model_dump(mode="python", exclude_defaults=True)
