"""Validation boundaries that must never produce a false PASS."""
from pathlib import Path

import pandas as pd
import pytest

from sluice.engine import run
from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.template import write_output
from sluice.engine.validate import _rule_violations, evaluate_agg, evaluate_controls
from sluice.spec import spec_from_dict
from sluice.spec.models import Agg, RuleSpec


def spec(**overrides):
    return spec_from_dict({"name": "boundary", "map": {"id": "id", "amount": "amount", "qty": "qty"},
                          "template": {"filename": "result.csv", "columns": {"amount": "amount", "qty": "qty"}},
                          **overrides})


def test_control_respects_sub_cent_tolerance():
    s = spec(controls=[{"name": "amount", "output": {"sum": ["amount"]},
                        "expected": {"value": 1}, "tol": 0.000001}])
    res = evaluate_controls({}, pd.DataFrame({"amount": [1.004]}), RunContext(s, Path("x.csv")))
    assert not res[0].passed and res[0].diff == pytest.approx(0.004)


@pytest.mark.parametrize("bad", ["oops", float("inf"), float("-inf")])
def test_control_rejects_invalid_numbers(bad):
    with pytest.raises(EngineError, match="non-numeric or non-finite"):
        evaluate_agg(pd.DataFrame({"amount": [bad]}), Agg(sum=["amount"]), "output")


def test_range_rejects_invalid_text_and_infinity_but_allows_nullable_values():
    rule = RuleSpec(type="range", cols=["n"], min=0, max=100)
    bad = _rule_violations(pd.DataFrame({"n": [10, -1, "oops", None, float("inf")]}), rule)
    assert bad.tolist() == [False, True, True, False, True]


def test_declared_value_missing_is_not_silently_ignored():
    s = spec(controls=[{"name": "amount", "source": {"sum": ["amount"]}, "output": {"sum": ["amount"]},
                        "expected": {"filename": r"total_(\d+)"}}])
    frame = pd.DataFrame({"amount": [1]})
    with pytest.raises(EngineError, match="could not be read"):
        evaluate_controls({"mapped": frame}, frame, RunContext(s, Path("x.csv")))


def test_each_control_accounts_for_its_own_dropped_metric_scope_and_stage(tmp_path):
    frame = pd.DataFrame({"id": ["A", "", "B"], "amount": [1.005, 0.004, 2.001], "qty": [2, 50, 3]})
    path = tmp_path / "input.csv"
    frame.to_csv(path, index=False)
    controls = [
        {"name": "money", "source": {"sum": ["amount"]}, "output": {"sum": ["amount"]}, "tol": 1e-9},
        {"name": "units", "source": {"sum": ["qty"]}, "output": {"sum": ["qty"]}},
        {"name": "scope", "source": {"sum": ["qty"], "where": {"qty": {"lt": 10}}},
         "output": {"sum": ["qty"], "where": {"qty": {"lt": 10}}}},
        {"name": "rows", "source": {"count_rows": True}, "output": {"count_rows": True}},
        {"name": "keyed", "source": {"sum": ["qty"], "stage": "keyed"}, "output": {"sum": ["qty"]}},
    ]
    r = run(spec(keys={"cols": ["id"]}, measures=["amount", "qty"], controls=controls), path, tmp_path / "out")
    assert r.ok, r.diagnosis
    drops = {c["name"]: c["dropped"] for c in r.controls}
    assert drops == pytest.approx({"money": 0.004, "units": 50, "scope": 0, "rows": 1, "keyed": 0})


def test_required_output_is_enforced_after_type_conversion(tmp_path):
    path = tmp_path / "input.csv"
    pd.DataFrame({"id": ["A"], "amount": ["oops"], "qty": [1]}).to_csv(path, index=False)
    s = spec(template={"filename": "result.csv", "columns": {"amount": {"col": "amount", "type": "number", "required": True}}})
    r = run(s, path, tmp_path / "out")
    assert not r.ok and r.output_path is None
    assert r.controls[0]["name"] == "required_output"


def test_grouped_sum_cannot_hide_invalid_text_from_declared_control(tmp_path):
    path = tmp_path / "input.csv"
    pd.DataFrame({"id": ["A", "A"], "amount": [10, "oops"], "qty": [1, 1]}).to_csv(path, index=False)
    s = spec(template={"filename": "result.csv", "group_by": ["id"],
                       "columns": {"amount": {"col": "amount", "agg": "sum"}}},
             controls=[{"name": "declared", "output": {"sum": ["amount"]}, "expected": {"value": 10}}])
    r = run(s, path, tmp_path / "out")
    assert not r.ok and r.diagnosis.code == "malformed_amount" and r.output_path is None


def test_failed_write_preserves_previous_output_and_cleans_temp(tmp_path, monkeypatch):
    s = spec()
    dest = tmp_path / "result.csv"
    dest.write_text("previous output", encoding="utf-8")
    def interrupted(self, path, **kwargs):
        Path(path).write_text("partial", encoding="utf-8")
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_csv", interrupted)
    with pytest.raises(OSError, match="disk full"):
        write_output(pd.DataFrame({"amount": [1]}), s.template, None, dest)
    assert dest.read_text(encoding="utf-8") == "previous output"
    assert not list(tmp_path.glob(".sluice-*"))
