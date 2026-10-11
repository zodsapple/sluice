"""Precedent store: answers a person confirmed once, reused when the same kind of source arrives again.

An entry says: "for this target (its field names) and a sheet with THESE source columns, these bindings were confirmed".
A new sheet matches when its columns are (almost) the same set and the target is the same. Reuse is never blind: the bindings
go through the same validation as any answer, scopes get their preview, and the review list says they came from a precedent.
Only confirmed work is stored (a person approved it, or the checker / controls passed) - never an unreviewed LLM answer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sluice.infer.bindings import Binding
from sluice.infer.frame import Frame

NOT_STORED = {"source_sheet", "source_cell", "source_row", "source_record"}        # lineage is rebuilt by the rules each time


def column_set(frame: Frame) -> list[str]:
    """What identifies the layout of a sheet: its source columns (not the data, not the sheet name)."""
    return sorted(n for n, s in frame.sources.items() if s.kind in ("column", "group") and not n.startswith("_"))


def target_key(fields: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(fields)).encode()).hexdigest()[:12]


def jaccard(a: list[str], b: list[str]) -> float:
    x, y = set(a), set(b)
    return len(x & y) / len(x | y) if (x | y) else 0.0


@dataclass
class Entry:
    id: str
    target: str
    columns: list[str]
    bindings: list[dict[str, Any]]
    confirmed_by: str
    confirmed_at: str
    verified: str = ""                                   # how it was checked (checker passed, controls reconciled ...)
    label: str = ""                                      # free text: which source this came from


@dataclass
class Match:
    entry: Entry
    similarity: float
    bindings: list[Binding] = field(default_factory=list)


class PrecedentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries: list[Entry] = []
        if self.path.exists():
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [Entry(**e) for e in doc.get("entries", [])]

    def save_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": [e.__dict__ for e in self.entries]}, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, frame: Frame, target_fields: list[str], bindings: list[Binding], *, confirmed_by: str, verified: str,
            label: str = "") -> Entry:
        def stable(b: Binding) -> bool:
            """Confirmed work worth reusing: anything a person / model answered, plus the column matches the rules found (they
            rested on the example row, which will come from an older file next time). Not stored: lineage, run parameters, and
            rule constants / formulas (an example's report month would be wrong next month)."""
            if b.target in NOT_STORED or "parameter" in b.evidence:
                return False
            return b.origin != "rule" or (b.kind == "column" and b.target is not None and b.args.get("source") not in ("_src_sheet", "_src_row"))
        kept = [b.to_dict() for b in bindings if stable(b)]
        for d in kept:
            d["origin"] = "precedent"
            d["evidence"] = []
        if not kept:
            raise ValueError("nothing to store: only rule-made bindings")
        if not verified:
            raise ValueError("a precedent needs a verification note (what proved it right)")
        cols, tk = column_set(frame), target_key(target_fields)
        # the same layout confirmed again replaces the old entry
        self.entries = [e for e in self.entries if not (e.target == tk and e.columns == cols)]
        entry = Entry(id=f"p{len(self.entries) + 1:03d}-{hashlib.sha256(('|'.join(cols) + tk).encode()).hexdigest()[:6]}", target=tk,
                      columns=cols, bindings=kept, confirmed_by=confirmed_by,
                      confirmed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"), verified=verified, label=label)
        self.entries.append(entry)
        self.save_file()
        return entry

    def lookup(self, frame: Frame, target_fields: list[str], min_similarity: float = 0.9) -> Match | None:
        tk, cols = target_key(target_fields), column_set(frame)
        best: Match | None = None
        for e in self.entries:
            if e.target != tk:
                continue
            sim = jaccard(cols, e.columns)
            if sim >= min_similarity and (best is None or sim > best.similarity):
                best = Match(e, round(sim, 3), [])
        if best is not None:
            best.bindings = [Binding.from_dict({**d, "evidence": [f"precedent {best.entry.id} ({best.entry.label or best.entry.confirmed_at}, "
                                                                f"{best.entry.verified})"]}) for d in best.entry.bindings]
        return best
