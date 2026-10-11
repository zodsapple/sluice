"""py benchmark/realworld/diag.py <case>  - bindings, questions, and the first output rows next to the expected ones."""
import json
import sys
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(__file__).rsplit("benchmark", 1)[0])
sys.path.insert(0, str(__file__).rsplit("diag.py", 1)[0])
import rw  # noqa: E402

cid = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 2
case = rw.load_case(cid)
t = rw.target_of(case)
t.mode = "stats" if "value" in t.fields else "record"
t.example = case["example"]
prof = rw._profile(case["raw"])
sheets = rw.scope_sheets(cid, list(prof.sheets))
mode = "human" if "--human" in sys.argv else "rules"
res = rw.draft_blind(case["raw"], sheets, t, {"source_id": cid}, name=cid, filename="x.csv",
                     answers=rw.load_answers(cid, sheets) if mode == "human" else None,
                     structure=rw.load_structure(cid) if mode == "human" else None)
print("sheets:", list(prof.sheets)[:12], "| layout:", [f.layout for f in res.frames][:6])
for fr, bs in list(zip(res.frames, res.bindings))[:1 if "--bad" not in sys.argv else 0]:
    print("locate:", fr.locate, "| facts:", {k: v[:60] for k, v in list(fr.facts.items())[:4]})
    for b in bs:
        print(f"  {str(b.target):24}{b.kind:11}{json.dumps(b.args, ensure_ascii=False)[:100]:102}{b.confidence}")
    for s in fr.sources.values():
        print(f"    src {s.name:30}{s.kind:7}{s.dtype:8}d={s.n_distinct:<5}{[v[:20] for v in s.distinct[:3]]} <- {s.origin[:30]}")
print("Q:", [q.id for q in res.questions][:12])
out = rw.evaluate(cid, mode)
print("status:", out["status"], out.get("why", ""))
if "out" in out:
    a = pd.read_csv(out["out"], dtype=str, keep_default_na=False)
    e = pd.read_csv(rw.PACK / "cases" / cid / "expected.csv", dtype=str, keep_default_na=False)
    print("rows actual/expected:", len(a), len(e))
    for i in range(min(n, len(a), len(e))):
        print("--- row", i)
        for c in e.columns:
            av = a[c].iloc[i] if c in a.columns else "<missing col>"
            ev = e[c].iloc[i]
            def same(x, y):
                try:
                    return abs(float(x) - float(y)) < 1e-6
                except ValueError:
                    return x == y
            if "--bad" in sys.argv and same(av, ev):
                continue
            print(f"  {c:28}{'OK ' if same(av, ev) else 'XX '} actual={av[:50]!r} expected={ev[:50]!r}")
if "out" in out and len(a) == len(e):
    def _same(x, y):
        try:
            return abs(float(x) - float(y)) < 1e-6
        except ValueError:
            return x == y
    bad = {c: sum(not _same(x, y) for x, y in zip(a[c], e[c])) for c in e.columns if c in a.columns}
    print("mismatching rows per field (aligned by order):", {k: v for k, v in bad.items() if v})
    for c, v in bad.items():
        if v:
            k = next(i for i, (x, y) in enumerate(zip(a[c], e[c])) if not _same(x, y))
            print(f"   first {c}: row {k} actual={a[c].iloc[k][:40]!r} expected={e[c].iloc[k][:40]!r}")
