"""Record tables (one row per record): column matching from names + the template's example row, and the ops that
the realworld pack needed (occurrence, group_flag, flag_list, extract, scale with where/offset, copy_col)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from sluice.engine import run
from sluice.engine.context import RunContext
from sluice.engine.ops import run_op
from sluice.infer.bindings import TargetField
from sluice.infer.blind import Target, draft_blind
from sluice.spec import spec_from_dict


def ctx() -> RunContext:
    from sluice.spec import spec_from_dict as sfd
    return RunContext(sfd({"name": "t", "workflow": "generic", "map": {}, "template": {"filename": "x.csv", "columns": {"a": "a"}}}), Path("x.xlsx"))


def test_ops_used_by_record_tables():
    c = ctx()
    df = pd.DataFrame({"k": ["a", "a", "b", "c"], "n": ["1", "2", "3", "4"], "v": ["AB1 x", "CD2 y", None, "zz"]})
    out = run_op("occurrence", {"by": ["k"], "out": "occ"}, df, c)
    assert out["occ"].tolist() == ["1", "2", "1", "1"]                      # repeated rows are counted, not dropped
    out = run_op("group_flag", {"by": ["k"], "out": "g", "repeated": "collision", "otherwise": "unique"}, df, c)
    assert out["g"].tolist() == ["collision", "collision", "unique", "unique"]
    out = run_op("extract", {"col": "v", "to": "code", "pattern": "^([A-Z]{2})"}, df, c)
    assert out["code"].tolist()[:2] == ["AB", "CD"] and pd.isna(out["code"].iloc[2])
    out = run_op("scale", {"col": "n", "factor": 1000, "out": "big", "where": {"k": "a"}}, df, c)
    assert out["big"].tolist()[:2] == [1000.0, 2000.0] and pd.isna(out["big"].iloc[2])
    out = run_op("scale", {"col": "n", "factor": 1, "out": "rec", "offset": -1}, df, c)
    assert out["rec"].tolist() == [0.0, 1.0, 2.0, 3.0]
    out = run_op("flag_list", {"out": "f", "rules": [
        {"label": "b_row", "any_of": [{"k": "b"}]}, {"label": "a_row", "any_of": [{"k": "a"}]},
        {"label": "no_v", "any_of": [{"v": {"isnull": True}}]}]}, df, c)
    assert out["f"].tolist() == ["a_row", "a_row", "b_row;no_v", ""]       # sorted, joined, rows never dropped
    out = run_op("copy_col", {"source": "k", "to": "k2"}, df, c)
    assert out["k2"].tolist() == df["k"].tolist()


def test_where_language_can_compare_columns_and_text():
    from sluice.engine.expr import where_mask
    df = pd.DataFrame({"odo": ["8", "5", None], "dist": ["842", "3", "1"], "d": ["2015-06-30", "2015-07-01", "2016-07-01"]})
    assert where_mask(df, {"odo": {"lt_col": "dist"}}).tolist() == [True, False, False]
    assert where_mask(df, {"d": {"lt": "2015-07-01"}}).tolist() == [True, False, False]
    assert where_mask(df, {"d": {"gt": "2016-06-30"}}).tolist() == [False, False, True]


@pytest.fixture()
def card_workbook(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cards"
    ws["B2"] = "Card Data - July 2025"
    ws.append([])
    ws.append([None, "Service", "Description", "Transaction Date", "Amount", "Supplier"])
    for row in (("Housing", "Repairs", "03/07/2025", 109.58, "Acme "), ("Housing", "Repairs", "03/07/2025", 109.58, "Acme "),
                ("Estates", "Power", "18/07/2025", 100, "Edf")):
        ws.append([None, *row])
    ws.append([None, None, None, "Total", 319.16, None])
    path = tmp_path / "cards.xlsx"
    wb.save(path)
    return path


def test_record_draft_from_names_and_the_example_row(card_workbook, tmp_path):
    fields = {
        "source_id": TargetField("source_id", "string", [], True, "", {}),
        "service_area": TargetField("service_area", "string", [], True, "", {}),
        "expense_description": TargetField("expense_description", "string", [], True, "", {}),
        "purchase_date": TargetField("purchase_date", "date", [], True, "", {}),
        "net_amount": TargetField("net_amount", "decimal", [], True, "", {}),
        "supplier": TargetField("supplier", "string", [], True, "", {}),
        "record_occurrence": TargetField("record_occurrence", "integer", [], True, "", {}),
        "source_row": TargetField("source_row", "integer", [], True, "", {}),
    }
    example = {"source_id": "cards", "service_area": "Housing", "expense_description": "Repairs", "purchase_date": "2025-07-03",
               "net_amount": "109.58", "supplier": "Acme", "record_occurrence": "1", "source_row": "4"}
    target = Target(fields, [], mode="record", example=example)
    res = draft_blind(card_workbook, ["Cards"], target, {"source_id": "cards"}, name="cards", filename="cards.csv")
    kinds = {b.target: (b.kind, b.args.get("source")) for b in res.bindings[0]}
    assert kinds["service_area"] == ("column", "service") and kinds["expense_description"] == ("column", "description")
    assert kinds["purchase_date"][1] == "transaction_date" and kinds["net_amount"][1] == "amount"   # by name, then type
    r = run(spec_from_dict(res.spec), card_workbook, tmp_path / "out", write_logs=False)
    assert r.ok
    out = pd.read_csv(r.output_path, dtype=str, keep_default_na=False)
    assert len(out) == 3                                                     # the Total row is a footer, not a record
    assert out["purchase_date"].tolist() == ["2025-07-03", "2025-07-03", "2025-07-18"]   # day-first format read from the values
    assert out["record_occurrence"].tolist() == ["1", "2", "1"]                         # equal rows kept and counted
    assert out["supplier"].tolist() == ["Acme", "Acme", "Edf"]
    assert out["source_row"].tolist() == ["4", "5", "6"]


def test_block_heading_rows_become_context_and_are_consumed():
    c = ctx()
    df = pd.DataFrame({"_src_sheet": "s", "_src_row": range(1, 7),
                       "label": ["Block A", "x", "y", "Block B", "z", None],
                       "y23": ["2023/24", "1", "2", "2023/24", "3", None], "y24": ["2024/25", "4", "5", "2024/25", "6", None]})
    out = run_op("block_heading", {"col": "label", "out": "b1", "marks": {"y23": "2023/24", "y24": "2024/25"}}, df, c)
    assert out["label"].tolist()[:3] == ["x", "y", "z"] and len(out) == 4
    assert out["b1"].tolist() == ["Block A", "Block A", "Block B", "Block B"]       # heading text flows down; headings are gone


def test_template_can_format_a_date_cell_and_keep_rows_overrides_a_total_rule():
    import datetime as dt
    c = ctx()
    df = pd.DataFrame({"d": [dt.datetime(2022, 10, 29), "2022 YTD"]})
    out = run_op("derive", {"col": "x", "template": "{d:%Y-%m-%dT%H:%M:%S}"}, df, c)
    assert out["x"].tolist() == ["2022-10-29T00:00:00", "2022 YTD"]                 # a non-date cell is used as printed
    from sluice.infer.bindings import Binding
    from sluice.infer.compile import _locate_keeping
    loc = {"skip_rows_matching": [r"^Total Pharmacy First", "^Notes:"]}
    kept = _locate_keeping(loc, [Binding("keep_rows", None, {"labels": ["Total Pharmacy First Consultations - GP Referral"]})])
    assert kept["skip_rows_matching"] == ["^Notes:"]
