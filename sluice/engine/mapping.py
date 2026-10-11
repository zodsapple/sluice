"""Step 2: map input headers to internal names (exact -> alias -> fuzzy)."""
from __future__ import annotations

from difflib import SequenceMatcher

import pandas as pd

from sluice.engine.context import LINEAGE, RunContext
from sluice.engine.diagnosis import EngineError
from sluice.engine.locate import norm


def close_headers(wanted: str, headers: list[str], floor: float = 0.5, top: int = 3) -> list[tuple[str, float]]:
    scored = [(h, round(SequenceMatcher(None, norm(wanted), norm(h)).ratio(), 3)) for h in headers]
    return sorted((x for x in scored if x[1] >= floor), key=lambda x: -x[1])[:top]


def apply_map(df: pd.DataFrame, ctx: RunContext, entries: dict | None = None, cutoff: float | None = None,
              const: dict | None = None) -> pd.DataFrame:
    spec = ctx.spec
    entries = spec.map if entries is None else entries
    cutoff = spec.map_cutoff if cutoff is None else cutoff
    const = spec.const if const is None else const
    headers = [c for c in df.columns if c not in LINEAGE]
    by_norm = {norm(h): h for h in headers}
    used: set[str] = set()
    rename: dict[str, str] = {}
    missing: list[str] = []
    candidates: dict[str, list[tuple[str, float]]] = {}

    for src_key, entry in entries.items():
        found = None
        for name in (src_key, *entry.aliases):
            h = by_norm.get(norm(name))
            if h and h not in used:
                found = h
                break
        if found is None and entry.fuzzy:
            pool = [h for h in headers if h not in used]
            best = close_headers(src_key, pool, floor=cutoff, top=1)
            if best:
                found = best[0][0]
                ctx.issue("header_fuzzy_match", f"Header {found!r} matched {src_key!r} by similarity {best[0][1]}",
                          severity="warning", action="fallback", stage="normalization", field=src_key,
                          raw=found, ratio=best[0][1])
        if found is None:
            missing.append(src_key)
            candidates[src_key] = close_headers(src_key, [h for h in headers if h not in used])
            continue
        used.add(found)
        if entry.to in rename.values():
            ctx.issue("dropped_row", f"Two source columns map to {entry.to!r}; kept the first", field=entry.to,
                      stage="normalization", action="skipped", raw=found)
            continue
        rename[found] = entry.to

    required_missing = [k for k in missing if entries[k].required]
    if required_missing:
        raise EngineError(
            "missing_required_column",
            f"Required column(s) not found in the input: {required_missing}",
            stage="normalization", missing=required_missing, available=headers,
            candidates={k: candidates[k] for k in required_missing}, sheet=ctx.sheet_name,
        )
    for k in missing:
        ctx.issue("missing_configured_column", f"Optional column {k!r} not found; filled empty",
                  severity="info", action="fallback", stage="normalization", field=k)

    out = df.rename(columns=rename)
    for k in missing:
        out[entries[k].to] = pd.NA
    for col, value in const.items():
        if col not in out.columns or out[col].isna().all():
            out[col] = value
    return out
