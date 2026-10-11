"""Command line: run / dry-run / run-dir / heal / init-spec / test."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sluice.engine.pipeline import RunResult, run
from sluice.spec.loader import load_spec


def _expects(items: list[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for it in items or []:
        name, _, val = it.partition("=")
        out[name] = float(val)
    return out


def _report(res: RunResult, verbose: bool = False) -> None:
    print(f"[{res.status.upper()}] {Path(res.input_path).name}: rows {res.rows_in} -> {res.rows_out}"
          + (f"  -> {Path(res.output_path).name}" if res.output_path else ""))
    if res.diagnosis:
        print(f"  why: {res.diagnosis.code}: {res.diagnosis.message}")
    for c in res.controls:
        flag = "ok " if c["passed"] else ("FAIL" if c["on_fail"] == "halt" else "warn")
        print(f"  [{flag}] {c['name']}: source={c['source']:,.2f} dropped={c['dropped']:,.2f} output={c['output']:,.2f}"
              + ("" if c["passed"] else "  " + "; ".join(c["reasons"])))
    sev: dict[str, int] = {}
    for i in res.issues:
        sev[i["severity"]] = sev.get(i["severity"], 0) + 1
    if sev:
        print("  issues: " + ", ".join(f"{k}={v}" for k, v in sorted(sev.items())))
    if verbose:
        for t in res.traces:
            print(f"  step {t['step']:<14} rows {t['rows_in']:>5} -> {t['rows_out']:<5} amount {t['amount_in']:>12,.2f} -> "
                  f"{t['amount_out']:<12,.2f} dropped {t['dropped_rows']} / {t['dropped_amount']:,.2f}")
    if res.feedback_path:
        print(f"  feedback: {res.feedback_path}")


def _cmd_run(a: argparse.Namespace) -> int:
    res = run(load_spec(a.spec), a.input, a.out, a.template, expect=_expects(a.expect),
              write_output=not a.dry_run, write_logs=not a.dry_run)
    _report(res, verbose=a.dry_run or a.verbose)
    return 0 if res.ok else 1


def _match_spec(path: Path, specs: list[Path]) -> Path | None:
    stem = path.stem.upper()
    best, score = None, 0
    for sp in specs:
        for alias in load_spec(sp).match.filename:
            if alias.upper() in stem and len(alias) > score:
                best, score = sp, len(alias)
    return best


def _cmd_run_dir(a: argparse.Namespace) -> int:
    specs = sorted(Path(a.specs).glob("*.yaml"))
    failed = 0
    for f in sorted(p for p in Path(a.input_dir).iterdir() if p.suffix.lower() in (".xlsx", ".csv") and not p.name.startswith("~$")):
        sp = _match_spec(f, specs)
        if sp is None:
            print(f"[SKIP] {f.name}: no spec matches the file name")
            failed += 1
            continue
        res = run(load_spec(sp), f, a.out, a.template)
        _report(res)
        failed += not res.ok
    return 1 if failed else 0


def _cmd_heal(a: argparse.Namespace) -> int:
    from sluice.agent import heal

    out = heal(a.spec, a.input, a.out, a.template, mode=a.mode, expect=_expects(a.expect))
    _report(out.result)
    print(f"heal status: {out.status}")
    for at in out.attempts:
        print("  attempt", at)
    if out.patch_path:
        print(f"  proposed patch written to {out.patch_path} (review, then re-run with --mode auto or edit the spec)")
    return 0 if out.result.ok else 1


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _template_headers(path: str) -> tuple[list[str], dict[str, str]]:
    """Headers from row 1; row 2, when present, is the example row used to infer formats and types."""
    if path.lower().endswith(".csv"):
        import csv

        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = [r for _, r in zip(range(2), csv.reader(fh))]
    else:
        from openpyxl import load_workbook

        ws = load_workbook(path, read_only=True).worksheets[0]
        rows = [["" if c.value is None else (c.value.strftime("%Y-%m-%d") if hasattr(c.value, "strftime") else str(c.value))
                 for c in r] for r in ws.iter_rows(min_row=1, max_row=2)]
    headers = [h for h in rows[0] if h]
    example = dict(zip(rows[0], rows[1])) if len(rows) > 1 else {}
    return headers, {k: v for k, v in example.items() if k in headers and v != ""}


def _cmd_draft(a: argparse.Namespace) -> int:
    """Profile the input, draft a spec from the template headers, and list what a human still has to decide."""
    import yaml

    from sluice.infer import draft_spec

    headers, example = _template_headers(a.template)
    keys = [k.strip() for k in a.key.split(",")] if a.key else None
    d = draft_spec(a.input, headers, name=Path(a.input).stem.lower(), filename=a.filename or f"{Path(a.input).stem}_out.csv",
                   keys=keys, examples=example)
    text = yaml.safe_dump(d.spec, sort_keys=False, allow_unicode=True, width=120)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"Draft spec written to {a.out}  (mapping confidence {d.confidence})")
    else:
        print(text)
    print(f"\n{len(d.questions)} open question(s) for the reviewer:")
    for q in d.questions:
        print("  -", {k: v for k, v in q.items() if k != "id"})
    for n in d.notes:
        print("  note:", n)
    if d.coverage["uncovered"]:
        print(f"  coverage: {len(d.coverage['uncovered'])} template column(s) not covered by a key or control")
    return 0


def _cmd_test(a: argparse.Namespace) -> int:
    if not (Path(__file__).parent / "tests").exists():
        raise ValueError("Tests are in the source checkout: install .[dev] and run python -m pytest")
    return subprocess.call([sys.executable, "-m", "pytest", str(Path(__file__).parent / "tests"), "-q"])


def _cmd_demo(a: argparse.Namespace) -> int:
    source = Path(__file__).parent / "examples" / "quickstart"
    dest = Path(a.out)
    dest.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if (dest / path.name).exists():
            raise ValueError(f"{dest / path.name} exists; choose an empty demo directory")
    for path in source.iterdir():
        shutil.copyfile(path, dest / path.name)
    print(f"Example ready: {dest}. Run: sluice run {dest / 'spec.yaml'} {dest / 'input.csv'} --out {dest / 'output'}")
    return 0


def _cmd_workflow(a: argparse.Namespace) -> int:
    from sluice import workflow
    if a.cmd == "prepare":
        params = {}
        for item in a.param or []:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise ValueError("--param must be NAME=VALUE")
            params[key] = value
        draft = workflow.prepare(a.input, a.schema, a.work, sheets=a.sheet, params=params,
                                 filename=a.filename, store=a.store)
        print(f"Draft: {Path(a.work) / 'spec.yaml'}; {len(draft.questions)} open questions. Review: sluice review {a.work}")
        return 0
    if a.cmd == "answer":
        replies = {}
        for item in a.reply:
            sheet, sep, file = item.partition("=")
            if not sep:
                raise ValueError("--reply must be SHEET=FILE")
            replies[sheet] = Path(file).read_text(encoding="utf-8")
        draft = workflow.answer(a.work, replies)
        print(f"Applied replies; {len(draft.questions)} open questions. Re-review the regenerated spec.")
        return 0
    if a.cmd == "review":
        print((Path(a.work) / "review.md").read_text(encoding="utf-8"))
        result = workflow.inspect_work(a.work)
        _report(result, verbose=True)
        return 0 if result.ok else 1
    if a.cmd == "approve":
        result = workflow.approve(a.work, by=a.by, reason=a.reason)
        _report(result)
        print(f"Approval recorded for {a.by}. Convert: sluice convert {a.work} --out OUTPUT_DIR")
        return 0
    result = workflow.convert(a.work, a.out)
    _report(result)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sluice", description="Spec-driven Excel/CSV -> template converter")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("spec")
        sp.add_argument("input")
        sp.add_argument("--out", default="out")
        sp.add_argument("--template")
        sp.add_argument("--expect", action="append", metavar="CONTROL=VALUE",
                        help="declared control value (e.g. invoice total)")

    r = sub.add_parser("run", help="convert one file")
    common(r)
    r.add_argument("--verbose", "-v", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="no output file, no logs; prints the step trace")
    r.set_defaults(fn=_cmd_run)
    d = sub.add_parser("dry-run", help="alias of run --dry-run")
    common(d)
    d.set_defaults(fn=_cmd_run, dry_run=True, verbose=True)
    rd = sub.add_parser("run-dir", help="convert every file in a folder, picking the spec by file name")
    rd.add_argument("input_dir")
    rd.add_argument("--specs", default=str(Path(__file__).parent / "specs"))
    rd.add_argument("--out", default="out")
    rd.add_argument("--template")
    rd.set_defaults(fn=_cmd_run_dir)
    h = sub.add_parser("heal", help="run; on failure ask the healers for a spec patch (verified in a sandbox)")
    common(h)
    h.add_argument("--mode", choices=["propose-only", "auto"])
    h.set_defaults(fn=_cmd_heal)
    i = sub.add_parser("draft", help="draft a spec from an input file + template headers (human reviews the questions)")
    i.add_argument("input")
    i.add_argument("--template", required=True, help="xlsx or csv whose first row holds the target headers")
    i.add_argument("--key", help="comma separated template columns that identify a row")
    i.add_argument("--filename", help="output file name")
    i.add_argument("--out")
    i.set_defaults(fn=_cmd_draft)
    t = sub.add_parser("test", help="run the sluice test-suite")
    t.set_defaults(fn=_cmd_test)
    demo = sub.add_parser("demo", help="copy the self-contained five-minute example")
    demo.add_argument("--out", default="quickstart")
    demo.set_defaults(fn=_cmd_demo)
    prep = sub.add_parser("prepare", help="draft your own file against a JSON target schema")
    prep.add_argument("input")
    prep.add_argument("--schema", required=True)
    prep.add_argument("--work", required=True)
    prep.add_argument("--sheet", action="append", help="sheet to include; repeat for several sheets")
    prep.add_argument("--param", action="append", metavar="NAME=VALUE")
    prep.add_argument("--filename", default="result.csv")
    prep.add_argument("--store", help="reuse confirmed bindings from a precedent store")
    prep.set_defaults(fn=_cmd_workflow)
    ans = sub.add_parser("answer", help="apply saved chat replies to a prepared workspace")
    ans.add_argument("work")
    ans.add_argument("--reply", action="append", required=True, metavar="SHEET=FILE")
    ans.set_defaults(fn=_cmd_workflow)
    for name in ("review", "approve", "convert"):
        sp = sub.add_parser(name, help=f"{name} a prepared workspace")
        sp.add_argument("work")
        if name == "approve":
            sp.add_argument("--by", required=True)
            sp.add_argument("--reason", required=True, help="review conclusion, including any open questions resolved manually")
        if name == "convert":
            sp.add_argument("--out", default="out")
        sp.set_defaults(fn=_cmd_workflow)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"sluice: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
