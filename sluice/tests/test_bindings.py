"""The closed answer vocabulary: what is accepted, what is refused, and how little a packet contains."""
from __future__ import annotations

import json

import pytest

from sluice.agent.prompting import RESPONSE_SCHEMA, build_packet, parse_response, render_messages
from sluice.infer.bindings import Binding, BindingError, TargetField, ValidationContext, validate

TARGETS = {
    "measure": TargetField("measure", "string", ["headcount", "fte"]),
    "sex": TargetField("sex"),
    "reference_date": TargetField("reference_date", "date"),
    "value": TargetField("value", "decimal"),
}


def ctx(**kw):
    base = dict(targets=TARGETS, sources={"kind_col", "dim1", "cell_value"},
                dim_values={"kind_col": ["FTE", "Headcount"], "dim1": ["All genders", "Female", "Male"]},
                cells={"A2"}, bound={"measure"})
    base.update(kw)
    return ValidationContext(**base)


def test_valid_answers_pass():
    validate(Binding("column", "measure", {"source": "kind_col", "mapping": {"FTE": "fte", "Headcount": "headcount"}}), ctx())
    validate(Binding("template", "sex", {"template": "all"}), ctx())
    validate(Binding("template", "sex", {"template": "{dim1}", "where": {"measure": "fte"}}), ctx())
    validate(Binding("grid_scope", None, {"dimension": "dim1", "include": ["Female"]}), ctx())
    validate(Binding("unknown", "sex", {"reason": "not in the sheet"}), ctx())


@pytest.mark.parametrize("b,why", [
    (Binding("column", "measure", {"source": "invented"}), "unknown source"),
    (Binding("column", "measure", {"source": "kind_col", "mapping": {"FTE": "full time"}}), "not allowed"),
    (Binding("column", "measure", {"source": "kind_col", "mapping": {"Total": "fte"}}), "do not occur"),
    (Binding("constant", "measure", {"value": "snapshot"}), "not allowed"),
    (Binding("sheet_cell", "reference_date", {"cell": "Z99", "date": True}), "banner cell"),
    (Binding("template", "sex", {"template": "{missing}"}), "unknown names"),
    (Binding("scale", "value", {"source": "cell_value", "factor": 0}), "non-zero"),
    (Binding("grid_scope", None, {"dimension": "dim9", "include": ["x"]}), "not available"),
    (Binding("grid_scope", None, {"dimension": "dim1", "include": ["Nobody"]}), "include"),
    (Binding("column", "no_such_field", {"source": "dim1"}), "not a field"),
    (Binding("python", "sex", {"code": "os.system('x')"}), "unknown kind"),
    (Binding("column", "sex", {"source": "dim1"}, confidence=1.7), "confidence"),
])
def test_everything_else_is_refused(b, why):
    with pytest.raises(BindingError, match=why):
        validate(b, ctx())


def test_whitespace_in_header_values_does_not_matter():
    c = ctx(dim_values={"dim1": ["Beamte/Beamtinnen,\nRichter"]})
    validate(Binding("column", "sex", {"source": "dim1", "mapping": {"Beamte/Beamtinnen, Richter": "x"}}), c)


def test_reply_parsing_is_strict_but_tolerates_prose():
    assert parse_response('Here you go: {"answers": []}') == []
    for bad in ("no json at all", '{"answers": "nope"}', '{"answers": [', "[]"):
        with pytest.raises(BindingError):
            parse_response(bad)
    assert RESPONSE_SCHEMA["required"] == ["answers"]


def test_packet_is_small_has_no_rows_and_honours_redaction():
    from sluice.infer.blind import Question
    from sluice.infer.frame import Frame, SourceInfo

    sources = {"kind_col": SourceInfo("kind_col", "column", "text", 0.0, ["FTE", "Headcount"], 2, origin="Data type"),
               "secret": SourceInfo("secret", "column", "text", 0.0, ["Alice", "Bob"], 2, origin="Name"),
               "dim1": SourceInfo("dim1", "dim", "text", 0.0, ["All genders", "Female", "Male"], 3, origin="header")}
    fr = Frame("S", {}, {}, [], sources, {"A2": "Staff by gender " + "x" * 400}, "wide", {"dim1": ["All genders", "Female", "Male"]},
               distinct_all={"kind_col": ["FTE", "Headcount"], "secret": ["Alice", "Bob"], "dim1": ["All genders", "Female", "Male"]})
    q = [Question("S:measure", "S", ["measure"], "field", "no evidence", ["column"])]
    pk = build_packet(fr, q, TARGETS, {}, redact={"secret"})
    text = json.dumps(pk)
    assert len(text) < 3000 and "Alice" not in text and "Bob" not in text
    assert all(len(v) <= 160 for v in pk["cells"].values())            # banner cells are truncated
    assert "kind_col" in text and "FTE" in text                          # but the evidence needed for the mapping is complete
    msgs = render_messages(pk)
    assert msgs[0]["role"] == "system" and json.loads(msgs[-1]["content"])["questions"][0]["id"] == "S:measure"
    assert "_hash" not in msgs[-1]["content"]


def test_row_scope_can_say_what_to_drop_or_match_a_pattern():
    import pandas as pd
    from sluice.engine.expr import where_mask
    from sluice.infer.compile import _where
    df = pd.DataFrame({"org": ["All organisations", "Trust A", "Trust B", None, "Unknown"]})
    keep = lambda c: where_mask(df, _where(c)).tolist()                          # noqa: E731
    assert keep({"column": "org", "not_in": ["Unknown"]}) == [True, True, True, False, False]
    assert keep({"column": "org", "not_regex": "^All "}) == [False, True, True, False, True]
    assert keep({"column": "org", "regex": "^All "}) == [True, False, False, False, False]
    validate(Binding("row_scope", None, {"conditions": [{"column": "dim1", "not_regex": "^All"}], "mode": "all"}), ctx())
    with pytest.raises(BindingError, match="regular expression"):
        validate(Binding("row_scope", None, {"conditions": [{"column": "dim1", "regex": "(["}], "mode": "all"}), ctx())
