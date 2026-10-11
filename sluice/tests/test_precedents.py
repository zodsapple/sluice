"""Precedent store: confirmed answers reused for the same kind of source - and not reused when the layout drifted."""
from __future__ import annotations

import openpyxl
import pandas as pd
import pytest

from sluice.engine import run
from sluice.infer.bindings import Binding, TargetField
from sluice.infer.blind import Target, draft_blind
from sluice.infer.precedent_store import PrecedentStore
from sluice.spec import spec_from_dict

HEADER = ["Code", "Qty", "Region", "Kind"]


def workbook(path, rows, header=HEADER, sheet="Data"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def target() -> Target:
    f = lambda n, t="string": TargetField(n, t, [], True, "", {})                           # noqa: E731
    return Target({"source_id": f("source_id"), "code": f("code"), "units": f("units", "decimal"), "region": f("region"),
                   "row_kind": f("row_kind")}, [], mode="record",
                  example={"source_id": "x", "code": "A1", "units": "3", "region": "N", "row_kind": "line"})


ANSWERS = [
    Binding("template", "row_kind", {"template": "summary", "where": {"code": "NA"}}, 0.9, "human"),
    Binding("template", "row_kind", {"template": "line", "where": {"code": {"not_in": ["NA"]}}}, 0.9, "human"),
    Binding("column", "units", {"source": "qty"}, 0.9, "human"),
]


def draft(path, store=None, answers=None):
    return draft_blind(path, ["Data"], target(), {"source_id": "s"}, name="t", filename="t.csv", answers=answers, store=store)


def test_confirmed_answers_are_reused_on_the_next_file_with_the_same_columns(tmp_path):
    first = workbook(tmp_path / "a.xlsx", [["A1", 3, "N", "x"], ["NA", 9, "S", "y"]])
    store = PrecedentStore(tmp_path / "store.json")
    res = draft(first, answers={"Data": ANSWERS})
    store.add(res.frames[0], list(target().fields), res.bindings[0], confirmed_by="ana", verified="controls reconciled", label="2024")
    assert all(b["origin"] == "precedent" for e in store.entries for b in e.bindings)
    assert not any(b["target"] in ("source_id", "source_sheet") for e in store.entries for b in e.bindings)     # rule work is not stored

    second = workbook(tmp_path / "b.xlsx", [["B2", 5, "W", "z"], ["NA", 1, "E", "q"], ["C3", 2, "N", "r"]], sheet="Data")
    again = draft(second, store=PrecedentStore(tmp_path / "store.json"))                  # a fresh object: read from disk
    assert again.precedents and again.precedents[0][1].similarity == 1.0
    r = run(spec_from_dict(again.spec), second, tmp_path / "out", write_logs=False)
    out = pd.read_csv(r.output_path, dtype=str, keep_default_na=False)
    assert out["row_kind"].tolist() == ["line", "summary", "line"] and out["units"].tolist() == ["5.0", "1.0", "2.0"]


def test_a_drifted_layout_or_another_target_does_not_match(tmp_path):
    first = workbook(tmp_path / "a.xlsx", [["A1", 3, "N", "x"]])
    store = PrecedentStore(tmp_path / "store.json")
    res = draft(first, answers={"Data": ANSWERS})
    store.add(res.frames[0], list(target().fields), res.bindings[0], confirmed_by="ana", verified="checker PASS")
    renamed = workbook(tmp_path / "c.xlsx", [["A1", 3, "N", "x"]], header=["Code", "Quantity", "Region", "Kind"])    # 2 of 5 columns differ
    assert draft(renamed, store=store).precedents == []
    other = Target({"code": TargetField("code", "string", [], True, "", {})}, [], mode="record", example={"code": "A1"})
    assert store.lookup(res.frames[0], list(other.fields)) is None                                                   # different target


def test_a_reused_answer_that_no_longer_fits_is_rejected_not_crashed(tmp_path):
    wide = ["Code", "Qty", "Region", "Kind", "c5", "c6", "c7", "c8", "c9", "c10"]
    first = workbook(tmp_path / "a.xlsx", [["A1", 3, "N", "x", 1, 2, 3, 4, 5, 6]], header=wide)
    store = PrecedentStore(tmp_path / "store.json")
    res = draft(first, answers={"Data": ANSWERS})
    store.add(res.frames[0], list(target().fields), res.bindings[0], confirmed_by="ana", verified="checker PASS")
    fewer = workbook(tmp_path / "d.xlsx", [["A1", 3, "N", 1, 2, 3, 4, 5, 6]], header=["Code", "Region", "Kind", "c5", "c6", "c7", "c8", "c9", "c10"])
    again = draft(fewer, store=store)                      # 9 of 10 columns still there: matches, but `qty` is gone
    assert again.precedents and any("qty" in why for _, _, why in again.rejected)


def test_only_stable_confirmed_work_is_stored(tmp_path):
    first = workbook(tmp_path / "a.xlsx", [["A1", 3, "N", "x"]])
    res = draft(first)
    store = PrecedentStore(tmp_path / "store.json")
    e = store.add(res.frames[0], list(target().fields), res.bindings[0], confirmed_by="ana", verified="x")   # rules only: column matches
    assert {b["kind"] for b in e.bindings} == {"column"}                    # no run parameters, no example-based constants, no lineage
    res2 = draft(first, answers={"Data": ANSWERS})
    with pytest.raises(ValueError, match="verification"):
        store.add(res2.frames[0], list(target().fields), res2.bindings[0], confirmed_by="ana", verified="")
