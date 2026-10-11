"""Rules vs rules + responder on the blind sources, with a perfect responder and with broken ones.

    py benchmark/run_blind_llm.py [nhs de fr] [--chaos]
No real model is called: the responder is an oracle that reads ONLY the packet, plus failure-mode wrappers. This measures the
plumbing (is the packet sufficient? what does a wrong or missing answer cost?) - not model quality.
"""
from __future__ import annotations

import os
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent / "blind"))

import run_blind as rb  # noqa: E402
from fakes import Chaos, OracleLLM  # noqa: E402

from sluice.agent.blind_resolve import draft_with_responder  # noqa: E402

from sluice.engine import run  # noqa: E402
from sluice.spec import spec_from_dict  # noqa: E402

import subprocess  # noqa: E402


def score(src: str, spec: dict) -> dict:
    cfg = rb.SOURCES[src]
    out = Path(tempfile.mkdtemp(prefix=f"llm_{src}_"))
    r = run(spec_from_dict(spec), cfg["path"], out, write_logs=False)
    if not r.ok:
        return {"status": "RUN-FAIL", "why": r.diagnosis.message[:160] if r.diagnosis else "?"}
    target = rb.Target.from_schema(rb.SCHEMA)
    fields = [f for f, t in target.fields.items() if t.required]
    sc = rb.field_scores(cfg["expected"], Path(r.output_path), fields)
    chk = json.loads(subprocess.run([sys.executable, str(rb.PACK / "check_output.py"), "--case", cfg["scorer"], "--actual", r.output_path],
                                    capture_output=True, text=True, encoding="utf-8", cwd=rb.PACK,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}).stdout or "{}")
    right = [f for f, v in sc["fields"].items() if v >= 0.9999 and sc["recall"] >= 0.9999]
    sem = sc["nonempty"]
    return {"status": "PASS" if chk.get("passed") else "FAIL", "filled": f"{len([f for f in sem if f in right])}/{len(sem)}",
            "on_produced_rows": f"{len(sc['right_on_matched'])}/{len(sem)} fields right on the {sc['matched_rows']} of {sc['rows'][0]} expected rows produced",
            "right_on_matched": sc["right_on_matched"], "total_rows_expected": sc["rows"][0], "filled_fields": sc["nonempty"],
            "precision": sc["precision"], "recall": sc["recall"], "wrong": [f for f in sem if f not in right]}


def scenario(src: str, label: str, llm) -> dict:
    cfg = rb.SOURCES[src]
    target = rb.Target.from_schema(rb.SCHEMA)
    gold_raw = json.loads((rb.GOLD_DIR / f"gold_{src}.json").read_text(encoding="utf-8"))
    llm_obj = llm(gold_raw) if callable(llm) else llm
    res, rep, answers = draft_with_responder(cfg["path"], cfg["sheets"], target, cfg["params"], name=f"b_{src}",
                                             filename=f"{src}.csv", llm=llm_obj, precedents=rb.precedents_for(src))
    s = score(src, res.spec)
    return {"src": src, "scenario": label, **s, "calls": rep.calls, "repaired": rep.repaired, "tok_in": rep.tokens_in,
            "tok_out": rep.tokens_out, "accepted": rep.accepted, "rejected": len(rep.rejected), "deferred": len(rep.deferred_low_confidence),
            "unknown": len(rep.unknown), "reverted": len(rep.reverted_by_verification), "open": len(res.questions),
            "unavailable": rep.llm_unavailable, "rej": rep.rejected[:2], "rej_all": rep.rejected}


def main(argv: list[str]) -> int:
    chaos = "--chaos" in argv
    srcs = [a for a in argv if not a.startswith("--")] or ["nhs", "de", "fr"]
    rows = []
    for src in srcs:
        base = scenario(src, "rules only (no responder)", None)
        rows.append(base)
        rows.append(scenario(src, "rules + perfect responder", lambda g: OracleLLM(g)))
        if chaos:
            for mode in ("low_confidence", "invented_source", "bad_enum", "invented_cell", "wrong_scope", "malformed_once", "garbage",
                         "unavailable", "timeout", "swap_sex"):
                rows.append(scenario(src, f"chaos:{mode}", lambda g, m=mode: Chaos(OracleLLM(g), m)))
    for r in rows:
        extra = f" wrong={r.get('wrong')}" if r.get("wrong") else ""
        print(f"[{r['src']}] {r['scenario']:34} {r['status']:9} filled {r.get('filled', '-'):>6} prec {r.get('precision', '-')} | calls {r['calls']}"
              f" (+{r['repaired']} repair) tok in/out {r['tok_in']}/{r['tok_out']} | accepted {r['accepted']} rejected {r['rejected']}"
              f" deferred {r['deferred']} unknown {r['unknown']} reverted {r['reverted']} open-for-human {r['open']}{extra}")
        if r.get("why"):
            print("      why:", r["why"])
        if r["unavailable"]:
            print("      responder unavailable:", r["unavailable"])
    (Path(__file__).parent / "blind_llm_results.json").write_text(json.dumps(rows, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
