"""Human-readable feedback for a run: why it failed (or what was dropped) and what to do about it."""
from __future__ import annotations

from collections import Counter
from typing import Any


def render_markdown(payload: dict[str, Any]) -> str:
    L: list[str] = []
    status = payload["status"].upper()
    L.append(f"# ETL run {status}: {payload['spec']}")
    L.append(f"- Input: `{payload['input_path']}`  (sheet `{payload.get('sheet', '')}`)")
    L.append(f"- Rows in/out: {payload.get('rows_in', 0)} / {payload.get('rows_out', 0)}"
             f" · dropped value: {payload.get('dropped_amount', 0):,.2f}")
    if payload.get("output_path"):
        L.append(f"- Output: `{payload['output_path']}`")
    diag = payload.get("diagnosis")
    if diag:
        L += ["", "## Why it failed", f"**{diag['code']}** at stage `{diag['stage']}`: {diag['message']}"]
        if diag.get("candidates"):
            L.append("\nClosest input headers for what the spec expects:")
            for want, cands in diag["candidates"].items():
                L.append(f"- `{want}` -> " + (", ".join(f"`{h}` ({r})" for h, r in cands) or "no close match"))
        if diag.get("details", {}).get("preview_rows"):
            L.append("\nFirst rows of the sheet:")
            for row in diag["details"]["preview_rows"]:
                L.append("- " + " | ".join(c or "·" for c in row))
        for s in diag.get("suggestions", []):
            L.append(f"- Suggestion: {s}")
    ctrl = payload.get("controls", [])
    if ctrl:
        L += ["", "## Controls", "| control | kind | source | dropped | output | declared | result |", "|---|---|---|---|---|---|---|"]
        for c in ctrl:
            res = "PASS" if c["passed"] else f"{'FAIL' if c['on_fail'] == 'halt' else 'WARN'}: " + "; ".join(c["reasons"])
            L.append(f"| {c['name']} | {c['kind']} | {c['source']:,.2f} | {c['dropped']:,.2f} | {c['output']:,.2f} | "
                     f"{'' if c['expected'] is None else format(c['expected'], ',.2f')} | {res} |")
            if not c["passed"] and c.get("hint"):
                L.append(f"\n> Hint for `{c['name']}`: {c['hint']}")
    issues = payload.get("issues", [])
    if issues:
        counts = Counter((i["severity"], i["reason_code"], i["action"]) for i in issues)
        L += ["", "## Issues", "| severity | reason | action | count |", "|---|---|---|---|"]
        for (sev, code, act), n in sorted(counts.items()):
            L.append(f"| {sev} | {code} | {act} | {n} |")
        L += ["", "First examples:"]
        for i in issues[:10]:
            L.append(f"- [{i['severity']}] {i['reason_code']} · sheet `{i['source_sheet']}` row {i['source_row']}"
                     f" · {i['message']}")
    traces = payload.get("traces", [])
    if traces:
        L += ["", "## Step trace", "| step | rows in | rows out | amount in | amount out | dropped |", "|---|---|---|---|---|---|"]
        for t in traces:
            L.append(f"| {t['step']} | {t['rows_in']} | {t['rows_out']} | {t['amount_in']:,.2f} | "
                     f"{t['amount_out']:,.2f} | {t['dropped_rows']} rows / {t['dropped_amount']:,.2f} |")
    return "\n".join(L) + "\n"
