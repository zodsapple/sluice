"""Run every hand-written benchmark spec through sluice and score it against the frozen expectations.

    py benchmark/run_benchmark.py [case ...]        # e.g. 02 04 06
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sluice.engine import run  # noqa: E402
from sluice.spec import load_spec  # noqa: E402

PACK = ROOT / "sample" / "etl_starter_20261010"
IN = PACK / "inputs"
SPECS = Path(__file__).parent / "specs"
HR = PACK / "sources" / "hr"

# case id -> (spec file, input, extra inputs, validate_outputs.py case id | csv to compare with)
CASES = {
    "01": ("01_sales_retail.yaml", IN / "01_sales_online_retail.xlsx", {}, None),
    "02": ("02_sales_weekly.yaml", IN / "02_sales_weekly_wide.csv", {}, "02"),
    "03": ("03_pharmacy_monthly.yaml", IN / "03_pharmacy_monthly_long.csv", {}, None),
    "04": ("04_pharmacy_multisheet.yaml", IN / "04_pharmacy_multisheet_wide.xlsx", {}, "04"),
    "05a": ("05a_hr_history_snapshot.yaml", IN / "05_hr_effective_history.csv", {}, "06"),
    "05b": ("05b_hr_interval_join.yaml", HR / "hr_source_dept_emp.csv", {"titles": HR / "hr_source_titles.csv"},
            IN / "05_hr_effective_history.csv"),
    "06": ("06_hr_grouped_report.yaml", IN / "06_hr_grouped_report.xlsx", {}, "06"),
}


def multiset(path: Path) -> Counter:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        return Counter(tuple(r[c] for c in rd.fieldnames) for r in rd), tuple(rd.fieldnames)


def score_against_pack(case: str, out_dir: Path) -> dict:
    p = subprocess.run([sys.executable, str(PACK / "scripts" / "validate_outputs.py"), "--outputs", str(out_dir),
                        "--case", case], capture_output=True, text=True)
    try:
        r = json.loads(p.stdout)[0]
    except Exception:       # noqa: BLE001
        return {"status": "ERROR", "detail": (p.stdout + p.stderr)[-300:]}
    return {"status": r["status"], "detail": r.get("error") or f"missing={r.get('missing_records')} extra={r.get('extra_records')}"}


def score_csv(expected: Path, actual: Path) -> dict:
    (e, ecols), (a, acols) = multiset(expected), multiset(actual)
    if ecols != acols:
        return {"status": "FAIL", "detail": f"columns {acols} != {ecols}"}
    miss, extra = sum((e - a).values()), sum((a - e).values())
    return {"status": "PASS" if not miss and not extra else "FAIL", "detail": f"missing={miss} extra={extra}"}


def main(argv: list[str]) -> int:
    chosen = argv or list(CASES)
    rows = []
    for case in chosen:
        spec_file, src, extra, ref = CASES[case]
        out = Path(tempfile.mkdtemp(prefix=f"bench_{case}_"))
        t0 = time.time()
        res = run(load_spec(SPECS / spec_file), src, out, extra_inputs=extra)
        secs = time.time() - t0
        failed_controls = [c["name"] for c in res.controls if not c["passed"]]
        if not res.ok:
            verdict = {"status": "RUN-FAIL", "detail": res.diagnosis.message[:160] if res.diagnosis else "?"}
        elif ref is None:
            verdict = {"status": "CONTROLS", "detail": "no frozen expected output; judged by declared controls"}
        elif isinstance(ref, str):
            verdict = score_against_pack(ref, out)
        else:
            verdict = score_csv(ref, Path(res.output_path))
        rows.append((case, res.status, res.rows_in, res.rows_out, round(secs, 1), len(res.controls) - len(failed_controls),
                     len(res.controls), verdict))
        print(f"[{case}] run={res.status} rows {res.rows_in}->{res.rows_out} {secs:.1f}s "
              f"controls {len(res.controls) - len(failed_controls)}/{len(res.controls)} -> {verdict['status']}: {verdict['detail']}")
        for n in failed_controls[:5]:
            c = next(c for c in res.controls if c["name"] == n)
            print(f"      control {n}: {'; '.join(c['reasons'])}")
    return 0 if all(r[7]["status"] in ("PASS", "CONTROLS") and r[1] == "success" and r[5] == r[6] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
