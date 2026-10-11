"""Per-run state: issues, lineage-aware row dropping, per-step traces."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from sluice.report.issues import make_issue
from sluice.spec.models import PipelineSpec

LINEAGE = ("_src_sheet", "_src_row")
MAX_ROW_ISSUES = 500   # per drop call; a summary issue covers the remainder


@dataclass
class StepTrace:
    step: str
    rows_in: int
    rows_out: int
    amount_in: float
    amount_out: float
    dropped_rows: int = 0
    dropped_amount: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RunContext:
    spec: PipelineSpec
    input_path: Path
    sheet_name: str = ""
    workbook_sheets: dict[str, pd.DataFrame] | None = None   # raw, header=None, for lookup / expected cells
    issues: list[dict[str, Any]] = field(default_factory=list)
    traces: list[StepTrace] = field(default_factory=list)
    dropped_amount: float = 0.0
    dropped_rows: int = 0
    source_frame: pd.DataFrame | None = None
    dropped_controls: dict[str, float] = field(default_factory=dict)
    stages: dict[str, pd.DataFrame] = field(default_factory=dict)      # mapped / shaped / keyed snapshots
    sources: dict[str, pd.DataFrame] = field(default_factory=dict)
    workbook_cache: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)      # one read per file per run
    raw_sheets: dict[str, pd.DataFrame] = field(default_factory=dict)      # sheet name -> raw grid (header=None)
    colrefs: dict[str, dict[str, str]] = field(default_factory=dict)      # sheet -> header name -> column letter     # extra named sources used by join ops

    def issue(self, code: str, message: str = "", **kw: Any) -> None:
        kw.setdefault("sheet", self.sheet_name)
        self.issues.append(make_issue(code, message, **kw))

    def amount_of(self, df: pd.DataFrame) -> pd.Series:
        cols = [c for c in self.spec.measures if c in df.columns]
        if not cols:
            return pd.Series(0.0, index=df.index)
        return sum(pd.to_numeric(df[c], errors="coerce").fillna(0.0) for c in cols)

    def amount_total(self, df: pd.DataFrame) -> float:
        return round(float(self.amount_of(df).sum()), 2)

    def drop_rows(
        self, df: pd.DataFrame, mask: pd.Series, code: str, message: str, *, field: str = "",
        severity: str = "warning", stage: str = "transformation", action: str = "dropped",
        **details: Any,
    ) -> pd.DataFrame:
        """Remove rows where mask is True; every removal is logged with its source row and amount."""
        mask = mask.fillna(False).astype(bool)
        if not mask.any():
            return df
        dropped = df[mask]
        self._record_control_drops(dropped)
        amounts = self.amount_of(dropped)
        for i, (idx, row) in enumerate(dropped.iterrows()):
            if i >= MAX_ROW_ISSUES:
                self.issue(code, f"... and {len(dropped) - MAX_ROW_ISSUES} more rows dropped for the same reason",
                           severity=severity, action=action, stage=stage, field=field,
                           dropped_rows=len(dropped) - MAX_ROW_ISSUES)
                break
            raw = row.get(field) if field in df.columns else None
            self.issue(
                code, message, severity=severity, action=action, stage=stage, field=field,
                sheet=row.get("_src_sheet", self.sheet_name), row=row.get("_src_row", ""),
                raw=None if raw is None or pd.isna(raw) else raw,
                dropped_amount=round(float(amounts.loc[idx]), 2), **details,
            )
        self.dropped_rows += len(dropped)
        self.dropped_amount += float(amounts.sum())
        return df[~mask]

    def _record_control_drops(self, dropped: pd.DataFrame) -> None:
        from sluice.engine.validate import evaluate_agg

        for c in self.spec.controls:
            if c.source is None or not c.allow_dropped or c.source.stage not in self.stages:
                continue   # auxiliary inputs and removals before the declared source stage are outside this control
            if c.source.sum is None and not c.source.count_rows:
                continue   # distinct counts and duplicate counts are not additive
            agg = c.dropped or c.source
            value = evaluate_agg(dropped, agg, f"dropped:{c.name}")
            self.dropped_controls[c.name] = self.dropped_controls.get(c.name, 0.0) + value

    def traced(self, step: str, before: pd.DataFrame, after: pd.DataFrame, d_rows: int, d_amt: float) -> None:
        self.traces.append(StepTrace(
            step, len(before), len(after), self.amount_total(before), self.amount_total(after),
            self.dropped_rows - d_rows, round(self.dropped_amount - d_amt, 2),
        ))
