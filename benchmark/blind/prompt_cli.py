"""Copy-paste workflow for a model you can only talk to in a chat window (no API, no files).

    py benchmark/blind/prompt_cli.py dump  fr  [--out DIR]            -> one .prompt.txt per sheet: paste it into the chat
    py benchmark/blind/prompt_cli.py apply fr  --reply "Figure 1=reply.txt" [--reply "sheet=file" ...]
                                                                       -> validate the pasted reply, apply it, run, score

The reply file may contain the model's text exactly as copied (code fences and prose around the JSON are tolerated).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmark"))

import run_blind as rb  # noqa: E402
import run_blind_llm as rl  # noqa: E402

from sluice.agent.blind_resolve import draft_with_responder  # noqa: E402
from sluice.agent.prompting import build_packet, est_tokens, render_single_prompt  # noqa: E402
from sluice.infer.blind import draft_blind  # noqa: E402


class ReplayLLM:
    """Hands back the pasted reply for the sheet in the packet. A second call for the same sheet (repair / nudge) gets an
    empty answer list, because there is nobody to ask - the item then simply stays with the human."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies, self.seen = replies, {}

    def complete(self, messages: list[dict[str, str]]) -> str:
        try:
            sheet = json.loads(messages[-1]["content"])["sheet"]
        except (json.JSONDecodeError, KeyError):                    # a repair message: find the packet before it
            sheet = next((json.loads(m["content"])["sheet"] for m in reversed(messages)
                          if m["role"] == "user" and m["content"].startswith("{") and '"sheet"' in m["content"]), None)
        self.seen[sheet] = self.seen.get(sheet, 0) + 1
        return self.replies.get(sheet, '{"answers": []}') if self.seen[sheet] == 1 else '{"answers": []}'


def cmd_dump(a: argparse.Namespace) -> None:
    cfg = rb.SOURCES[a.src]
    target = rb.Target.from_schema(rb.SCHEMA)
    res = draft_blind(cfg["path"], cfg["sheets"], target, cfg["params"], name="x", filename="x.csv")
    out = Path(a.out or Path(tempfile.gettempdir()) / "sluice_prompts")
    out.mkdir(parents=True, exist_ok=True)
    for fr, bs in zip(res.frames, res.bindings):
        qs = [q for q in res.questions if q.frame == fr.sheet]
        bound = {b.target: f"{b.kind} {b.args.get('source') or b.args.get('value') or b.args.get('template') or ''}"[:60]
                 for b in bs if b.target and b.target not in ("source_sheet", "source_cell")}
        text = render_single_prompt(build_packet(fr, qs, target.fields, bound, conventions=target.notes,
                                                 precedents=rb.precedents_for(a.src)))
        p = out / f"{a.src}__{fr.sheet}.prompt.txt"
        p.write_text(text, encoding="utf-8")
        print(f"{p}  ({len(text)} chars ~{est_tokens(text)} tokens, {len(qs)} questions)")


def cmd_apply(a: argparse.Namespace) -> None:
    cfg = rb.SOURCES[a.src]
    target = rb.Target.from_schema(rb.SCHEMA)
    replies = {}
    for item in a.reply:
        sheet, _, file = item.partition("=")
        replies[sheet] = Path(file).read_text(encoding="utf-8")
    llm = ReplayLLM(replies)
    res, rep, answers = draft_with_responder(cfg["path"], cfg["sheets"], target, cfg["params"], name=f"b_{a.src}",
                                             filename=f"{a.src}.csv", llm=llm, precedents=rb.precedents_for(a.src))
    print(f"accepted {rep.accepted} | rejected {len(rep.rejected)} | deferred (low confidence) {len(rep.deferred_low_confidence)}"
          f" | unknown {len(rep.unknown)} | unanswered {len(rep.unanswered)} | reverted {len(rep.reverted_by_verification)}")
    for who, why in rep.rejected[:12]:
        print(f"  rejected  {who}: {why}")
    for d in rep.deferred_low_confidence[:8]:
        print(f"  deferred  {d}")
    if rep.unanswered:
        print(f"  unanswered question ids: {rep.unanswered[:20]}")
    s = rl.score(a.src, res.spec)
    print("result:", s["status"], "| precision", s.get("precision"), "| row recall", s.get("recall"))
    print("on the rows it did produce:", s.get("on_produced_rows"))
    print("  right :", s.get("right_on_matched"))
    print("  wrong or missing:", [f for f in s.get("filled_fields", []) if f not in s.get("right_on_matched", [])])
    print(f"open questions for the human: {len(res.questions)}")
    print(f"\nREVIEW BEFORE TRUSTING THIS DRAFT ({len(rep.review)} item(s), most risky first):")
    for item in rep.review:
        print("  " + item.line())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("src")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_dump)
    p = sub.add_parser("apply")
    p.add_argument("src")
    p.add_argument("--reply", action="append", required=True, help='"<sheet>=<file with the pasted reply>"')
    p.set_defaults(fn=cmd_apply)
    args = ap.parse_args()
    args.fn(args)
