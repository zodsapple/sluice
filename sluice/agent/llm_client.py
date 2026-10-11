"""LLM plumbing: an OpenAI-compatible HTTP client, a scripted stub for tests, and the LLM healer."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from sluice.agent.interface import LLMClient, LLMUnavailable, SpecPatch

SYSTEM_PROMPT = """You repair ETL pipeline specs. A run failed; you receive a structured diagnosis and the spec.
Reply with ONE JSON object: {"ops": [...], "rationale": "...", "confidence": 0..1}.
`ops` is an RFC 6902 subset (add / replace / remove) using JSON-pointer paths ('/' inside a key is '~1').
You may ONLY change paths under /locate, /map, /shape, /const. Never touch controls, rules, keys, template or measures,
never add python hooks, and never loosen validation - fix how the input is read, not what counts as correct.
Typical fixes: add the new header as an alias (path /map/<expected header>/aliases/-), set /locate/header,
or point /locate/sheet at the right sheet. If you cannot fix it safely reply {"ops": []}."""


class OpenAICompatClient:
    """Any /chat/completions endpoint. The key is read from the environment, never from a spec or log."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key_env: str = "SLUICE_LLM_API_KEY",
                 timeout: float = 60.0) -> None:
        self.key = os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("SLUICE_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("SLUICE_LLM_MODEL") or "gpt-4o-mini"
        self.timeout = timeout
        if not self.key:
            raise LLMUnavailable(f"Set {api_key_env} (or OPENAI_API_KEY) to enable the LLM healer")

    def complete(self, messages: list[dict[str, str]]) -> str:
        import requests

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.model, "messages": messages, "temperature": 0,
                  "response_format": {"type": "json_object"}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class StubLLMClient:
    """Returns scripted replies in order - for tests and offline demos."""

    def __init__(self, *replies: str | dict[str, Any]) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else '{"ops": []}'


def _redact(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    out = []
    for r in rows[:3]:
        out.append({k: ("<redacted>" if k in columns else (str(v)[:40] if v is not None else None))
                    for k, v in r.items()})
    return out


class LLMHealer:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def propose(self, diagnosis: dict[str, Any], spec: dict[str, Any], context: dict[str, Any]) -> SpecPatch | None:
        redact = spec.get("healing", {}).get("redact_columns", [])
        payload = {
            "diagnosis": {**diagnosis, "sample_rows": _redact(diagnosis.get("sample_rows", []), redact),
                          "details": {k: v for k, v in diagnosis.get("details", {}).items() if k != "traceback"}},
            "healable_spec": {k: spec.get(k) for k in ("locate", "map", "shape", "const")},
            "previous_attempts": context.get("rejected", []),
        }
        reply = self.client.complete([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ])
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return None
        try:
            doc = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not doc.get("ops"):
            return None
        return SpecPatch(doc["ops"], doc.get("rationale", ""), "llm", doc.get("confidence"))
