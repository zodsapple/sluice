"""What a human must look at before an LLM-made draft is trusted.

Validation proves an answer is expressible and consistent with the file; it cannot prove it is RIGHT. This module turns the
answers that can silently change the result into a short, ranked list with the numbers a reviewer needs:

  * scope answers (row_scope / grid_scope): how many rows / columns survive, and which values are thrown away
  * a source column copied into a field that normally has its own vocabulary (labels copied without a mapping)
  * a free-text constant (nothing in the schema can check it)
  * low-confidence assumptions made by the rules
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from sluice.engine.expr import where_mask
from sluice.infer.bindings import Binding, TargetField
from sluice.infer.compile import _where
from sluice.infer.frame import Frame

SEVERITY = {"high": 0, "medium": 1, "low": 2}


@dataclass
class ReviewItem:
    sheet: str
    what: str                       # target field or scope kind
    origin: str
    severity: str                   # high | medium | low
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"[{self.severity.upper():6}] {self.sheet}: {self.what} ({self.origin}) - {self.reason}"


def _top(series: pd.Series, n: int = 6) -> dict[str, int]:
    return {str(k): int(v) for k, v in series.astype(str).value_counts().head(n).items()}


def row_scope_preview(frame: Frame, b: Binding) -> dict[str, Any] | None:
    df = frame.data
    if df is None or df.empty:
        return None
    conds = b.args["conditions"]
    if any(c["column"] not in df.columns or (c.get("same_as") and c["same_as"] not in df.columns) for c in conds):
        return None                                                   # refers to reshaped columns: cannot preview here
    masks = [where_mask(df, _where(c)) for c in conds]
    mask = masks[0]
    for m in masks[1:]:
        mask = (mask | m) if b.args.get("mode", "all") == "any" else (mask & m)
    kept = int(mask.sum())
    first = conds[0]["column"]
    return {"kept": kept, "total": int(len(df)), "dropped_values": _top(df.loc[~mask, first]), "column": first}


def grid_scope_preview(frame: Frame, b: Binding) -> dict[str, Any] | None:
    cols = frame.grid_cols
    if not cols:
        return None
    ws = lambda x: re.sub(r"\s+", " ", str(x)).strip()               # noqa: E731
    dim = b.args["dimension"]
    keep = {ws(v) for v in b.args["include"]}
    idx = 0 if dim == "dim1" else 1
    parts = [ws(c.split("|", 1)[idx]) if "|" in c else ws(c) for c in cols]
    kept = sum(1 for p in parts if p in keep)
    dropped = sorted({p for p in parts if p not in keep})
    return {"kept": kept, "total": len(cols), "dropped_values": {d: parts.count(d) for d in dropped[:8]}, "dimension": dim}


def review(frames: list[Frame], per_frame: list[list[Binding]], targets: dict[str, TargetField],
           *, assumption_below: float = 0.7, example: dict[str, Any] | None = None) -> list[ReviewItem]:
    out: list[ReviewItem] = []
    for fr, bs in zip(frames, per_frame):
        reused = [b for b in bs if b.origin == "precedent"]
        if reused:
            out.append(ReviewItem(fr.sheet, "precedent", "precedent", "low",
                                  f"{len(reused)} answer(s) reused: {reused[0].evidence[0] if reused[0].evidence else 'stored earlier'}",
                                  {"fields": sorted({b.target or b.kind for b in reused})}))
        for b in bs:
            if b.kind == "row_scope" and b.origin != "rule":
                pv = row_scope_preview(fr, b)
                if pv is None:
                    out.append(ReviewItem(fr.sheet, "row_scope", b.origin, "medium", "keeps only some rows (preview not available)",
                                          {"conditions": b.args["conditions"]}))
                else:
                    ratio = pv["kept"] / max(pv["total"], 1)
                    sev = "high" if pv["kept"] == 0 or ratio < 0.5 else "medium"
                    out.append(ReviewItem(
                        fr.sheet, "row_scope", b.origin, sev,
                        f"keeps {pv['kept']} of {pv['total']} rows; discards {pv['total'] - pv['kept']} "
                        f"(values of {pv['column']}: {pv['dropped_values']})", pv))
            elif b.kind == "grid_scope" and b.origin != "rule":
                pv = grid_scope_preview(fr, b)
                if pv:
                    out.append(ReviewItem(fr.sheet, "grid_scope", b.origin, "high" if pv["kept"] == 0 else "medium",
                                          f"keeps {pv['kept']} of {pv['total']} value columns; discards {pv['dropped_values']}", pv))
            elif b.kind == "column" and b.origin != "rule":
                t = targets[b.target]
                src = fr.sources.get(b.args["source"])
                if (not b.args.get("mapping") and not t.enum and t.type == "string" and "as printed" not in t.description and src is not None
                        and src.kind in ("dim", "group", "column") and (src.n_distinct or 0) > 1):
                    out.append(ReviewItem(
                        fr.sheet, b.target, b.origin, "medium",
                        f"labels of {b.args['source']} are copied as they are ({src.n_distinct} values, e.g. {src.distinct[:3]}); "
                        "the target usually has its own vocabulary - map them or confirm",
                        {"source": b.args["source"], "values": src.distinct[:6]}))
            elif b.kind == "constant" and b.origin == "llm":
                t = targets[b.target]
                if example and str(example.get(b.target, "")) == str(b.args["value"]) and b.target not in ("source_id",):
                    out.append(ReviewItem(fr.sheet, b.target, b.origin, "high",
                                          f"constant {b.args['value']!r} is the template's example value, copied for every row "
                                          "(nothing in the file supports it)", {"value": b.args["value"]}))
                elif not t.enum:
                    out.append(ReviewItem(fr.sheet, b.target, b.origin, "medium",
                                          f"free-text constant {b.args['value']!r}: nothing in the schema can check it",
                                          {"value": b.args["value"]}))
            if b.origin == "rule" and b.confidence < assumption_below and b.target:
                out.append(ReviewItem(fr.sheet, b.target, "rule", "low", "assumption: " + "; ".join(b.evidence)[:120],
                                      {"confidence": b.confidence}))
    return sorted(out, key=lambda r: (SEVERITY[r.severity], r.sheet, r.what))
