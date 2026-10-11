"""The agent seam. A Healer reads a Diagnosis and proposes a SpecPatch - never touches data or engine code."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class SpecPatch:
    """RFC 6902 subset: [{"op": "add"|"replace"|"remove", "path": "/map/Foo/aliases/-", "value": ...}]."""
    ops: list[dict[str, Any]]
    rationale: str = ""
    source: str = "unknown"          # which healer produced it
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ops": self.ops, "rationale": self.rationale, "source": self.source, "confidence": self.confidence}


class Healer(Protocol):
    def propose(self, diagnosis: dict[str, Any], spec: dict[str, Any], context: dict[str, Any]) -> SpecPatch | None:
        """Return a patch to try, or None when this healer has nothing to offer."""
        ...


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return the model's reply text (expected to be a JSON object)."""
        ...


class LLMUnavailable(RuntimeError):
    """No API key / endpoint configured; the LLM healer is simply skipped."""
