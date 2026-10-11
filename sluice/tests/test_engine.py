from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from sluice.agent import DeterministicHealer, LLMHealer, SpecPatch, StubLLMClient, heal
from sluice.agent.sandbox import PatchRejected, apply_patch, validate_patch
from sluice.engine import run
from sluice.spec.loader import load_spec, load_spec_dict, spec_from_dict, spec_to_dict

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "sluice" / "specs"
FIX = Path(__file__).parent / "fixtures" / "transport"
TEMPLATE = FIX / "transport_template.xlsx"
INPUT = FIX / "input"
GOLDEN = FIX / "golden"


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    return df.fillna("").astype(str).apply(lambda s: s.str.replace(r"\.0$", "", regex=True))


def _base() -> pd.DataFrame:
    return pd.read_excel(INPUT / "DHL_DE_202604_Transport.xlsx").astype({"Delivery Number": object, "Shipment ID": object})


def _write(tmp: Path, df: pd.DataFrame, name: str = "DHL_DE_202604_Transport.xlsx") -> Path:
    p = tmp / name
    df.to_excel(p, index=False)
    return p


@pytest.fixture
def spec():
    return load_spec(SPECS / "dhl_de_transport.yaml")


@pytest.mark.parametrize("spec_file,src,gold", [
    ("dhl_de_transport.yaml", "DHL_DE_202604_Transport.xlsx", "CTD_EMEA_TRANSPORT_DE_DHL_202604.xlsx"),
    ("kn_fr_transport.yaml", "KN_FR_202604_Transport.xlsx", "CTD_EMEA_TRANSPORT_FR_KN_202604.xlsx"),
])
def test_matches_ctd_golden(tmp_path, spec_file, src, gold):
    res = run(load_spec(SPECS / spec_file), INPUT / src, tmp_path, TEMPLATE)
    assert res.ok, res.diagnosis
    assert Path(res.output_path).name == gold
    pd.testing.assert_frame_equal(_norm(pd.read_excel(res.output_path)), _norm(pd.read_excel(GOLDEN / gold)))


def test_corrupt_input_fails_with_feedback(tmp_path, spec):
    res = run(spec, INPUT / "BAD_TRANSPORT_202605.xlsx", tmp_path, TEMPLATE)
    assert not res.ok and res.diagnosis.code == "header_not_found"
    assert res.output_path is None and not list(tmp_path.glob("*.xlsx"))
    assert "Why it failed" in Path(res.feedback_path).read_text(encoding="utf-8")
    assert (tmp_path / "dq_logs" / "issues.csv").exists()


def test_invalid_key_row_dropped_and_value_conserved(tmp_path, spec):
    df = _base()
    df.loc[3, "Delivery Number"] = "N/A"
    df.loc[3, "Shipment ID"] = "N/A"
    res = run(spec, _write(tmp_path, df), tmp_path / "o", TEMPLATE)
    assert res.ok and res.rows_out == 9
    ctl = {c["name"]: c for c in res.controls}["total_cost"]
    assert ctl["source"] == 1650 and ctl["dropped"] == 165 and ctl["output"] == 1485
    dropped = [i for i in res.issues if i["action"] == "dropped"]
    assert dropped and all(i["source_row"] == 5 for i in dropped)      # Excel row of df index 3
    assert json.loads(dropped[0]["details_json"])["dropped_amount"] in (150.0, 15.0)


def test_max_dropped_ratio_halts(tmp_path):
    d = load_spec_dict(SPECS / "dhl_de_transport.yaml")
    d["controls"][0]["max_dropped_ratio"] = 0.05
    df = _base()
    df.loc[3, "Delivery Number"] = "N/A"
    df.loc[3, "Shipment ID"] = "N/A"
    res = run(spec_from_dict(d), _write(tmp_path, df), tmp_path / "o", TEMPLATE)
    assert not res.ok and res.diagnosis.code == "validation_failed" and "above the 5.0% limit" in res.diagnosis.message


def test_declared_total_mismatch_halts(tmp_path, spec):
    path = _write(tmp_path, _base())
    assert run(spec, path, tmp_path / "a", TEMPLATE, expect={"total_cost": 1650}).ok
    bad = run(spec, path, tmp_path / "b", TEMPLATE, expect={"total_cost": 1700})
    assert not bad.ok and "declared 1,700.00" in bad.diagnosis.message


def test_multi_delivery_cell_is_exploded_and_allocated_to_the_cent(tmp_path, spec):
    df = _base().iloc[:1].copy()
    df["Delivery Number"] = "3000100001;3000100002;3000100003"
    df["Base Freight"], df["Fuel Surcharge"] = 100.00, 10.00
    res = run(spec, _write(tmp_path, df), tmp_path / "o", TEMPLATE)
    assert res.ok and res.rows_out == 3
    out = res.output_frame
    assert out["Transport Cost"].sum() == 100.00 and sorted(out["Transport Cost"]) == [33.33, 33.33, 33.34]
    assert round(out["Fuel Cost"].sum(), 2) == 10.00


