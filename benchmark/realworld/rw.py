"""Run the realworld noisy pack (20 cases, 4 domains) through the blind drafter and score with the pack's own checker.

    py benchmark/realworld/rw.py list
    py benchmark/realworld/rw.py frames <case>
    py benchmark/realworld/rw.py run <case> [<case> ...|all]

The pack's template.json carries `mapping` / `source_mapping` (the answers). They are NEVER shown to the drafter: only field
names, types, nullability, the one example row and the template's written rules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PACK = Path(os.environ["RW_PACK"]) if os.environ.get("RW_PACK") else ROOT / "sample" / "etl_realworld_noisy_20261011_v3"      # another pack, same layout

from sluice.engine import run  # noqa: E402
from sluice.infer.bindings import Binding, TargetField  # noqa: E402
from sluice.infer.blind import Target, draft_blind, _profile  # noqa: E402
from sluice.spec import spec_from_dict  # noqa: E402

TYPES = {"string": "string", "integer": "integer", "date": "date", "decimal": "decimal", "number": "decimal"}


def kind_of(declared: str) -> str:
    d = str(declared).lower()
    for key in ("date", "integer", "decimal", "number"):
        if key in d:
            return TYPES[key]
    return "string"


def manifest() -> dict[str, dict]:
    m = json.loads((PACK / "benchmark_manifest.json").read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in m["cases"]}


def load_case(cid: str) -> dict:
    d = PACK / "cases" / cid
    t = json.loads((d / "template.json").read_text(encoding="utf-8"))
    ex = t.get("example_rows", [None])[0] if "example_rows" in t else (t.get("example_row") or t.get("actual_example_row") or t.get("actual_example"))
    fields = {}
    for f in t["fields"]:
        nullable = f.get("nullable", f.get("nullable_in_scope", True))
        fields[f["name"]] = TargetField(f["name"], kind_of(f["type"]), [], not nullable, "", {})
    notes = list(t.get("rules") or [])
    desc_file = Path(__file__).parent / "descriptions" / f"{cid}.json"          # one-sentence meanings the template's author would write
    descs = json.loads(desc_file.read_text(encoding="utf-8")) if desc_file.exists() else {}
    for name, text in descs.items():
        fields[name].description = text
    raw = manifest()[cid]["raw_file"]
    return {"id": cid, "dir": d, "raw": d / Path(raw).name, "fields": fields, "example": ex or {}, "notes": notes,
            "types": {f["name"]: f["type"] for f in t["fields"]}}


def scope_sheets(cid: str, available: list[str]) -> list[str]:
    """The sheets a human says belong to the extraction (the pack names them in validation.json); all non-empty ones otherwise."""
    v = json.loads((PACK / "cases" / cid / "validation.json").read_text(encoding="utf-8"))
    names: list[str] = []
    if isinstance(v.get("sheet"), str):
        names.append(v["sheet"])
    for key in ("sheets", "ranges"):
        for item in v.get(key) or []:
            n = item if isinstance(item, str) else (item.get("name") or item.get("sheet"))
            if n and n not in names:
                names.append(n)
    hit = [n for n in names if n in available]
    return hit or available


def load_answers(cid: str, sheets: list[str]) -> dict[str, list[Binding]]:
    p = Path(__file__).parent / "answers" / f"{cid}.json"
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, list[Binding]] = {}
    for sh in sheets:
        items = [*doc.get("*", []), *doc.get(sh, [])]
        out[sh] = [Binding.from_dict({**b, "origin": "human"}) for b in items]
    return out


def load_structure(cid: str) -> dict:
    """Human structure hints for sheets the profiler cannot read alone (no header row ...), from answers/<case>.json "_structure"."""
    p = Path(__file__).parent / "answers" / f"{cid}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("_structure", {}) if p.exists() else {}


def target_of(case: dict) -> Target:
    return Target(case["fields"], case["notes"])


def score(cid: str, out_path: str) -> dict:
    p = subprocess.run([sys.executable, str(PACK / "check_output.py"), "--case", cid, "--actual", out_path],
                       capture_output=True, text=True, encoding="utf-8", cwd=PACK, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"passed": False, "error": (p.stdout + p.stderr)[-400:]}


def _fmt_fields(res: dict) -> str:
    f = res.get("fields")
    if not f or "error" in f:
        return ""
    return f"rows {f['rows_actual']}/{f['rows_expected']} fields {f['fields_right']}/{f['fields_total']}"


def evaluate(cid: str, mode: str = "rules", store_path: str | None = None, save: bool = False, by: str = "human") -> dict:
    case = load_case(cid)
    target = target_of(case)
    target.mode = "stats" if "value" in target.fields else "record"
    target.example = case["example"]
    prof = _profile(case["raw"])
    sheets = scope_sheets(cid, list(prof.sheets))
    answers = load_answers(cid, sheets) if mode == "human" else None
    from sluice.infer.precedent_store import PrecedentStore
    store = PrecedentStore(store_path) if store_path else None
    res = draft_blind(case["raw"], sheets, target, {"source_id": cid}, name=cid, filename=f"{cid}.csv", answers=answers, store=store,
                      structure=load_structure(cid) if mode == "human" else None)
    info = {"case": cid, "mode": target.mode, "sheets": len(sheets), "open_questions": len(res.questions), "rejected": len(res.rejected)}
    out = Path(tempfile.mkdtemp(prefix=f"rw_{cid}_"))
    try:
        r = run(spec_from_dict(res.spec), case["raw"], out, write_logs=False)
    except Exception as exc:  # noqa: BLE001
        return {**info, "status": "DRAFT-INVALID", "why": f"{type(exc).__name__}: {exc}"[:240]}
    if not r.ok:
        return {**info, "status": "RUN-FAIL", "why": r.diagnosis.message[:240] if r.diagnosis else "?"}
    v = score(cid, r.output_path)
    if res.precedents:
        info["reused"] = sorted({f"{m.entry.id}@{m.similarity}" for _, m in res.precedents})
    if save and store is not None and v.get("passed"):
        for fr, bs in zip(res.frames, res.bindings):
            if any(b.origin != "rule" for b in bs):
                store.add(fr, list(target.fields), bs, confirmed_by=by, verified="pack checker PASS", label=cid)
        info["saved"] = True
    try:
        info["fields"] = field_report(cid, r.output_path)
    except Exception as exc:  # noqa: BLE001
        info["fields"] = {"error": str(exc)[:80]}
    keep = {k: v[k] for k in v if k.startswith(("missing", "extra", "mismatch", "duplicate", "rows", "field"))}
    return {**info, "status": "PASS" if v.get("passed") else "FAIL", "out": r.output_path, **{k: (str(x)[:300]) for k, x in keep.items()}}


def _same(x: str, y: str) -> bool:
    try:
        return abs(float(x) - float(y)) <= 1e-6 * max(1.0, abs(float(y)))
    except ValueError:
        return x == y


def field_report(cid: str, out_path: str) -> dict:
    """Per field: do the values the draft produced match the expected ones as a bag (row association aside)?
    The checker is all-or-nothing; this says WHICH columns the draft got right."""
    import collections

    import pandas as pd

    e = pd.read_csv(PACK / "cases" / cid / "expected.csv", dtype=str, keep_default_na=False)
    a = pd.read_csv(out_path, dtype=str, keep_default_na=False)

    def norm(v: str) -> str:
        v = v.strip()
        try:
            return f"{float(v):.8g}"
        except ValueError:
            return v
    right, partial, wrong = [], [], []
    for c in e.columns:
        if c not in a.columns:
            wrong.append(c)
            continue
        be, ba = collections.Counter(map(norm, e[c])), collections.Counter(map(norm, a[c]))
        hit = sum((be & ba).values()) / max(sum(be.values()), 1)
        extra = sum((ba - be).values())
        (right if hit >= 0.9999 and extra == 0 else partial if hit >= 0.5 else wrong).append(c)
    return {"rows_expected": len(e), "rows_actual": len(a), "fields_right": len(right), "fields_total": len(e.columns),
            "partial": partial, "wrong_fields": wrong + partial}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        for cid, c in manifest().items():
            print(f"{cid:34} {c['domain']:10} {c['format']:5} rows={c.get('expected_rows')}")
        return 0
    if cmd == "frames":
        from sluice.infer.frame import build_frame
        prof = _profile(load_case(argv[1])["raw"])
        for name, sp in prof.sheets.items():
            fr = build_frame(sp)
            print(f"== {name}: layout={fr.layout} locate={fr.locate}\n   facts={ {k: v[:50] for k, v in list(fr.facts.items())[:4]} }")
            for n, s in fr.sources.items():
                print(f"   {n:28} {s.kind:6} {s.dtype:8} d={s.n_distinct:<4} {[v[:22] for v in s.distinct[:4]]}  <- {s.origin[:30]}")
        return 0
    if cmd == "run":
        mode = "human" if "--human" in argv else "rules"
        store_path = next((argv[i + 1] for i, a in enumerate(argv) if a == "--store"), None)
        save = "--save" in argv
        argv = [a for i, a in enumerate(argv) if not a.startswith("--") and not (i > 0 and argv[i - 1] == "--store")]
        ids = list(manifest()) if argv[1:] == ["all"] else argv[1:]
        for cid in ids:
            try:
                res = evaluate(cid, mode, store_path, save)
            except Exception as exc:  # noqa: BLE001
                res = {"case": cid, "status": "CRASH", "why": f"{type(exc).__name__}: {exc}"[:240]}
            print(f"{res['case']:30} {res.get('mode', '-'):6} {res['status']:13} Q={res.get('open_questions', '-'):<4} "
                  f"missing={res.get('missing_key_count', '-'):<6} extra={res.get('extra_key_count', '-'):<6} "
                  f"mismatch={res.get('mismatched_rows', '-'):<5} {_fmt_fields(res)} {res.get('why', '')[:120]}"
                  + (" reused " + ",".join(res["reused"]) if res.get("reused") else "") + (" SAVED" if res.get("saved") else ""), flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
