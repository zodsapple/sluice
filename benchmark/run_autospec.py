"""How far does automatic spec drafting get on the benchmark?

For every case the "user" supplies only what the product promises: the file, the template (column headers), the key,
the dedup choice and the validation ranges. The draft is then run and scored against the frozen expectation.

  cold      = draft as is (every open question stays unanswered)
  assisted  = the minimum answers a human would give to the questions (counted as human effort)

    py benchmark/run_autospec.py [case ...]
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sluice.engine import run  # noqa: E402
from sluice.infer.autospec import draft_spec  # noqa: E402
from sluice.spec import spec_from_dict  # noqa: E402

PACK = ROOT / "sample" / "etl_starter_20261010"
IN, EXP = PACK / "inputs", PACK / "expected"
SPECS = Path(__file__).parent / "specs"


def header_of(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh))


def declared_controls(spec_file: str) -> tuple[list[dict], dict | None]:
    doc = yaml.safe_load((SPECS / spec_file).read_text(encoding="utf-8"))
    ctl = [c for c in doc.get("controls", []) if "source" not in c]          # validation ranges the user chose
    return ctl, doc.get("keys", {}).get("on_duplicate")


CASES = {
    "02": dict(file=IN / "02_sales_weekly_wide.csv", tpl=header_of(EXP / "02_sales_weekly_long.csv"), spec="02_sales_weekly.yaml",
               keys=["product_code", "week_index"], out="02_sales_weekly_long.csv", ref=("pack", "02"), answers={}),
    "04": dict(file=IN / "04_pharmacy_multisheet_wide.xlsx", tpl=header_of(EXP / "04_pharmacy_long.csv"), spec="04_pharmacy_multisheet.yaml",
               keys=["year_month", "bnf_code", "bnf_name", "unit"], out="04_pharmacy_long.csv", ref=("pack", "04"),
               answers={"derived": ["source_present"],
                        "extra_shape": [{"op": "flag_present", "cols": ["items", "total_quantity", "nic_gbp"], "out": "source_present"},
                                        {"op": "regex_replace", "col": "year_month", "pattern": "-", "repl": ""}]}),
    "05a": dict(file=IN / "05_hr_effective_history.csv", tpl=header_of(EXP / "06_hr_snapshot.csv"), spec="05a_hr_history_snapshot.yaml",
                keys=["employee_id"], out="06_hr_snapshot.csv", ref=("pack", "06"),
                answers={"derived": ["as_of"],
                         "extra_shape": [{"op": "asof_filter", "from_col": "effective_from", "to_col": "effective_to",
                                          "as_of": "2000-01-01", "out_col": "as_of"}]}),
    "06": dict(file=IN / "06_hr_grouped_report.xlsx", tpl=header_of(EXP / "06_hr_snapshot.csv"), spec="06_hr_grouped_report.yaml",
               keys=["employee_id"], out="06_hr_snapshot.csv", ref=("pack", "06"),
               answers={"const": {"as_of": "2000-01-01"}, "sentinels": {"OPEN": "9999-01-01"}}),
    "03": dict(file=IN / "03_pharmacy_monthly_long.csv", tpl=header_of(IN / "03_pharmacy_monthly_long.csv"), spec="03_pharmacy_monthly.yaml",
               keys=None, out="03_pharmacy_landing.csv", ref=("controls", None), answers={}),
    "01": dict(file=IN / "01_sales_online_retail.xlsx",
               tpl=["invoice_no", "stock_code", "description", "quantity", "invoice_date", "unit_price", "customer_id", "country"],
               spec="01_sales_retail.yaml", keys=None, out="01_sales_landing.csv", ref=("controls", None), answers={}),
}


def first_row(path: Path) -> dict[str, str]:
    """The one example row a user would put under the template headers."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return next(csv.DictReader(fh))


EXAMPLE_FILES = {"02": "02_sales_weekly_long.csv", "04": "04_pharmacy_long.csv", "05a": "06_hr_snapshot.csv",
                 "06": "06_hr_snapshot.csv"}


def _num(v: str):
    try:
        return Decimal(v)
    except InvalidOperation:
        return v


def column_scores(expected: Path, actual: Path) -> dict[str, bool]:
    def load(p: Path):
        with p.open(encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            rows = list(rd)
            return rd.fieldnames, rows
    ef, er = load(expected)
    af, ar = load(actual)
    res = {}
    for c in ef:
        res[c] = c in (af or []) and Counter(_num(r[c]) for r in er) == Counter(_num(r[c]) for r in ar)
    return res


def evaluate(case: str, c: dict, answers: dict | None, label: str, use_examples: bool = True) -> dict:
    ctl, dup = declared_controls(c["spec"])
    d = draft_spec(str(c["file"]), c["tpl"], name=f"auto_{case}", filename=c["out"], keys=c["keys"],
                   on_duplicate=dup, controls=ctl, answers=answers,
                   examples=first_row(EXP / EXAMPLE_FILES[case]) if use_examples and case in EXAMPLE_FILES else None)
    spec = spec_from_dict(d.spec)
    out = Path(tempfile.mkdtemp(prefix=f"auto_{case}_"))
    t0 = time.time()
    res = run(spec, c["file"], out, write_logs=False)
    secs = time.time() - t0
    info = {"case": case, "mode": label, "questions": len(d.questions), "qtypes": dict(Counter(q["type"] for q in d.questions)),
            "run": res.status, "secs": round(secs, 1), "conf": d.confidence, "why": None, "cols": None, "verdict": None}
    if not res.ok:
        info["why"] = res.diagnosis.message[:140] if res.diagnosis else "?"
        info["verdict"] = "RUN-FAIL"
        return info
    kind, key = c["ref"]
    if kind == "pack":
        exp = EXP / {"02": "02_sales_weekly_long.csv", "04": "04_pharmacy_long.csv", "06": "06_hr_snapshot.csv"}[key]
        scores = column_scores(exp, Path(res.output_path))
        info["cols"] = f"{sum(scores.values())}/{len(scores)}"
        info["bad_cols"] = [k for k, v in scores.items() if not v]
        info["verdict"] = "PASS" if all(scores.values()) and res.rows_out == sum(1 for _ in open(exp, encoding='utf-8-sig')) - 1 else "FAIL"
    else:
        info["verdict"] = "CONTROLS-OK" if res.controls and all(x["passed"] for x in res.controls) else "FAIL"
        info["cols"] = f"controls {sum(x['passed'] for x in res.controls)}/{len(res.controls)}"
    return info


def main(argv: list[str]) -> int:
    rows = []
    for case in argv or ["02", "03", "04", "05a", "06", "01"]:
        c = CASES[case]
        for label, ans, use_ex in (("cold", None, False), ("cold+ex", None, True), ("assisted", c["answers"] or None, True)):
            if label != "cold" and case not in EXAMPLE_FILES and label == "cold+ex":
                continue
            if label == "assisted" and not c["answers"]:
                continue
            info = evaluate(case, c, ans, label, use_ex)
            rows.append(info)
            extra = f" bad_cols={info.get('bad_cols')}" if info.get("bad_cols") else ""
            print(f"[{case}/{label:8}] {info['verdict']:11} cols={info['cols']} questions={info['questions']} {info['qtypes']} "
                  f"conf={info['conf']} {info['secs']}s{extra}" + (f"\n      why: {info['why']}" if info["why"] else ""))
    (Path(__file__).parent / "autospec_results.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
