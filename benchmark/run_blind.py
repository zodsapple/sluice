"""Source-blind experiment: develop on NHS, freeze the rules, then draft DE and FR from the target schema alone.

    py benchmark/run_blind.py [nhs de fr] [--rules-only | --gold]  [--show-frames]

rules-only : the deterministic drafter with only the human parameters (source_id, country)
gold       : rules + the answers a perfect responder would give in the closed binding vocabulary (upper bound / sufficiency)
"""
from __future__ import annotations

import os
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sluice.engine import run  # noqa: E402
from sluice.infer.bindings import Binding  # noqa: E402
from sluice.infer.blind import Target, draft_blind  # noqa: E402
from sluice.spec import spec_from_dict  # noqa: E402

PACK = ROOT / "sample" / "etl_external_reports_20261010_v2"
SCHEMA = PACK / "common_hr" / "schema_for_blind_mapping.json"
GOLD_DIR = Path(__file__).parent / "blind"
SOURCES = {
    "nhs": dict(path=PACK / "cases/hr_nhs/nhs_hchs_trusts_core_june_2026.xlsx", sheets=["2", "3", "15"],
                params={"source_id": "nhs_hchs_core_2026_06", "country": "GB"}, scorer="hr_nhs",
                expected=PACK / "common_hr/nhs_expected.csv"),
    "de": dict(path=PACK / "cases/hr_de_fr/de_destatis_personal_2024_original.xlsx", sheets=["74111-03", "74111-04"],
               params={"source_id": "de_destatis_personal_2024", "country": "DE"}, scorer="hr_de",
               expected=PACK / "common_hr/de_expected.csv"),
    "fr": dict(path=PACK / "cases/hr_de_fr/fr_dgafp_insee_ip2052_original.xlsx", sheets=["Figure 1"],
               params={"source_id": "fr_dgafp_insee_ip2052", "country": "FR"}, scorer="hr_fr",
               expected=PACK / "common_hr/fr_expected.csv"),
}


def load_gold(src: str) -> dict[str, list[Binding]]:
    p = GOLD_DIR / f"gold_{src}.json"
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    return {sheet: [Binding.from_dict({**b, "origin": "human"}) for b in bs] for sheet, bs in doc.items()}


def precedents_for(src: str) -> dict[str, list[str]]:
    """Approved values of free-text fields from the other sources (their gold bindings stand in for approved specs)."""
    from sluice.agent.prompting import collect_precedents
    target = Target.from_schema(SCHEMA)
    sets = [bs for other in SOURCES if other != src for bs in load_gold(other).values()]
    return collect_precedents(sets, target.fields)


def field_scores(expected: Path, actual: Path, fields: list[str]) -> dict:
    e = pd.read_csv(expected, dtype=str, keep_default_na=False)
    a = pd.read_csv(actual, dtype=str, keep_default_na=False)
    key = ["source_sheet", "source_cell"]
    if not set(key) <= set(a.columns):
        return {"recall": 0.0, "fields": {f: 0.0 for f in fields}, "rows": (len(e), len(a))}
    # the same cell can feed several rows (sex x relationship): align by cell + a stable sequence
    e["_n"] = e.groupby(key).cumcount()
    a["_n"] = a.groupby(key).cumcount()
    m = e.merge(a, on=[*key, "_n"], how="left", suffixes=("", "_a"), indicator=True)
    found = m["_merge"] == "both"
    out = {}
    for f in fields:
        if f not in a.columns:
            out[f] = 0.0
            continue
        if f in key:                       # the alignment key itself: right whenever the row was found
            ok = [True] * len(m)
        elif f == "value":
            def same(x, y):
                try:
                    return abs(float(x) - float(y)) <= 1e-8
                except (TypeError, ValueError):
                    return x == y
            ok = [same(x, y) for x, y in zip(m[f], m[f + "_a"].fillna("<missing>"))]
        else:
            ok = (m[f] == m[f + "_a"]).tolist()
        out[f] = round(sum(o and fd for o, fd in zip(ok, found)) / max(len(e), 1), 4)
    nonempty = [f for f in fields if (e[f] != "").any()]            # fields the gold fills for this source
    matched_right = []
    mm = m[found]
    for f in nonempty:
        if f in key:
            matched_right.append(f)
        elif f == "value":
            def close(x, y):
                try:
                    return abs(float(x) - float(y)) <= 1e-8
                except ValueError:                  # empty (not applicable) on both sides is a match
                    return x == y
            if all(close(x, y) for x, y in zip(mm[f], mm[f + "_a"])):
                matched_right.append(f)
        elif f + "_a" in mm.columns and (mm[f] == mm[f + "_a"]).all() and len(mm):
            matched_right.append(f)
    return {"recall": round(found.mean(), 4), "fields": out, "rows": (len(e), len(a)), "nonempty": nonempty,
            "right_on_matched": matched_right, "matched_rows": int(found.sum()),
            "precision": round(found.sum() / max(len(a), 1), 4)}