def test_conflicting_duplicate_key_errors_but_exact_duplicate_is_dropped(tmp_path, spec):
    exact = pd.concat([_base(), _base().iloc[[0]]], ignore_index=True)
    ok = run(spec, _write(tmp_path, exact), tmp_path / "a", TEMPLATE, expect=None)
    assert ok.rows_out == 10 and any(i["reason_code"] == "duplicate_row" for i in ok.issues)
    clash = pd.concat([_base(), _base().iloc[[0]].assign(**{"Base Freight": 999})], ignore_index=True)
    bad = run(spec, _write(tmp_path, clash, "DHL_DE_x.xlsx"), tmp_path / "b", TEMPLATE)
    assert not bad.ok and bad.diagnosis.code == "duplicate_key"


def test_header_found_below_a_banner_and_total_row_skipped(tmp_path):
    d = load_spec_dict(SPECS / "dhl_de_transport.yaml")
    d["locate"]["skip_rows_matching"] = ["^total$"]
    body = _base()
    total = {c: None for c in body.columns}
    total["Shipment ID"] = "Total"
    body = pd.concat([body, pd.DataFrame([total])], ignore_index=True)
    p = tmp_path / "DHL_DE_banner.xlsx"
    with pd.ExcelWriter(p) as xw:
        pd.DataFrame([["DHL monthly report"], [None]]).to_excel(xw, header=False, index=False)
        body.to_excel(xw, startrow=2, index=False)
    res = run(spec_from_dict(d), p, tmp_path / "o", TEMPLATE)
    assert res.ok and res.rows_out == 10
    assert any(i["reason_code"] == "skipped_total_row" for i in res.issues)


def test_csv_input_with_semicolons(tmp_path, spec):
    p = tmp_path / "DHL_DE_202604.csv"
    _base().to_csv(p, sep=";", index=False)
    res = run(spec, p, tmp_path / "o", TEMPLATE)
    assert res.ok and res.rows_out == 10


# ---- self-healing -----------------------------------------------------------------------------

def _renamed(tmp: Path) -> Path:
    return _write(tmp, _base().rename(columns={"Loading Date": "Load-Date (new)"}), "DHL_DE_202605_Transport.xlsx")


def test_renamed_header_fails_with_candidates(tmp_path, spec):
    res = run(spec, _renamed(tmp_path), tmp_path / "o", TEMPLATE)
    assert not res.ok and res.diagnosis.code == "missing_required_column"
    assert res.diagnosis.candidates["Loading Date"]


def test_deterministic_healer_proposes_alias_and_sandbox_verifies(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text((SPECS / "dhl_de_transport.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    out = heal(spec_path, _renamed(tmp_path), tmp_path / "o", TEMPLATE, healer=DeterministicHealer(), mode="propose-only")
    assert out.status == "proposed"
    patch = json.loads(Path(out.patch_path).read_text(encoding="utf-8"))["patch"]
    assert patch["ops"][0]["path"] == "/map/Loading Date/aliases/-" and patch["ops"][0]["value"] == "Load-Date (new)"
    assert "aliases" not in (spec_path.read_text(encoding="utf-8").split("Loading Date")[1][:60])   # spec untouched


def test_auto_mode_applies_patch_and_keeps_backup(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text((SPECS / "dhl_de_transport.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    out = heal(spec_path, _renamed(tmp_path), tmp_path / "o", TEMPLATE, healer=DeterministicHealer(), mode="auto")
    assert out.status == "healed" and out.result.ok
    assert spec_path.with_suffix(".yaml.bak").exists()
    assert run(load_spec(spec_path), _renamed(tmp_path), tmp_path / "o2", TEMPLATE).ok


def test_stub_llm_patch_goes_through_the_same_sandbox(tmp_path, spec):
    good = {"ops": [{"op": "add", "path": "/map/Loading Date/aliases/-", "value": "Load-Date (new)"}], "rationale": "r"}
    client = StubLLMClient(good)
    diag = run(spec, _renamed(tmp_path), tmp_path / "o", TEMPLATE).diagnosis.to_dict()
    patch = LLMHealer(client).propose(diag, spec_to_dict(spec), {})
    assert patch and patch.source == "llm"
    assert "<redacted>" not in json.dumps(client.calls[0][1])
    verdict = validate_patch(spec_to_dict(spec), patch, _renamed(tmp_path), TEMPLATE)
    assert verdict.ok


@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "/controls/0/on_fail", "value": "warn"},               # loosen validation
    {"op": "remove", "path": "/controls/0"},
    {"op": "replace", "path": "/keys/pattern", "value": ".*"},
    {"op": "replace", "path": "/template/columns", "value": {}},
    {"op": "add", "path": "/shape/-", "value": {"op": "hook", "python": "os:system"}},  # code injection
    {"op": "add", "path": "/shape/-", "value": {"op": "strip", "python": "x:y"}},
])
def test_sandbox_rejects_patches_that_weaken_checks_or_inject_code(spec, op):
    with pytest.raises(PatchRejected):
        apply_patch(spec_to_dict(spec), SpecPatch([op]))


def test_validation_failures_are_never_auto_healed(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text((SPECS / "dhl_de_transport.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    called = []

    class Spy:
        def propose(self, *a):
            called.append(a)

    out = heal(spec_path, _write(tmp_path, _base()), tmp_path / "o", TEMPLATE, healer=Spy(), expect={"total_cost": 1})
    assert out.status == "unhealed" and not called


def test_unknown_op_and_bad_reason_code_are_reported():
    from sluice.report.issues import make_issue

    with pytest.raises(ValueError):
        make_issue("not_a_code")
