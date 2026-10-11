"""Held-out publishers (DE, FR, NHS, CNAM): hand-written specs must pass the pack's own checker."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "sample" / "etl_external_reports_20261010_v2"
pytestmark = pytest.mark.skipif(not PACK.exists(), reason="external pack not present")
sys.path.insert(0, str(ROOT / "benchmark"))


@pytest.mark.parametrize("case", ["fr", "de", "nhs", "cnam"])
def test_external_spec_passes_pack_checker(tmp_path, case):
    import run_external as rx
    from sluice.engine import run
    from sluice.spec import load_spec

    spec_file, src, scorer, _ = rx.CASES[case]
    res = run(load_spec(ROOT / "benchmark" / "external" / spec_file), src, tmp_path, write_logs=False)
    assert res.ok, res.diagnosis
    assert all(c["passed"] for c in res.controls)
    p = subprocess.run([sys.executable, str(PACK / "check_output.py"), "--case", scorer, "--actual", res.output_path],
                       capture_output=True, text=True, encoding="utf-8", cwd=PACK,
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    verdict = json.loads(p.stdout)
    assert verdict["passed"], {k: v for k, v in verdict.items() if k.startswith(("missing", "extra", "mismatch", "duplicate"))}


def test_x_marker_is_not_a_zero_and_leading_zeros_survive(tmp_path):
    import csv

    import run_external as rx
    from sluice.engine import run
    from sluice.spec import load_spec

    spec_file, src, _, _ = rx.CASES["de"]
    res = run(load_spec(ROOT / "benchmark" / "external" / spec_file), src, tmp_path, write_logs=False)
    rows = list(csv.DictReader(open(res.output_path, encoding="utf-8")))
    na = [r for r in rows if r["value_status"] == "not_applicable"]
    assert len(na) == 108 and all(r["value"] == "" for r in na)          # 108 X cells: empty, never 0
    assert {r["category_code"] for r in rows} >= {"01", "11/12", "16/17"}  # codes stay text


def test_nhs_region_headcount_gap_is_kept_not_repaired(tmp_path):
    import run_external as rx
    from sluice.engine import run
    from sluice.spec import load_spec

    spec_file, src, _, _ = rx.CASES["nhs"]
    res = run(load_spec(ROOT / "benchmark" / "external" / spec_file), src, tmp_path, write_logs=False)
    ctl = {c["name"]: c for c in res.controls}
    assert ctl["headcount_regions_sum_kept_as_published"]["output"] == 1537351     # England is 1,536,637: the 714 stays
    assert ctl["england_headcount"]["output"] == 1536637
