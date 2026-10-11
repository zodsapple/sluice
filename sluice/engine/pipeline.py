"""Pipeline executor: locate -> map -> shape -> keys -> template -> controls -> write -> report."""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from sluice.engine import mapping, template, validate
from sluice.engine.context import LINEAGE, RunContext
from sluice.engine.diagnosis import Diagnosis, EngineError
from sluice.engine.locate import build_frame, choose_sheet, read_workbook
from sluice.engine.ops import run_op
from sluice.engine.ops.keys import apply_keys
from sluice.report import feedback, log
from sluice.report.issues import PIPELINE_STAGES, REASON_CODES
from sluice.spec.models import PipelineSpec


@dataclass
class RunResult:
    status: str                                  # success | failed
    run_id: str
    spec_name: str
    input_path: str
    output_path: str | None = None
    rows_in: int = 0
    rows_out: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    log_path: str | None = None
    feedback_path: str | None = None
    output_frame: pd.DataFrame | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _diagnose(exc: Exception, ctx: RunContext, mapped: pd.DataFrame | None) -> Diagnosis:
    if isinstance(exc, EngineError):
        d = Diagnosis(stage=exc.stage, code=exc.code, message=str(exc), details=dict(exc.details))
        d.candidates = {k: [tuple(x) for x in v] for k, v in exc.details.get("candidates", {}).items()}
        d.source_headers = list(exc.details.get("available", []))
        if exc.code == "missing_required_column":
            d.suggestions.append("Add the new header as an alias in `map`, or fix the input; `sluice heal` can propose this.")
        if exc.code == "header_not_found":
            d.suggestions.append("Check the file is the right report/sheet; set `locate.header` or `locate.sheet` explicitly.")
        if exc.code == "duplicate_key":
            d.suggestions.append("Set `keys.on_duplicate.conflict` to first/last/sum if repeated keys are expected.")
    else:
        d = Diagnosis(stage="etl", code="processing_failed", message=f"{type(exc).__name__}: {exc}",
                      details={"traceback": traceback.format_exc(limit=6)})
    if mapped is not None and not d.sample_rows:
        d.sample_rows = mapped.drop(columns=list(LINEAGE), errors="ignore").head(3).astype(object).where(
            lambda x: x.notna(), None).to_dict("records")
    return d


def _prepare_source(path: Path, locate, entries, cutoff, const, shape, ctx: RunContext, primary: bool) -> pd.DataFrame:
    """locate -> map -> shape for one input table."""
    key = str(path)
    if key not in ctx.workbook_cache:
        ctx.workbook_cache[key] = read_workbook(path, locate)
    sheets = ctx.workbook_cache[key]
    sheet, _ = choose_sheet(sheets, locate, entries, cutoff)
    previous = ctx.sheet_name
    if primary:
        ctx.workbook_sheets = sheets
    ctx.sheet_name = sheet
    ctx.raw_sheets[sheet] = sheets[sheet]
    df = build_frame(sheets[sheet], sheet, locate, entries, cutoff, ctx)
    df = mapping.apply_map(df, ctx, entries, cutoff, const)
    if primary:
        ctx.stages["mapped"] = df.copy()
    for step in shape:
        df = run_op(step.op, step.params(), df, ctx)
    if not primary:
        ctx.sheet_name = previous
    return df


