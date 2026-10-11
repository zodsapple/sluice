"""Step 4: declared controls (reconcile source <-> output <-> declared value) and rules."""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

from sluice.engine.context import RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.expr import where_mask
from sluice.spec.models import Agg, Expected, RuleSpec


@dataclass
class ControlResult:
    name: str
    kind: str
    source: float
    output: float
    dropped: float
    expected: float | None
    diff: float
    passed: bool
    on_fail: str
    reasons: list[str] = field(default_factory=list)
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evaluate_agg(df: pd.DataFrame, agg: Agg, where: str) -> float:
    if agg.where:
        df = df[where_mask(df, agg.where)]
    if agg.count_rows:
        return float(len(df))
    if agg.duplicates:
        payload = [c for c in df.columns if c not in ("_src_sheet", "_src_row")]
        return float(df.duplicated(subset=payload, keep="first").sum())
    cols = agg.sum if agg.sum is not None else [agg.count_unique]
    for c in cols:
        if c not in df.columns:
            raise EngineError("control_column_missing", f"Control {where} column {c!r} does not exist",
                              stage="output_validation", available=list(df.columns))
    if agg.sum is not None:
        values = []
        for c in agg.sum:
            raw = df[c]
            n = pd.to_numeric(raw, errors="coerce")
            present = raw.notna() & raw.astype(str).str.strip().ne("")
            invalid = present & (n.isna() | ~n.map(lambda v: pd.isna(v) or math.isfinite(v)))
            if invalid.any():
                raise EngineError("malformed_amount", f"Control {where} column {c!r} contains non-numeric or non-finite values",
                                  stage="output_validation", sample=raw[invalid].head(5).tolist())
            values.extend(n.dropna().tolist())
        return math.fsum(values)
    tokens: set[str] = set()
    splitter = re.compile("|".join(re.escape(s) for s in agg.split)) if agg.split else None
    rx = re.compile(agg.pattern) if agg.pattern else None
    for v in df[agg.count_unique].dropna():
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        for tok in (splitter.split(str(v)) if splitter else [str(v)]):
            tok = tok.strip()
            if rx:
                m = rx.search(tok)
                tok = m.group(0) if m else ""
            if tok:
                tokens.add(tok)
    return float(len(tokens))


def resolve_expected(exp: Expected, ctx: RunContext, override: float | None) -> float | None:
    if override is not None:
        return override
    if exp.value is not None:
        return exp.value
    if exp.cell:
        sheet = exp.cell.get("sheet") or ctx.sheet_name
        raw = (ctx.workbook_sheets or {}).get(sheet)
        if raw is None:
            raise EngineError("sheet_not_found", f"Declared-value sheet {sheet!r} not found", stage="output_validation")
        col, row = coordinate_from_string(exp.cell["ref"])
        v = raw.iat[row - 1, column_index_from_string(col) - 1]
        return float(pd.to_numeric(v, errors="coerce")) if pd.notna(pd.to_numeric(v, errors="coerce")) else None
    if exp.filename:
        m = re.search(exp.filename, ctx.input_path.stem)
        return float(m.group(1)) if m else None
    return None


