"""Source-blind drafting (rules + closed vocabulary) and the responder contract, on the held-out publishers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "sample" / "etl_external_reports_20261010_v2"
pytestmark = pytest.mark.skipif(not PACK.exists(), reason="external pack not present")
sys.path.insert(0, str(ROOT / "benchmark"))
sys.path.insert(0, str(ROOT / "benchmark" / "blind"))


@pytest.fixture(scope="module")
def rb():
    import run_blind
    return run_blind


@pytest.fixture(scope="module")
def rl():
    import run_blind_llm
    return run_blind_llm


@pytest.mark.parametrize("src", ["fr", "de", "nhs"])
def test_gold_answers_in_the_closed_vocabulary_are_sufficient(rb, src):
    r = rb.evaluate(src, "gold")
    assert r["status"] == "PASS" and r["rejected_answers"] == 0, r.get("why")
    assert r["fields_right"] == r["fields_total"]


@pytest.mark.parametrize("src", ["fr", "de", "nhs"])
def test_rules_alone_admit_what_they_do_not_know(rb, src):
    r = rb.evaluate(src, "rules")
    assert r["status"] == "FAIL" and r["open_questions"] >= 10                 # never a silent partial success
    assert r["sem_right"] < r["sem_total"]


def test_perfect_responder_needs_one_small_call_per_sheet(rl):
    r = rl.scenario("fr", "perfect", lambda g: rl.OracleLLM(g))
    assert r["status"] == "PASS" and r["calls"] == 1 and r["tok_in"] < 4000      # structure + distinct values, no rows
    d = rl.scenario("de", "perfect", lambda g: rl.OracleLLM(g))
    assert d["status"] == "PASS" and d["calls"] == 2 and d["tok_in"] < 8000


def test_responder_down_equals_no_responder(rl):
    base = rl.scenario("fr", "none", None)
    for mode in ("unavailable", "timeout"):
        r = rl.scenario("fr", mode, lambda g, m=mode: rl.Chaos(rl.OracleLLM(g), m))
        assert (r["status"], r["filled"], r["precision"]) == (base["status"], base["filled"], base["precision"])
        assert r["accepted"] == 0 and r["unavailable"]


def test_refusal_is_not_retried_but_broken_json_is_repaired_once(rl):
    g = rl.scenario("fr", "garbage", lambda g: rl.Chaos(rl.OracleLLM(g), "garbage"))
    assert g["accepted"] == 0 and g["repaired"] == 0 and g["calls"] == 1
    m = rl.scenario("fr", "malformed", lambda g: rl.Chaos(rl.OracleLLM(g), "malformed_once"))
    assert m["repaired"] == 1 and m["status"] == "PASS"


def test_invented_names_and_low_confidence_never_reach_the_spec(rl):
    inv = rl.scenario("fr", "invented", lambda g: rl.Chaos(rl.OracleLLM(g), "invented_source"))
    assert inv["rejected"] > 0 and inv["status"] != "PASS"
    low = rl.scenario("fr", "low", lambda g: rl.Chaos(rl.OracleLLM(g), "low_confidence"))
    assert low["accepted"] == 0 and low["deferred"] > 0


def test_valid_looking_but_wrong_mapping_is_caught_by_known_vocabulary(rl):
    r = rl.scenario("de", "swap", lambda g: rl.Chaos(rl.OracleLLM(g), "swap_sex"))
    assert any("known word" in why for _, why in r["rej_all"]), r["rej_all"][:3]       # male<->female refused, left to a human


def test_empty_reply_is_nudged_once_and_never_counted_as_an_answer(rl):
    never = rl.scenario("fr", "empty", lambda g: rl.Chaos(rl.OracleLLM(g), "empty"))
    assert never["accepted"] == 0 and never["repaired"] == 1 and never["status"] != "PASS"
    once = rl.scenario("fr", "empty_once", lambda g: rl.Chaos(rl.OracleLLM(g), "empty_once"))
    assert once["status"] == "PASS" and once["repaired"] == 1


def test_single_prompt_for_chat_windows_has_everything_and_no_files(rb):
    from sluice.agent.prompting import build_packet, render_single_prompt
    from sluice.infer.blind import draft_blind

    cfg, t = rb.SOURCES["fr"], rb.Target.from_schema(rb.SCHEMA)
    res = draft_blind(cfg["path"], cfg["sheets"], t, cfg["params"], name="x", filename="x.csv")
    text = render_single_prompt(build_packet(res.frames[0], res.questions, t.fields, {}))
    assert "### INSTRUCTIONS" in text and "### PACKET" in text and "Do not return an empty list" in text
    assert "Figure 1:measure" in text and len(text) < 14000


# ---- a real model's reply (Gemini, first attempt on FR) as a regression sample --------------------------------------------------
FIXTURE = ROOT / "sluice" / "tests" / "fixtures" / "gemini_fr_reply_1.txt"


def _apply_real_reply():
    from prompt_cli import ReplayLLM
    import run_blind as rb_

    from sluice.agent.blind_resolve import draft_with_responder

    cfg, t = rb_.SOURCES["fr"], rb_.Target.from_schema(rb_.SCHEMA)
    llm = ReplayLLM({"Figure 1": FIXTURE.read_text(encoding="utf-8")})
    return draft_with_responder(cfg["path"], cfg["sheets"], t, cfg["params"], name="g", filename="g.csv", llm=llm,
                                precedents=rb_.precedents_for("fr"))


def test_real_reply_is_accepted_but_its_costly_scope_choice_is_surfaced():
    res, rep, _ = _apply_real_reply()
    assert rep.accepted == 8 and not rep.rejected and rep.calls == 1             # well-formed: nothing to repair
    high = [r for r in rep.review if r.severity == "high"]
    assert len(high) == 1 and high[0].what == "row_scope"
    assert high[0].detail["kept"] == 2 and high[0].detail["total"] == 8          # only the totals survive ...
    assert {"Fonction publique territoriale (FPT) ", "Fonction publique hospitalière (FPH) "} <= set(high[0].detail["dropped_values"])
    what = {r.what for r in rep.review}
    assert {"population_scope", "breakdown_dimension", "grid_scope"} <= what    # label copy, free-text constant, column scope
    assert "category" not in what                                                # copying a label is right for "as printed" fields


def test_precedents_come_only_from_other_sources_and_only_for_free_text_fields(rb):
    from sluice.agent.prompting import build_packet
    from sluice.infer.blind import draft_blind

    prec = rb.precedents_for("fr")
    flat = " ".join(v for vs in prec.values() for v in vs)
    assert "public_service_branch" not in flat and "FR_excluding_Mayotte" not in flat      # nothing from FR itself
    assert "region" in prec["breakdown_dimension"] and "measure" not in prec and "unit" not in prec
    cfg, t = rb.SOURCES["fr"], rb.Target.from_schema(rb.SCHEMA)
    r = draft_blind(cfg["path"], cfg["sheets"], t, cfg["params"], name="x", filename="x.csv")
    pk = build_packet(r.frames[0], r.questions, t.fields, {}, precedents=prec)
    assert set(pk["precedents"]) <= {q for q in t.fields if not t.fields[q].enum}
    assert all(u not in pk.get("unit_texts", []) for u in ("Effectif au 31 décembre1",))   # group labels are not units