def run(
    spec: PipelineSpec,
    input_path: str | Path,
    output_dir: str | Path,
    template_path: str | Path | None = None,
    *,
    expect: dict[str, float] | None = None,
    extra_inputs: dict[str, str | Path] | None = None,
    write_output: bool = True,
    write_logs: bool = True,
) -> RunResult:
    input_path, output_dir = Path(input_path), Path(output_dir)
    started = datetime.now(timezone.utc)
    run_id = f"{spec.workflow}_{input_path.stem}_{started.strftime('%Y%m%d_%H%M%S_%f')}"
    ctx = RunContext(spec=spec, input_path=input_path)
    result = RunResult("failed", run_id, spec.name, str(input_path))
    mapped: pd.DataFrame | None = None
    try:
        for src in spec.sources:      # extra tables (other sheet / other file) used by join ops
            src_path = Path((extra_inputs or {}).get(src.name) or src.file or input_path)
            ctx.sources[src.name] = _prepare_source(src_path, src.locate, src.map, spec.map_cutoff, spec.const, src.shape,
                                                    ctx, primary=False)
        df = _prepare_source(input_path, spec.locate, spec.map, spec.map_cutoff, spec.const, spec.shape, ctx,
                             primary=True)
        result.rows_in = len(ctx.stages["mapped"])
        ctx.source_frame = ctx.stages["mapped"]
        mapped = ctx.stages["mapped"]
        ctx.stages["shaped"] = df.copy()
        df = apply_keys(df, ctx)
        ctx.stages["keyed"] = df
        out = template.build_output_frame(df, spec.template, ctx)
        result.rows_out = len(out)
        result.output_frame = out

        results = validate.evaluate_controls(ctx.stages, out, ctx, expect) + validate.evaluate_rules(out, ctx)
        result.controls = [r.as_dict() for r in results]
        halting = [r for r in results if not r.passed and r.on_fail == "halt"]
        if halting:
            d = Diagnosis(
                stage="output_validation", code="validation_failed",
                message="; ".join(f"{r.name}: {'; '.join(r.reasons)}" for r in halting),
                controls=result.controls,
            )
            d.suggestions = [r.hint for r in halting if r.hint]
            d.suggestions.append("See issues for rows dropped before the mismatch; dropped value is included in each control.")
            result.diagnosis = d
        else:
            tpl = Path(template_path) if template_path else (Path(spec.template.path) if spec.template.path else None)
            if write_output:
                dest = output_dir / template.output_name(spec.template, out, input_path.stem, spec.name)
                template.write_output(out, spec.template, tpl, dest)
                result.output_path = str(dest)
            result.status = "success"
    except Exception as exc:   # noqa: BLE001 - every failure becomes a structured diagnosis
        result.diagnosis = _diagnose(exc, ctx, mapped)
        if isinstance(exc, EngineError):
            ctx.issue(exc.code if exc.code in REASON_CODES else "processing_failed", str(exc), severity="error",
                      action="failed_validation", stage=exc.stage if exc.stage in PIPELINE_STAGES else "etl")
        else:
            ctx.issue("processing_failed", result.diagnosis.message, severity="error", action="failed_validation")
    finished = datetime.now(timezone.utc)
    result.traces = [t.as_dict() for t in ctx.traces]
    result.issues = log.finalize_issues(ctx.issues, run_id=run_id, workflow=spec.workflow, input_path=str(input_path),
                                        output_path=result.output_path or "", detected_at=finished)
    payload = {
        "run_id": run_id, "workflow": spec.workflow, "spec": spec.name, "status": result.status,
        "input_path": str(input_path), "output_path": result.output_path, "sheet": ctx.sheet_name,
        "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "rows_in": result.rows_in, "rows_out": result.rows_out, "dropped_amount": ctx.dropped_amount,
        "dropped_rows": ctx.dropped_rows,
        "issue_counts": {s: sum(1 for i in result.issues if i["severity"] == s)
                         for s in ("info", "warning", "error", "critical")} | {"total": len(result.issues)},
        "controls": result.controls, "traces": result.traces,
        "diagnosis": result.diagnosis.to_dict() if result.diagnosis else None,
        "issues": result.issues,
    }
    if write_logs:
        result.log_path = log.write_run_json(output_dir, payload)
        if result.issues:
            log.append_issues_csv(output_dir, result.issues)
        if result.status == "failed" or result.issues:
            fb = Path(result.log_path).with_suffix(".feedback.md")
            fb.write_text(feedback.render_markdown(payload), encoding="utf-8")
            result.feedback_path = str(fb)
    return result
