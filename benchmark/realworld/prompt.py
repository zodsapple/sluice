"""Copy-paste workflow for the realworld pack with a chat-only model (no API, no files).

    py benchmark/realworld/prompt.py dump  <case> [--out DIR]
    py benchmark/realworld/prompt.py apply <case> --reply "<sheet>=<file>" [--reply ...]

The packet carries: the template's field names/types, its written rules, its one example row, the structure of the sheet
(neutral names, a few values per source) and what the rules already bound. Never the file.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(errors="replace")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "benchmark" / "blind"))

import rw  # noqa: E402
from prompt_cli import ReplayLLM  # noqa: E402

from sluice.agent.blind_resolve import draft_with_responder  # noqa: E402
from sluice.agent.prompting import build_packet, est_tokens, render_single_prompt  # noqa: E402
from sluice.engine import run  # noqa: E402
from sluice.infer.blind import _profile, draft_blind  # noqa: E402
from sluice.spec import spec_from_dict  # noqa: E402


def setup(cid: str):
    case = rw.load_case(cid)
    target = rw.target_of(case)
    target.mode = "stats" if "value" in target.fields else "record"
    target.example = case["example"]
    sheets = rw.scope_sheets(cid, list(_profile(case["raw"]).sheets))
    return case, target, sheets


def cmd_dump(a: argparse.Namespace) -> None:
    case, target, sheets = setup(a.case)
    res = draft_blind(case["raw"], sheets, target, {"source_id": a.case}, name=a.case, filename=f"{a.case}.csv")
    out = Path(a.out or Path(tempfile.gettempdir()) / "sluice_prompts")
    out.mkdir(parents=True, exist_ok=True)
    seen: set[tuple] = set()
    for fr, bs in zip(res.frames, res.bindings):
        sig = tuple(sorted(n for n, s in fr.sources.items() if s.kind == "column")) if target.mode == "record" else (fr.sheet,)
        if sig in seen:
            continue                                  # the same table in another sheet: one question covers it
        seen.add(sig)
        qs = [q for q in res.questions if q.frame == fr.sheet]
        bound = {b.target: f"{b.kind} {b.args.get('source') or b.args.get('value') or b.args.get('template') or ''}"[:60]
                 for b in bs if b.target and b.target not in ("source_sheet", "source_cell", "source_row")}
        text = render_single_prompt(build_packet(fr, qs, target.fields, bound, conventions=target.notes, example=target.example))
        p = out / f"{a.case}__{fr.sheet.replace('/', '_')}.prompt.txt"
        p.write_text(text, encoding="utf-8")
        print(f"{p}  ({len(text)} chars ~{est_tokens(text)} tokens, {len(qs)} questions)")


def cmd_apply(a: argparse.Namespace) -> None:
    case, target, sheets = setup(a.case)
    replies = {}
    for item in a.reply:
        sheet, _, file = item.partition("=")
        replies[sheet] = Path(file).read_text(encoding="utf-8")
    from sluice.infer.precedent_store import PrecedentStore
    store = PrecedentStore(a.store) if a.store else None
    res, rep, _ = draft_with_responder(case["raw"], sheets, target, {"source_id": a.case}, name=a.case, filename=f"{a.case}.csv",
                                       llm=ReplayLLM(replies), store=store)
    if res.precedents:
        print("reused precedents:", sorted({f"{m.entry.id}@{m.similarity}" for _, m in res.precedents}))
    print(f"accepted {rep.accepted} | rejected {len(rep.rejected)} | unknown {len(rep.unknown)} | unanswered {len(rep.unanswered)}")
    for who, why in rep.rejected[:12]:
        print(f"  rejected {who}: {why}")
    out = Path(tempfile.mkdtemp(prefix=f"rwp_{a.case}_"))
    r = run(spec_from_dict(res.spec), case["raw"], out, write_logs=False)
    if not r.ok:
        print("RUN-FAIL:", r.diagnosis.message[:300] if r.diagnosis else "?")
        return
    v = rw.score(a.case, r.output_path)
    if a.save and store is not None and (v.get("passed") or a.confirm):
        for fr, bs in zip(res.frames, res.bindings):
            if any(b.origin != "rule" for b in bs):
                store.add(fr, list(target.fields), bs, confirmed_by=a.by, verified="pack checker PASS" if v.get("passed") else a.confirm, label=a.case)
        print("saved to the precedent store" + ("" if v.get("passed") else f" (checker did not pass; confirmed by {a.by}: {a.confirm})"))
    print("checker:", "PASS" if v.get("passed") else "FAIL", {k: str(x)[:60] for k, x in v.items() if k in ("missing_key_count", "extra_key_count", "mismatched_rows")})
    f = rw.field_report(a.case, r.output_path)
    print(f"fields right {f['fields_right']}/{f['fields_total']} | rows {f['rows_actual']}/{f['rows_expected']} | still wrong: {f['wrong_fields']}")
    print(f"open questions for the human: {len(res.questions)}")
    print(f"\nREVIEW BEFORE TRUSTING ({len(rep.review)} item(s), most risky first):")
    for item in rep.review[:20]:
        print("  " + item.line()[:230])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("case")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_dump)
    p = sub.add_parser("apply")
    p.add_argument("case")
    p.add_argument("--reply", action="append", required=True)
    p.add_argument("--store", help="precedent store (json): reuse what was confirmed before")
    p.add_argument("--save", action="store_true", help="after the checker passes, store the non-rule answers as precedents")
    p.add_argument("--confirm", default="", help="save although the checker did not pass: a person's reason, recorded in the store")
    p.add_argument("--by", default="human", help="who confirms the answers (recorded in the store)")
    p.set_defaults(fn=cmd_apply)
    args = ap.parse_args()
    args.fn(args)
