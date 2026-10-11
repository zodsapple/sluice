"""Test doubles for the responder: an oracle (answers from the gold file, by reading only the packet) and failure modes."""
from __future__ import annotations

import copy
import json
from typing import Any

from sluice.agent.interface import LLMUnavailable


class OracleLLM:
    """Answers each question in the packet from the gold bindings - what a perfect model would return."""

    def __init__(self, gold: dict[str, list[dict[str, Any]]]) -> None:
        self.gold = gold
        self.calls = 0
        self.last_packet: dict[str, Any] | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        packet = json.loads(messages[-1]["content"])
        self.last_packet = packet
        bindings = self.gold.get(packet["sheet"], [])
        answers = []
        for q in packet["questions"]:
            if q.get("kind") == "scope":
                kind = q["id"].split(":")[1]
                got = [b for b in bindings if b["kind"] == kind]
            else:
                got = [b for b in bindings if b.get("target") in q["targets"]]
            real = [b for b in got if b["kind"] != "unknown"]
            if real:
                answers.append({"question_id": q["id"], "status": "answered", "bindings": copy.deepcopy(real)})
            else:
                answers.append({"question_id": q["id"], "status": "unknown", "bindings": []})
        return json.dumps({"answers": answers}, ensure_ascii=False)


class Chaos:
    """Wraps a responder and breaks its replies in one specific way."""

    def __init__(self, inner: OracleLLM, mode: str) -> None:
        self.inner, self.mode, self.calls = inner, mode, 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        repair = "Invalid reply" in messages[-1]["content"] or "Your reply answers" in messages[-1]["content"]
        if repair:                                   # a real model answers the original packet again
            messages = messages[:-2]
        if self.mode == "unavailable":
            raise LLMUnavailable("no API key configured")
        if self.mode == "timeout":
            raise TimeoutError("read timed out")
        if self.mode == "empty":
            return '{"answers": []}'
        if self.mode == "empty_once":
            return '{"answers": []}' if self.calls == 1 else self.inner.complete(messages[:-2] if "Your reply answers" in messages[-1]["content"] else messages)
        if self.mode == "garbage":
            return "I'm sorry, I cannot help with that."
        if self.mode == "malformed_once":
            text = self.inner.complete(messages)
            return text if repair or self.calls > 1 else "Sure! Here is the mapping:\n" + text[: len(text) // 2]
        doc = json.loads(self.inner.complete(messages))
        for a in doc["answers"]:
            for b in a["bindings"]:
                if self.mode == "low_confidence":
                    b["confidence"] = 0.3
                elif self.mode == "invented_source" and b["kind"] == "column":
                    b["args"]["source"] = "region_total_2"
                elif self.mode == "bad_enum" and b["kind"] == "constant" and b.get("target") in ("period_basis",):
                    b["args"]["value"] = "snapshot"
                elif self.mode == "invented_cell" and b["kind"] == "sheet_cell":
                    b["args"]["cell"] = "Z99"
                elif self.mode == "swap_sex" and b.get("target") == "sex" and b["args"].get("mapping"):
                    m = b["args"]["mapping"]
                    b["args"]["mapping"] = {k: ({"male": "female", "female": "male"}.get(v, v)) for k, v in m.items()}
                elif self.mode == "wrong_scope" and b["kind"] == "row_scope":
                    b["args"]["conditions"] = [{"column": b["args"]["conditions"][0]["column"], "equals": "NO SUCH VALUE"}]
        return json.dumps(doc, ensure_ascii=False)
