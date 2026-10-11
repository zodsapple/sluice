"""Generic issue model. Schema matches ctd.etl_error_log so the existing DQ dashboard can read it."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

DQ_LOG_COLUMNS: list[str] = [
    "fingerprint", "detected_at", "run_id", "workflow", "pipeline_stage", "severity",
    "reason_code", "message", "action", "input_path", "output_path", "source_sheet",
    "source_row", "field_name", "raw_value", "normalized_value", "details_json",
]

SEVERITIES = frozenset({"info", "warning", "error", "critical"})
ACTIONS = {
    "appended", "dropped", "excluded", "excluded_from_total", "failed_validation", "flagged",
    "fallback", "retained", "rejected", "skipped", "staged", "updated",
}
PIPELINE_STAGES = {
    "etl", "extraction", "finalization", "input_validation", "normalization",
    "output_validation", "preprocessing", "transformation",
}
# Core vocabulary; a workflow plugin / hook can add its own via register_reason_codes().
REASON_CODES: set[str] = {
    "dropped_row", "invalid_key", "missing_key", "duplicate_row", "duplicate_key",
    "filtered_source_row", "skipped_total_row", "malformed_amount", "malformed_date",
    "header_fuzzy_match", "missing_configured_column", "missing_required_column",
    "sheet_not_found", "header_not_found", "input_unreadable", "reconcile_failed",
    "declared_mismatch", "rule_failed", "missing_lookup", "template_mismatch",
    "control_column_missing", "unknown_op", "hook_failed", "processing_failed",
    "validation_failed", "unspecified_issue",
}

_STANDARD = {
    "severity", "reason_code", "code", "message", "action", "pipeline_stage", "source_sheet",
    "source_row", "field_name", "raw_value", "normalized_value", "details",
}


def register_reason_codes(*codes: str) -> None:
    REASON_CODES.update(codes)


def _check(value: str, name: str, allowed: Any) -> str:
    if value not in allowed:
        raise ValueError(f"Unknown DQ {name}: {value!r}")   # a bug in the caller, not data
    return value


def make_issue(
    code: str,
    message: str = "",
    *,
    severity: str = "warning",
    action: str = "flagged",
    stage: str = "etl",
    sheet: str = "",
    row: Any = "",
    field: str = "",
    raw: Any = None,
    normalized: Any = None,
    **details: Any,
) -> dict[str, Any]:
    return {
        "severity": _check(severity, "severity", SEVERITIES),
        "reason_code": _check(code, "reason_code", REASON_CODES),
        "message": message or code.replace("_", " "),
        "action": _check(action, "action", ACTIONS),
        "pipeline_stage": _check(stage, "pipeline_stage", PIPELINE_STAGES),
        "source_sheet": sheet,
        "source_row": row,
        "field_name": field,
        "raw_value": raw,
        "normalized_value": normalized,
        "details": details,
    }


def finalize_issue(
    raw: dict[str, Any], *, run_id: str, workflow: str, input_path: str, output_path: str,
    detected_at: datetime, seq: int,
) -> dict[str, Any]:
    extras = {k: v for k, v in raw.items() if k not in _STANDARD}
    details = {**(raw.get("details") or {}), **extras}

    def blank(v: Any) -> Any:
        return "" if v is None else v

    parts = [detected_at.strftime("%Y%m%d_%H%M%S_%f"), str(seq), raw["reason_code"], raw["action"],
             str(raw.get("source_sheet", "")), str(raw.get("source_row", "")), str(raw.get("field_name", ""))]
    return {
        "fingerprint": "|".join(p for p in parts if p),
        "detected_at": detected_at.isoformat(),
        "run_id": run_id,
        "workflow": workflow,
        "pipeline_stage": raw["pipeline_stage"],
        "severity": raw["severity"],
        "reason_code": raw["reason_code"],
        "message": raw["message"],
        "action": raw["action"],
        "input_path": input_path,
        "output_path": output_path,
        "source_sheet": raw.get("source_sheet", ""),
        "source_row": raw.get("source_row", ""),
        "field_name": raw.get("field_name", ""),
        "raw_value": blank(raw.get("raw_value")),
        "normalized_value": blank(raw.get("normalized_value")),
        "details_json": json.dumps(details, ensure_ascii=True, sort_keys=True, default=str) if details else "{}",
    }
