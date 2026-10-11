"""Benchmark cases (starter pack) as tests: hand-written specs must pass, drafted specs must reach their known level."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "sample" / "etl_starter_20261010"
pytestmark = pytest.mark.skipif(not PACK.exists(), reason="starter data pack not present")
sys.path.insert(0, str(ROOT / "benchmark"))


@pytest.fixture(scope="module")
def bench():
    import run_autospec
    import run_benchmark
    return run_benchmark, run_autospec


@pytest.mark.parametrize("case", ["02", "03", "04", "05a", "05b", "06"])
def test_hand_written_spec_passes(bench, tmp_path, case):
    rb, _ = bench
    from sluice.engine import run
    from sluice.spec import load_spec

    spec_file, src, extra, ref = rb.CASES[case]
    res = run(load_spec(rb.SPECS / spec_file), src, tmp_path, extra_inputs=extra)
    assert res.ok, res.diagnosis
    assert all(c["passed"] for c in res.controls), [c for c in res.controls if not c["passed"]][:3]
    if isinstance(ref, str):
        assert rb.score_against_pack(ref, tmp_path)["status"] == "PASS"
    elif ref is not None:
        assert rb.score_csv(ref, Path(res.output_path))["status"] == "PASS"


@pytest.mark.parametrize("case,mode,verdict", [
    ("02", "cold", "PASS"),
    ("03", "cold", "CONTROLS-OK"),
    ("04", "assisted", "PASS"),
    ("05a", "assisted", "PASS"),
    ("06", "assisted", "PASS"),
])
def test_drafted_spec_reaches_known_level(bench, case, mode, verdict):
    _, ra = bench
    c = ra.CASES[case]
    info = ra.evaluate(case, c, c["answers"] or None if mode == "assisted" else None, mode)
    assert info["verdict"] == verdict, info


def test_unanswered_questions_are_surfaced_not_guessed(bench):
    _, ra = bench
    cold = ra.evaluate("06", ra.CASES["06"], None, "cold")
    assert cold["verdict"] == "RUN-FAIL" and "unknown_date_token" in cold["qtypes"]      # OPEN sentinel is a human decision
    hist = ra.evaluate("05a", ra.CASES["05a"], None, "cold")
    assert hist["verdict"] == "RUN-FAIL" and "key_not_unique" in hist["qtypes"]          # history is not a snapshot


def test_dedupe_is_never_silent(tmp_path):
    import pandas as pd

    from sluice.engine import run
    from sluice.spec import spec_from_dict

    spec = spec_from_dict({
        "name": "t", "map": {"id": {"to": "id", "required": True}, "v": "v"}, "keys": {"cols": ["id"]},
        "template": {"filename": "o.csv", "columns": {"id": "id", "v": "v"}}})
    p = tmp_path / "in.csv"
    pd.DataFrame({"id": ["1", "1", "2"], "v": ["a", "a", "b"]}).to_csv(p, index=False)
    res = run(spec, p, tmp_path / "o")
    assert not res.ok and res.diagnosis.code == "duplicate_key"       # an exact duplicate is kept -> key clash -> asks


def test_example_row_removes_format_questions(bench):
    _, ra = bench
    without = ra.evaluate("04", ra.CASES["04"], None, "cold", use_examples=False)
    with_ex = ra.evaluate("04", ra.CASES["04"], None, "cold+ex", use_examples=True)
    assert without["verdict"] == "RUN-FAIL"                      # source_present / yyyymm unknown
    assert with_ex["verdict"] == "PASS"                          # example row: 202606 format + 0/1 presence flag
    assert "assumed_from_example" in with_ex["qtypes"]           # ...but the assumption is surfaced for review


def test_draft_never_succeeds_silently_with_lost_values(bench):
    _, ra = bench
    info = ra.evaluate("06", ra.CASES["06"], None, "cold+ex", use_examples=True)
    assert info["verdict"] == "RUN-FAIL" and "unknown_date_token" in info["qtypes"]   # OPEN would have become empty
