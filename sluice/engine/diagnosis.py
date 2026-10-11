"""Structured failure description: read by humans (feedback.md) and by the healing agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class EngineError(Exception):
    """A run-stopping problem. `code` is a reason code; `details` feeds the Diagnosis."""

    def __init__(self, code: str, message: str, *, stage: str = "etl", **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details


@dataclass
class Diagnosis:
    stage: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    source_headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    candidates: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # expected -> close actual headers
    controls: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