def frozen_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "sluice" / "infer").glob("*.py")) + sorted((ROOT / "sluice" / "engine").rglob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def evaluate(src: str, mode: str) -> dict:
    cfg = SOURCES[src]
    target = Target.from_schema(SCHEMA)
    answers = load_gold(src) if mode == "gold" else None
    t0 = time.time()
    res = draft_blind(cfg["path"], cfg["sheets"], target, cfg["params"], name=f"blind_{src}", filename=f"{src}_blind.csv",
                      answers=answers)
    out_dir = Path(tempfile.mkdtemp(prefix=f"blind_{src}_{mode}_"))
    info = {"src": src, "mode": mode, "open_questions": len(res.questions), "rejected_answers": len(res.rejected),
            "rule_bindings": sum(1 for bs in res.bindings for b in bs if b.origin != "human"),
            "answer_bindings": sum(1 for bs in res.bindings for b in bs if b.origin == "human"), "spec": res.spec}
    try:
        r = run(spec_from_dict(res.spec), cfg["path"], out_dir, write_logs=False)
    except Exception as exc:       # noqa: BLE001
        info.update(status="DRAFT-INVALID", why=f"{type(exc).__name__}: {exc}"[:200])
        return info
    info["secs"] = round(time.time() - t0, 1)
    if not r.ok:
        info.update(status="RUN-FAIL", why=r.diagnosis.message[:240] if r.diagnosis else "?")
        return info
    fields = [f for f, t in target.fields.items() if t.required]
    sc = field_scores(cfg["expected"], Path(r.output_path), fields)
    verdict = subprocess.run([sys.executable, str(PACK / "check_output.py"), "--case", cfg["scorer"], "--actual", r.output_path],
                             capture_output=True, text=True, encoding="utf-8", cwd=PACK,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        chk = json.loads(verdict.stdout)
    except json.JSONDecodeError:
        chk = {"passed": False}
    right = [f for f, v in sc["fields"].items() if v >= 0.9999 and sc["recall"] >= 0.9999]
    sem = [f for f in sc["nonempty"]]
    info.update(status="PASS" if chk.get("passed") else "FAIL", rows=sc["rows"], recall=sc["recall"], precision=sc["precision"],
                fields=sc["fields"], fields_right=len(right), fields_total=len(fields), wrong=[f for f in fields if f not in right],
                sem_right=len([f for f in sem if f in right]), sem_total=len(sem))
    return info


def show_frames(src: str) -> None:
    from sluice.infer.frame import build_frame
    from sluice.infer.profile import profile_file
    cfg = SOURCES[src]
    prof = profile_file(cfg["path"])
    for sh in cfg["sheets"]:
        fr = build_frame(prof.sheets[sh])
        print(f"== {src}/{sh}: layout={fr.layout} locate={fr.locate}")
        print("   facts:", {k: v[:70] for k, v in list(fr.facts.items())[:4]})
        print("   units:", fr.unit_texts, "tokens:", fr.value_tokens)
        for n, s_ in fr.sources.items():
            print(f"   {n:32} {s_.kind:6} {s_.dtype:8} distinct={s_.n_distinct:<4} {[v[:28] for v in s_.distinct[:6]]}")
        for k, v in fr.dims.items():
            print(f"   {k}: {[x[:30] for x in v][:12]}")


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    if "--show-frames" in flags:
        for s in [a for a in argv if not a.startswith("--")] or ["nhs"]:
            show_frames(s)
        return 0
    srcs = [a for a in argv if not a.startswith("--")] or ["nhs", "de", "fr"]
    modes = ["rules"] if "--rules-only" in flags else (["gold"] if "--gold" in flags else ["rules", "gold"])
    print(f"frozen rule hash: {frozen_hash()}")
    results = []
    for s in srcs:
        for m in modes:
            i = evaluate(s, m)
            results.append(i)
            line = f"[{s}/{m:5}] {i['status']:13}"
            if "fields_right" in i:
                line += (f" filled-fields {i['sem_right']}/{i['sem_total']} (all {i['fields_right']}/{i['fields_total']})"
                         f" row recall {i['recall']} precision {i['precision']} rows exp/got {i['rows']}")
            line += f" | rule bindings {i['rule_bindings']} answers {i['answer_bindings']} open questions {i['open_questions']} rejected {i['rejected_answers']}"
            print(line)
            if i.get("why"):
                print("      why:", i["why"])
            if "wrong" in i and i["wrong"] and "--verbose" in flags:
                print("      not right:", i["wrong"])
    (Path(__file__).parent / "blind_results.json").write_text(json.dumps([{k: v for k, v in r.items() if k != "spec"} for r in results],
                                                                         indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
