import json
import shutil
from pathlib import Path

import pytest

from sluice import workflow
from sluice.cli import main


@pytest.fixture
def prepared(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "quickstart"
    demo = tmp_path / "demo"
    shutil.copytree(src, demo)
    work = tmp_path / "work"
    workflow.prepare(demo / "input.csv", demo / "schema.json", work)
    return demo, work


def test_cli_demo_and_full_reviewed_conversion(tmp_path, capsys):
    demo, work, out = (tmp_path / n for n in ("demo", "work", "out"))
    assert main(["demo", "--out", str(demo)]) == 0
    assert main(["prepare", str(demo / "input.csv"), "--schema", str(demo / "schema.json"), "--work", str(work)]) == 0
    assert main(["review", str(work)]) == 0
    assert main(["approve", str(work), "--by", "ana", "--reason", "Reviewed each mapping and declared controls"]) == 0
    assert main(["convert", str(work), "--out", str(out)]) == 0
    assert len((out / "result.csv").read_text().splitlines()) == 4
    assert json.loads((work / "approval.json").read_text())["by"] == "ana"


@pytest.mark.parametrize("changed", ["input.csv", "schema.json", "spec.yaml"])
def test_approval_is_invalidated_by_changed_input_schema_or_spec(prepared, tmp_path, changed):
    demo, work = prepared
    workflow.approve(work, by="ana", reason="Checked mapping, scope and controls")
    path = (work if changed == "spec.yaml" else demo) / changed
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="changed since approval"):
        workflow.convert(work, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_approval_requires_reconciliation_and_a_passing_dry_run(prepared):
    demo, work = prepared
    schema = json.loads((demo / "schema.json").read_text())
    schema["controls"] = []
    (demo / "schema.json").write_text(json.dumps(schema))
    workflow.prepare(demo / "input.csv", demo / "schema.json", work)
    with pytest.raises(ValueError, match="halting reconciliation"):
        workflow.approve(work, by="ana", reason="Checked")


def test_saved_reply_answers_a_real_question_and_invalidates_approval(prepared):
    demo, work = prepared
    schema = json.loads((demo / "schema.json").read_text())
    schema["fields"].append({"name": "currency", "type": "string", "nullable": False})
    schema["example_row"]["currency"] = "EUR"
    (demo / "schema.json").write_text(json.dumps(schema))
    draft = workflow.prepare(demo / "input.csv", demo / "schema.json", work)
    q = next(q for q in draft.questions if "currency" in q.targets)
    reply = json.dumps({"answers": [{"question_id": q.id, "bindings": [
        {"kind": "constant", "target": "currency", "args": {"value": "EUR"}, "confidence": 1}]}]})
    updated = workflow.answer(work, {q.frame: reply})
    assert not any("currency" in q.targets for q in updated.questions)
    assert workflow.inspect_work(work).ok
    assert not (work / "approval.json").exists()


def test_benchmark_failure_sets_nonzero_exit(monkeypatch):
    from benchmark.realworld import rw
    monkeypatch.setattr(rw, "evaluate", lambda *a: {"case": "example", "status": "FAIL"})
    assert rw.main(["run", "example"]) == 1
