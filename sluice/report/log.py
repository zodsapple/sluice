"""Run logs in the same layout the ctd DQ dashboard reads: _etl_logs/<run>.json + dq_logs/issues.csv."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sluice.report.issues import DQ_LOG_COLUMNS, finalize_issue


def finalize_issues(issues: list[dict[str, Any]], *, run_id: str, workflow: str, input_path: str,
                    output_path: str, detected_at: datetime) -> list[dict[str, Any]]:
    return [
        finalize_issue(i, run_id=run_id, workflow=workflow, input_path=input_path, output_path=output_path,
                       detected_at=detected_at, seq=n)
        for n, i in enumerate(issues)
    ]


def append_issues_csv(output_dir: Path, issues: list[dict[str, Any]]) -> str:
    path = output_dir / "dq_logs" / "issues.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    if not write_header:
        with open(path, newline="", encoding="utf-8") as fh:
            if next(csv.reader(fh)) != DQ_LOG_COLUMNS:
                raise ValueError(f"{path} header does not match the DQ schema")
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DQ_LOG_COLUMNS)
        if write_header:
            w.writeheader()
        w.writerows({c: i.get(c, "") for c in DQ_LOG_COLUMNS} for i in issues)
    return str(path)


def write_run_json(output_dir: Path, payload: dict[str, Any]) -> str:
    path = output_dir / "_etl_logs" / f"{payload['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    return str(path)
