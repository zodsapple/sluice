"""Run hand-written specs on the external (held-out publisher) pack and score them with the pack's own checker.

    py benchmark/run_external.py [fr cnam nhs de]
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sluice.engine import run  # noqa: E402
from sluice.spec import load_spec  # noqa: E402

PACK = ROOT / "sample" / "etl_external_reports_20261010_v2"
CASES = {
    "fr": ("fr.yaml", PACK / "cases/hr_de_fr/fr_dgafp_insee_ip2052_original.xlsx", "hr_fr_native", "fr_out.csv"),
    "de": ("de.yaml", PACK / "cases/hr_de_fr/de_destatis_personal_2024_original.xlsx", "hr_de_native", "de_out.csv"),
    "nhs": ("nhs.yaml", PACK / "cases/hr_nhs/nhs_hchs_trusts_core_june_2026.xlsx", "hr_nhs_native", "nhs_out.csv"),
    "cnam": ("cnam.yaml", PACK / "cases/pharmacy_cnam/2025-01-a-06_medic-am-par-type-de-prescripteur_serie-mensuelle.xls",
             "pharmacy_cnam", "cnam_out.csv"),
}


def main(argv: list[str]) -> int:
    ok = True
    for case in argv or ["fr"]:
        spec_file, src, scorer, out_name = CASES[case]
        out = Path(tempfile.mkdtemp(prefix=f"ext_{case}_"))
        t0 = time.time()
        res = run(load_spec(Path(__file__).parent / "external" / spec_file), src, out)
        secs = time.time() - t0
        bad = [c for c in res.controls if not c["passed"]]
        print(f"[{case}] run={res.status} rows {res.rows_in}->{res.rows_out} {secs:.1f}s controls {len(res.controls) - len(bad)}/{len(res.controls)}")
        if not res.ok:
            print("   why:", res.diagnosis.message[:300] if res.diagnosis else "?")
            ok = False
            continue
        p = subprocess.run([sys.executable, str(PACK / "check_output.py"), "--case", scorer, "--actual", res.output_path],
                           capture_output=True, text=True, encoding="utf-8", cwd=PACK,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        r = json.loads(p.stdout)
        keep = {k: v for k, v in r.items() if k in ("passed", "error", "expected_rows", "actual_rows", "missing_key_count", "extra_key_count",
                                                    "duplicate_key_count", "mismatched_rows", "missing_columns")}
        print("   checker:", keep)
        for ex in r.get("mismatch_examples", [])[:3]:
            print("   mismatch:", json.dumps(ex, ensure_ascii=False)[:400])
        for c in r.get("sum_controls", []):
            if not c.get("passed"):
                print("   control:", c)
        ok = ok and bool(r.get("passed"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