def evaluate_controls(stages: dict[str, pd.DataFrame], out: pd.DataFrame, ctx: RunContext,
                      expect: dict[str, float] | None = None) -> list[ControlResult]:
    results: list[ControlResult] = []
    for c in ctx.spec.controls:
        o = evaluate_agg(out, c.output, "output")
        override = (expect or {}).get(c.name)
        declared = resolve_expected(c.expected, ctx, override) if c.expected else override
        if c.expected is not None and declared is None:
            raise EngineError("declared_mismatch", f"Control {c.name!r}: declared value could not be read",
                              stage="output_validation")
        if declared is not None and not math.isfinite(declared):
            raise EngineError("declared_mismatch", f"Control {c.name!r}: declared value must be finite",
                              stage="output_validation")
        reasons: list[str] = []
        if c.source is None:                       # declared-only control: the output must match the declared value
            if declared is None:
                raise EngineError("control_column_missing", f"Control {c.name!r} has neither source nor expected value",
                                  stage="output_validation")
            s, dropped, diff = declared, 0.0, o - declared
            if abs(diff) > c.tol:
                reasons.append(f"output {o:.12g} != declared {declared:.12g} (diff {diff:+.10g})")
            kind = "declared"
        else:
            s = evaluate_agg(stages[c.source.stage], c.source, "source")
            is_sum = c.source.sum is not None
            dropped = ctx.dropped_controls.get(c.name, 0.0) if c.allow_dropped else 0.0
            diff = o - (s - dropped)
            if abs(diff) > c.tol:
                reasons.append(f"output {o:.12g} != source {s:.12g} - dropped {dropped:.12g} (diff {diff:+.10g})")
            if c.max_dropped_ratio is not None and s and dropped / s > c.max_dropped_ratio:
                reasons.append(f"dropped value {dropped:.12g} is {dropped / s:.1%} of source, above the "
                               f"{c.max_dropped_ratio:.1%} limit")
            if declared is not None and abs(s - declared) > c.tol:
                reasons.append(f"source {s:.12g} != declared {declared:.12g} (diff {s - declared:+.10g})")
            kind = "sum" if is_sum else ("count_unique" if c.source.count_unique else
                                         "duplicates" if c.source.duplicates else "count_rows")
        passed = not reasons
        res = ControlResult(c.name, kind, s, o, dropped, declared, diff, passed, c.on_fail, reasons, c.hint)
        results.append(res)
        if not passed:
            ctx.issue("reconcile_failed" if abs(diff) > c.tol else "declared_mismatch",
                      f"Control {c.name!r}: " + "; ".join(reasons),
                      severity="error" if c.on_fail == "halt" else "warning", action="failed_validation",
                      stage="output_validation", field=c.name, control=res.as_dict())
    return results


def evaluate_rules(out: pd.DataFrame, ctx: RunContext) -> list[ControlResult]:
    results: list[ControlResult] = []
    required = [h for h, e in ctx.spec.template.columns.items() if e.required]
    rules = [*ctx.spec.rules, *([RuleSpec(name="required_output", type="not_null", cols=required, on_fail="halt")] if required else [])]
    for r in rules:
        name = r.name or f"{r.type}:{','.join(r.cols)}"
        bad = _rule_violations(out, r)
        passed = bad.sum() == 0
        res = ControlResult(name, f"rule:{r.type}", 0.0, float(bad.sum()), 0.0, None, float(bad.sum()), bool(passed),
                            r.on_fail, [] if passed else [f"{int(bad.sum())} output row(s) violate {r.type} on {r.cols}"])
        results.append(res)
        if not passed:
            for idx in out.index[bad][:50]:
                ctx.issue("rule_failed", f"Rule {name!r} violated at output row {idx + 2}",
                          severity="error" if r.on_fail == "halt" else "warning", action="flagged",
                          stage="output_validation", field=",".join(r.cols), row=int(idx) + 2)
    return results


def _rule_violations(out: pd.DataFrame, r: RuleSpec) -> pd.Series:
    for c in r.cols:
        if c not in out.columns:
            raise EngineError("control_column_missing", f"Rule column {c!r} is not an output column",
                              stage="output_validation", available=list(out.columns))
    if r.type == "not_null":
        return out[r.cols].isna().any(axis=1) | (out[r.cols].astype(str).apply(lambda s: s.str.strip() == "").any(axis=1))
    if r.type == "unique":
        return out.duplicated(subset=r.cols, keep=False)
    col = out[r.cols[0]]
    if r.type == "regex":
        return ~col.astype(str).str.fullmatch(r.pattern or ".*", na=False) & col.notna()
    if r.type == "range":
        n = pd.to_numeric(col, errors="coerce")
        bad = col.notna() & (n.isna() | ~n.map(lambda v: pd.isna(v) or math.isfinite(v)))
        if r.min is not None:
            bad |= n < r.min
        if r.max is not None:
            bad |= n > r.max
        return bad
    return ~col.isin(r.values) & col.notna()   # in_set
