"""Draft a pipeline spec from (file, template headers, key, controls). Deterministic; a human reviews the questions.

The draft is a *proposal*: every decision carries a confidence, and everything the engine could not infer from the
file and the template alone is listed under `questions` instead of being guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from sluice.engine.locate import norm
from sluice.infer.profile import FileProfile, SheetProfile, duplicate_diagnostics, profile_file

SYNONYMS = {
    "qty": "quantity", "quantities": "quantity", "no": "number", "nr": "number", "num": "number", "nbr": "number",
    "desc": "description", "descr": "description", "dept": "department", "emp": "employee", "amt": "amount",
    "dt": "date", "start": "from", "begin": "from", "end": "to", "until": "to", "ym": "yearmonth", "yyyymm": "yearmonth",
}
INDEX_WORDS = {"week", "index", "period", "month", "yearmonth", "year", "day", "idx", "seq"}
CODE_WORDS = {"code", "id", "key"}
NAME_WORDS = {"name", "description", "title", "label"}


def snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z])([A-Z])", r"\1_\2", s).lower()).strip("_")


def tokens(s: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d+", re.sub(r"([a-z])([A-Z])", r"\1 \2", s).lower())
    return {SYNONYMS.get(w, w) for w in words}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    ratio = SequenceMatcher(None, norm(a), norm(b)).ratio()
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    contain = 0.9 if ta and tb and (ta <= tb or tb <= ta) else 0.0
    return round(max(ratio, jac, contain), 3)


@dataclass
class Draft:
    spec: dict[str, Any]
    questions: list[dict[str, Any]] = field(default_factory=list)
    mapping: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        scores = [m["score"] for m in self.mapping if m.get("score") is not None]
        return round(sum(scores) / len(scores), 3) if scores else 0.0


def _best_pairs(targets: list[str], sources: list[str], floor: float = 0.6) -> list[tuple[float, str, str]]:
    pairs = sorted(((similarity(t, s), t, s) for t in targets for s in sources), reverse=True)
    used_t: set[str] = set()
    used_s: set[str] = set()
    out = []
    for score, t, s in pairs:
        if score >= floor and t not in used_t and s not in used_s:
            out.append((score, t, s))
            used_t.add(t)
            used_s.add(s)
    return out


def _coverage(sheet: SheetProfile, targets: list[str]) -> float:
    cols = [c for c in sheet.flat_names if not any(c in f.members for f in sheet.families)]
    direct = len(_best_pairs(targets, cols))
    fam = sum(1 for f in sheet.families for t in targets if similarity(f.prefix, t) >= 0.6)
    return direct + 0.5 * fam


def draft_spec(
    path: str, template_headers: list[str], *, name: str, filename: str,
    keys: list[str] | None = None, on_duplicate: dict[str, Any] | None = None,
    controls: list[dict[str, Any]] | None = None, answers: dict[str, Any] | None = None,
    profile: FileProfile | None = None, examples: dict[str, str] | None = None,
) -> Draft:
    ans = answers or {}
    ex = {k: str(v) for k, v in (examples or {}).items() if v not in (None, "")}
    prof = profile or profile_file(path)
    q: list[dict[str, Any]] = []
    notes: list[str] = []
    mapping: list[dict[str, Any]] = []

    # ---- 1 which sheet is the primary table -------------------------------------------------------------------
    ranked = sorted(prof.sheets.values(), key=lambda s: (-_coverage(s, template_headers), -s.nrows))
    primary = ranked[0]
    others = ranked[1:]

    internal = {t: snake(t) for t in template_headers}
    spec_map: dict[str, Any] = {}
    shape: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    const: dict[str, Any] = dict(ans.get("const", {}))
    done_targets: set[str] = set(ans.get("derived", [])) | set(const)

    fam_members = {m for f in primary.families for m in f.members}
    direct_cols = [c for c in primary.flat_names if c not in fam_members]
    by_name = {c.name: c for c in primary.columns}
    for score, t, s in _best_pairs([t for t in template_headers if t not in done_targets], direct_cols):
        spec_map[s] = {"to": internal[t], "required": t in (keys or [])}
        done_targets.add(t)
        mapping.append({"template": t, "source": s, "score": score, "how": "name"})
    for m in mapping:     # ambiguity: a runner-up almost as good
        alts = sorted((similarity(m["template"], c) for c in direct_cols if c != m["source"]), reverse=True)
        if alts and m["score"] - alts[0] < 0.1:
            q.append({"id": f"ambiguous:{m['template']}", "type": "ambiguous_mapping", "target": m["template"],
                      "chosen": m["source"], "score": m["score"], "runner_up": alts[0]})

    free_s = [c for c in direct_cols if c not in spec_map]
    for t in [t for t in template_headers if t not in done_targets and tokens(t) & NAME_WORDS]:
        cands = [c for c in free_s if by_name[c].dtype == "text" and not tokens(c) & CODE_WORDS]
        if len(cands) == 1:
            spec_map[cands[0]] = {"to": internal[t], "required": False}
            done_targets.add(t)
            free_s.remove(cands[0])
            mapping.append({"template": t, "source": cands[0], "score": 0.55, "how": "name-hint"})
            q.append({"id": f"guess:{t}", "type": "guessed_by_elimination", "target": t, "source": cands[0]})

    # ---- 2 wide column families -> melt ----------------------------------------------------------------------
    def melt_for(sheet: SheetProfile, owner_id_cols: list[str], sheet_label: str, find_index: bool = True) -> tuple[list[dict[str, Any]], str | None, str]:
        fams, index_target, index_type = [], None, "text"
        free = [t for t in template_headers if t not in done_targets]
        for f in sheet.families:
            hit = max(((similarity(f.prefix or sheet_label, t), t) for t in free), default=(0.0, None))
            if hit[0] < 0.6 and f.prefix in ("", "{M}", "{N}"):
                hit = max(((similarity(sheet_label, t), t) for t in free), default=(0.0, None))
            target = hit[1] if hit[0] >= 0.6 else None
            how, score = "family-name", hit[0]
            if target is None:
                numeric_free = [t for t in free if not (tokens(t) & (INDEX_WORDS | CODE_WORDS))]
                if len(numeric_free) >= 1:
                    target, how, score = numeric_free[0], "elimination", 0.5
                    q.append({"id": f"guess:{target}", "type": "guessed_by_elimination", "target": target,
                              "family": f.prefix})
            if target is None:
                q.append({"id": f"family:{f.prefix}", "type": "unmapped_family", "family": f.prefix})
                continue
            free.remove(target)
            done_targets.add(target)
            fams.append({"pattern": f.pattern, "value": internal[target], "type": "number"})
            mapping.append({"template": target, "source": f"family {f.prefix or sheet_label}", "score": score, "how": how})
            index_type = "int" if f.index_kind == "int" else "text"
        if sheet.families and find_index:
            cands = [t for t in free if tokens(t) & INDEX_WORDS]
            if not cands:
                kind_ok = [t for t in free if t not in keys_unmapped]
                cands = kind_ok[:1] if len(kind_ok) == 1 else []
            if cands:
                index_target = cands[0]
                done_targets.add(index_target)
                mapping.append({"template": index_target, "source": "family index", "score": 0.7 if tokens(index_target) & INDEX_WORDS else 0.5,
                                "how": "index"})
            else:
                q.append({"id": "family_index", "type": "unmapped_index", "message": "which template column holds the column-family index?"})
        return fams, index_target, index_type

    keys_unmapped: set[str] = set()
    index_col = None
    if primary.families:
        id_cols = [m["to"] for m in spec_map.values()]
        fams, index_target, index_type = melt_for(primary, id_cols, primary.name)
        if fams and index_target:
            index_col = internal[index_target]
            shape.append({"op": "melt_families", "families": fams, "id": id_cols, "index": index_col, "index_type": index_type})
        # companion sheets that share the same identifying columns and hold more measures
        for sh in others:
            shared = [c for c in sh.flat_names if any(norm(c) == norm(s) for s in spec_map)]
            if len(shared) >= 2 and sh.families:
                smap = {c: {"to": next(v["to"] for k, v in spec_map.items() if norm(k) == norm(c)), "required": True}
                        for c in shared}
                sfams, sidx, _ = melt_for(sh, [v["to"] for v in smap.values()], sh.name, find_index=False)
                if sfams and index_col:
                    s_entry: dict[str, Any] = {
                        "name": snake(sh.name), "locate": _locate_dict(sh), "map": smap,
                        "shape": [{"op": "melt_families", "families": sfams, "id": [v["to"] for v in smap.values()],
                                   "index": index_col, "index_type": "text"}]}
                    sources.append(s_entry)
                    shape.append({"op": "join_source", "source": snake(sh.name),
                                  "by": [*[v["to"] for v in smap.values()], index_col], "how": "outer"})
                    notes.append(f"joined sheet {sh.name!r} on {[v['to'] for v in smap.values()]} + {index_col}")
        # an index target that was claimed by a family index must not stay "free"
    # ---- 3 group header rows -> context columns -----------------------------------------------------------------
    if primary.group_rows and primary.group_pattern:
        label_col = primary.flat_names[0]
        free = [t for t in template_headers if t not in done_targets]
        sample = primary.group_rows[0][1]
        parts = [p.strip() for p in sample.split("|", 1)]
        names = {}
        for g, part in zip(("g1", "g2"), parts):
            code_like = bool(re.search(r"\d", part)) and len(part) <= 12
            want = CODE_WORDS if code_like else NAME_WORDS
            hit = next((t for t in free if tokens(t) & want and t not in names.values()), None)
            if hit:
                names[g] = hit
                done_targets.add(hit)
        if len(names) == 2 and label_col in spec_map:
            pat = primary.group_pattern
            for g, t in names.items():
                pat = pat.replace(f"?P<{g}>", f"?P<{internal[t]}>")
            shape.insert(0, {"op": "group_context", "col": spec_map[label_col]["to"], "pattern": pat})
            for t in names.values():
                mapping.append({"template": t, "source": "group row", "score": 0.7, "how": "group"})
        else:
            q.append({"id": "group_rows", "type": "unmapped_group", "sample": sample})

    # ---- 4 cleaning ops from the column profiles -------------------------------------------------------------------
    id_like = [v["to"] for s, v in spec_map.items() if s in by_name and (by_name[s].dtype in ("id_text",) or
               v["to"] in {internal[k] for k in (keys or [])})]
    if id_like:
        shape.insert(0 if not shape or shape[0]["op"] != "group_context" else 1, {"op": "strip", "cols": id_like})
    date_cols = {s: by_name[s] for s in spec_map if s in by_name and by_name[s].dtype == "date"}
    if date_cols:
        formats: list[str] = []
        for c in date_cols.values():
            formats += [f for f in c.date_formats if f not in formats]
        tokens_seen = {t for c in date_cols.values() for t in c.non_date_tokens}
        as_text = any(c.far_future or c.non_date_tokens for c in date_cols.values())
        op: dict[str, Any] = {"op": "parse_date", "cols": [spec_map[s]["to"] for s in date_cols]}
        if formats:
            op["formats"] = formats + (["%Y-%m-%d %H:%M:%S"] if "%Y-%m-%d" in formats else [])
        if as_text:
            op["as_text"] = True
        sent = ans.get("sentinels")
        if tokens_seen:
            if sent:
                op["sentinels"] = sent
            else:
                q.append({"id": "sentinel", "type": "unknown_date_token", "tokens": sorted(tokens_seen),
                          "message": "non-date values inside date columns: which date do they stand for?"})
        shape.append(op)
    # ---- 4b the template's example row: formats, types and constant candidates -----------------------------------------
    measure_cols = [f["value"] for o in shape if o["op"] == "melt_families" for f in o["families"]] + \
                   [f["value"] for src in sources for o in src["shape"] if o["op"] == "melt_families" for f in o["families"]]
    for t in template_headers:
        e = ex.get(t)
        if e is None:
            continue
        if index_col and internal[t] == index_col and re.fullmatch(r"\d{6}", e):
            shape.append({"op": "regex_replace", "col": index_col, "pattern": "-", "repl": ""})
            mapping.append({"template": t, "source": "example", "score": 0.8, "how": "period-format yyyymm"})
        elif re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", e) and (t in done_targets or tokens(t) & {"date", "from", "to"}):
            shape.append({"op": "format_date", "cols": [internal[t]], "fmt": "%d.%m.%Y"})
            mapping.append({"template": t, "source": "example", "score": 0.8, "how": "date-format dd.mm.yyyy"})
        if t not in done_targets:
            asof_like = re.fullmatch(r"\d{4}-\d{2}-\d{2}", e) and (norm(t) in {"asof", "snapshotdate", "reportdate", "period"} or "asof" in norm(t))
            flag_like = e in ("0", "1") and tokens(t) & {"present", "flag", "has", "exists", "available"} and measure_cols
            if asof_like:
                const[internal[t]] = e
                done_targets.add(t)
                mapping.append({"template": t, "source": "example constant", "score": 0.5, "how": "assumed-from-example"})
                q.append({"id": f"assumed:{t}", "type": "assumed_from_example", "target": t, "value": e,
                          "message": "constant taken from the template example row - confirm it is the same for every run"})
            elif flag_like:
                shape.append({"op": "flag_present", "cols": measure_cols, "out": internal[t]})
                done_targets.add(t)
                mapping.append({"template": t, "source": "example flag", "score": 0.5, "how": "assumed-from-example"})
                q.append({"id": f"assumed:{t}", "type": "assumed_from_example", "target": t,
                          "message": f"0/1 flag assumed to mean any of {measure_cols} present in the source"})

    shape.extend(ans.get("extra_shape", []))

    # ---- 5 template, unmapped targets, keys, duplicates ---------------------------------------------------------------
    for t in template_headers:
        if t not in done_targets:
            cands = [(round(sim, 2), c) for sim, c in sorted(((similarity(t, c), c) for c in primary.flat_names), reverse=True)[:3]]
            q.append({"id": f"unmapped:{t}", "type": "unmapped_target", "target": t, "candidates": cands,
                      **({"example": ex[t]} if t in ex else {})})
    columns: dict[str, Any] = {}
    for t in template_headers:
        src_prof = next((by_name[s] for s, v in spec_map.items() if v["to"] == internal[t] and s in by_name), None)
        col: dict[str, Any] = {"col": internal[t]}
        if src_prof is not None:
            if src_prof.dtype == "int":
                col["type"] = "int"
            elif src_prof.dtype == "decimal":
                col["type"] = "number"
        elif any(f.get("value") == internal[t] for op in shape if op["op"] == "melt_families" for f in op["families"]):
            col["type"] = "int" if all(float(x).is_integer() for x in _fam_sample(primary, internal[t], shape)) else "number"
        elif ex.get(t) is not None and "type" not in col:
            if re.fullmatch(r"-?[1-9]\d*|0", ex[t]):
                col["type"] = "int"
            elif re.fullmatch(r"-?\d+\.\d+", ex[t]):
                col["type"] = "number"
        columns[t] = col if len(col) > 1 else internal[t]
    if index_col:
        columns = {t: ({**c, "type": "int"} if isinstance(c, dict) and c["col"] == index_col else
                       ({"col": index_col, "type": "int"} if c == index_col and shape and shape[-1:] and
                        any(o.get("index_type") == "int" for o in shape if o["op"] == "melt_families") else c))
                   for t, c in columns.items()}

    key_cols = [internal[k] for k in (keys or [])]
    spec: dict[str, Any] = {
        "name": name, "workflow": "generic", "locate": _locate_dict(primary), "map": spec_map, "const": const,
        "sources": sources, "shape": shape,
        "keys": {"cols": key_cols, **({"on_duplicate": on_duplicate} if on_duplicate else {})},
        "template": {"filename": filename, "columns": columns},
        "controls": controls or [],
        "rules": ([{"type": "unique", "cols": list(keys), "on_fail": "halt"}] if keys else []),
    }
    # Safety net: a column that is never empty in the source must not come out empty (unparsed dates, lost joins, ...).
    full = [t for t in template_headers
            if any(v["to"] == internal[t] and s_ in by_name and by_name[s_].null_rate == 0 for s_, v in spec_map.items())]
    if full:
        spec["rules"] = [*spec["rules"], {"type": "not_null", "cols": full, "on_fail": "halt"}]
    if keys and not on_duplicate and primary.data is not None:
        src_of = {v["to"]: k for k, v in spec_map.items()}
        if all(internal[k] in src_of for k in keys) and not primary.families and not primary.group_rows:
            diag = duplicate_diagnostics(primary.data, [src_of[internal[k]] for k in keys])
            if diag["exact_duplicate_rows"] or diag["key_clash_groups"]:
                q.append({"id": "duplicates", "type": "key_not_unique", **diag,
                          "choices": ["exact: drop|keep", "conflict: error|first|last|sum|latest(order_by)|earliest(order_by)"],
                          "message": "the declared key repeats in the source; choose how to deduplicate (or filter to a snapshot)"})
    if not keys:
        q.append({"id": "key", "type": "no_key", "message": "no key given: nothing prevents duplicate rows"})
    if not controls:
        q.append({"id": "controls", "type": "no_controls", "message": "no validation ranges chosen",
                  "suggestions": [{"output": {"count_rows": True}}]})
    spec = {k: v for k, v in spec.items() if v not in ({}, [])}
    covered = {c for ctl in (controls or []) for a in (ctl.get("output", {}).get("sum", []), [ctl.get("output", {}).get("count_unique")]) for c in a if c}
    covered |= set(keys or [])
    cov = {"template_columns": len(template_headers), "covered_by_control_or_key": sorted(covered & set(template_headers)),
           "uncovered": [t for t in template_headers if t not in covered]}
    return Draft(spec, q, mapping, cov, notes)


def _fam_sample(sheet: SheetProfile, value: str, shape: list[dict[str, Any]]) -> list[float]:
    for op in shape:
        if op["op"] != "melt_families":
            continue
        for f in op["families"]:
            if f["value"] == value:
                members = next((fam.members for fam in sheet.families if fam.pattern == f["pattern"]), [])
                if members and sheet.data is not None and members[0] in sheet.data.columns:
                    vals = []
                    for v in sheet.data[members[0]].dropna().head(200):
                        try:
                            vals.append(float(v))
                        except (TypeError, ValueError):
                            return [0.5]
                    return vals or [0.0]
    return [0.5]


def _locate_dict(sheet: SheetProfile) -> dict[str, Any]:
    loc: dict[str, Any] = {"sheet": {"name": sheet.name}}
    if len(sheet.header_rows) > 1:
        loc["header_rows"] = sheet.header_rows
    else:
        loc["header"] = sheet.header_rows[0]
    if sheet.data_start != max(sheet.header_rows) + 1:
        loc["data_start"] = sheet.data_start
    pats = []
    for _, label, kind in sheet.skip_labels:
        pats.append(f"^{re.escape(label)}$" if kind == "repeated_header" else "^" + re.escape(label[:18]))
    if pats:
        loc["skip_rows_matching"] = sorted(set(pats))
    return loc
