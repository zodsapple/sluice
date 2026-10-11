"""Bindings -> spec. The only place where answers become ops, so every answer source (rule, human, LLM) is treated alike."""
from __future__ import annotations

import re
from typing import Any

from sluice.infer.bindings import Binding, BindingError, TargetField, _placeholders, validate
from sluice.infer.frame import Frame

AUTO = {"source_sheet": ("column", {"source": "_src_sheet"}), "source_cell": ("column", {"source": "source_cell"})}


def _literal(v: Any) -> str:
    return str(v).replace("{", "{{").replace("}", "}}")


def _where(cond: dict[str, Any]) -> dict[str, Any]:
    if "equals" in cond:
        return {cond["column"]: str(cond["equals"])}
    if "in" in cond:
        return {cond["column"]: {"in": [str(x) for x in cond["in"]]}}
    if "not_in" in cond:
        return {cond["column"]: {"not_in": [str(x) for x in cond["not_in"]]}}
    if "same_as" in cond:
        return {cond["column"]: {"same_as": cond["same_as"]}}
    if "not_regex" in cond:
        return {cond["column"]: {"not_regex": cond["not_regex"]}}
    if "regex" in cond:
        return {cond["column"]: {"regex": cond["regex"]}}
    return {cond["column"]: {"notnull": True}}


def _alt(values: list[str]) -> str:
    return "|".join(re.escape(re.sub(r"\s+", " ", v).strip()).replace(r"\ ", r"\s+") for v in values)


def _restrict_pattern(frame: Frame, scope: dict[str, list[str]]) -> str:
    if frame.layout == "grid":
        a = _alt(scope["dim1"]) if "dim1" in scope else "[^|]+"
        b = _alt(scope["dim2"]) if "dim2" in scope else r"[\s\S]+"
        return rf"^({a})\|({b})$"
    vals = scope.get("dim1") or frame.dims["dim1"]
    return "^(" + _alt(vals) + ")$"


def _used_names(bindings: list[Binding]) -> set[str]:
    """Every column a binding reads: sources, template placeholders, where columns, scope columns, flag conditions."""
    out: set[str] = set()

    def where_cols(w: Any) -> None:
        if isinstance(w, dict):
            out.update(k for k in w if isinstance(k, str))

    for b in bindings:
        a = b.args
        if a.get("source"):
            out.add(a["source"])
        if b.kind == "template":
            out |= _placeholders(a["template"])
            where_cols(a.get("where"))
        if b.kind in ("group_flag", "occurrence"):
            out.update(a.get("by", []))
        if b.kind == "row_scope":
            for c in a["conditions"]:
                out.add(c["column"])
                if c.get("same_as"):
                    out.add(c["same_as"])
        if b.kind == "flags":
            for r in a["rules"]:
                for cond in r["any_of"]:
                    where_cols(cond)
    return out


def frame_ops(frame: Frame, bindings: list[Binding], targets: dict[str, TargetField]) -> list[dict[str, Any]]:
    """Ops for one sheet: structure, scopes, then the target fields."""
    ops = [dict(o) for o in frame.shape]
    keep_raw = {b.args["source"] for b in bindings if b.kind == "column" and b.args.get("verbatim")}      # keep the cell text untrimmed
    for b in bindings:
        if b.kind == "template" and b.args.get("verbatim"):
            keep_raw |= set(b.args["verbatim"])
    for o in ops:
        if o["op"] == "strip" and keep_raw:
            o["cols"] = [c for c in o["cols"] if c not in keep_raw]
    row_scopes = [b for b in bindings if b.kind == "row_scope"]
    grid_scopes = [b for b in bindings if b.kind == "grid_scope"]

    # grid / wide scope: restrict which value columns are melted
    scope: dict[str, list[str]] = {}
    for b in grid_scopes:
        scope.setdefault(b.args["dimension"], []).extend(b.args["include"])
    used = _used_names(bindings)
    keep_plain = {n: h for n, h in frame.wide_numeric.items() if n in used}
    if scope or keep_plain:
        if keep_plain and frame.layout == "wide":
            vals = [v for v in (scope.get("dim1") or frame.dims["dim1"]) if v not in set(keep_plain.values())]
            scope = {**scope, "dim1": vals}
        if keep_plain and frame.layout == "grid":
            ws_ = lambda x: re.sub(r"\s+", " ", x).strip()                                  # noqa: E731
            gone = {ws_(h.split("|", 1)[1]) for h in keep_plain.values()}
            scope = {**scope, "dim2": [v for v in (scope.get("dim2") or frame.dims["dim2"]) if ws_(v) not in gone]}
        for o in ops:
            if o["op"] == "melt_families":
                o["families"] = [{**o["families"][0], "pattern": _restrict_pattern(frame, scope)}]
                o["id"] = list(o.get("id", [])) + [n for n in keep_plain if n not in o.get("id", [])]
        melt_i = next((i for i, o in enumerate(ops) if o["op"] == "melt_families"), None)
        if melt_i is not None and keep_plain:
            ops[melt_i:melt_i] = [{"op": "copy_col", "source": h, "to": n} for n, h in keep_plain.items()]

    # row scope: before the melt when it only needs plain columns (cheaper), else after
    melt_at = next((i for i, o in enumerate(ops) if o["op"] == "melt_families"), None)
    pre, post = [], []
    for k, b in enumerate(row_scopes):
        conds = b.args["conditions"]
        any_of = [_where(c) for c in conds] if b.args.get("mode", "all") == "any" else \
            [{k2: v for c in conds for k2, v in _where(c).items()}]
        flag = f"_keep_{k}"
        pair = [{"op": "flag", "out": flag, "any_of": any_of},
                {"op": "filter", "col": flag, "equals": "True", "reason": "outside the requested scope"}]
        needs_late = any(c["column"] in ("dim1", "dim2", "cell_value") or c.get("same_as") in ("dim1", "dim2", "cell_value")
                         for c in conds)
        (post if needs_late or melt_at is None else pre).extend(pair)
    if melt_at is None:
        ops.extend(pre + post)
    else:
        ops[melt_at:melt_at] = pre
        ops.extend(post)

    # target fields: constants, sheet facts, columns, scales, then templates (which may use the others)
    field_b = [b for b in bindings if b.kind in ("constant", "sheet_cell", "column", "scale", "template", "occurrence", "group_flag", "flags", "period", "arith")]
    done: set[str] = set()
    order = {"constant": 0, "sheet_cell": 1, "column": 2, "scale": 3, "template": 4, "occurrence": 5, "group_flag": 5, "flags": 6, "period": 3, "arith": 5}
    pending = sorted(field_b, key=lambda b: order[b.kind])
    guard = 0
    while pending and guard < 50:
        guard += 1
        rest = []
        for b in pending:
            deps = set()
            if b.kind == "template":
                deps = {p for p in _placeholders(b.args["template"]) if p in targets and p != b.target}
                deps |= {c for c in (b.args.get("where") or {}) if c in targets and c != b.target}
            elif b.kind == "arith":
                deps = {c for c in (b.args["a"], b.args["b"]) if c in targets and c != b.target}
            elif b.kind == "period":
                deps = {b.args["source"]} & set(targets) - {b.target}
            elif b.kind == "group_flag":
                deps = {c for c in b.args["by"] if c in targets and c != b.target}
            elif b.kind == "flags":
                deps = {c for r in b.args["rules"] for cond in r["any_of"] for c in cond if c in targets and c != b.target}
            if deps - done and any(x.target in deps - done for x in pending if x is not b):
                rest.append(b)
                continue
            ops.extend(_binding_ops(b, targets))
            done.add(b.target)
        if len(rest) == len(pending):          # circular: emit as is rather than loop forever
            for b in rest:
                ops.extend(_binding_ops(b, targets))
            break
        pending = rest
    return ops


def _binding_ops(b: Binding, targets: dict[str, TargetField]) -> list[dict[str, Any]]:
    a, t = b.args, b.target
    if b.kind == "constant":
        return [{"op": "derive", "col": t, "template": _literal(a["value"])}]
    if b.kind == "column":
        out: list[dict[str, Any]] = []
        if a.get("datetime_fmt"):                      # a timestamp rendered as text, e.g. %Y-%m-%dT%H:%M:%S
            return [{"op": "copy_col", "source": a["source"], "to": t}, {"op": "format_date", "cols": [t], "fmt": a["datetime_fmt"]}]
        if a.get("strip_quotes"):
            out.append({"op": "regex_replace", "col": a["source"], "pattern": r'^\s*"(.*)"\s*$', "repl": r"\1"})
        if a.get("mapping"):
            out.append({"op": "map_values", "col": a["source"], "into": t, "mapping": a["mapping"]})
        elif a.get("extract"):
            out.append({"op": "extract", "col": a["source"], "to": t, "pattern": a["extract"]})
        elif a["source"] != t:                         # a source already named like the field needs no copy
            out.append({"op": "derive", "col": t, "template": "{" + a["source"] + "}"})
        if targets[t].type in ("decimal", "integer"):
            out.append({"op": "parse_number", "cols": [t], "decimals": 10, **({"integer": True} if targets[t].type == "integer" else {}),
                        **({"decimal": a["decimal"]} if a.get("decimal") else {})})
        if a.get("as_date") or targets[t].type == "date":
            fm = a.get("date_format")
            out.append({"op": "parse_date", "cols": [t], "as_text": True,
                        "formats": [fm] if fm and fm != "iso" else ["%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y%m%d"]})
        return out
    if b.kind == "sheet_cell":
        op = {"op": "sheet_cell", "cell": a["cell"], "out": t}
        if a.get("mapping"):
            op["mapping"] = a["mapping"]
        if a.get("date") or targets[t].type == "date":
            op["date"] = True
        return [op]
    if b.kind == "period":
        return [{"op": "period_bounds", "col": a["source"], "out": t, "part": a["part"]}]
    if b.kind == "arith":
        ops_ = [{"op": "arith", "out": t, "op_name": a["op"], "a": a["a"], "b": a["b"], **({"decimals": a["decimals"]} if "decimals" in a else {})}]
        return ops_
    if b.kind == "flags":
        return [{"op": "flag_list", "out": t, "rules": a["rules"], **({"sep": a["sep"]} if a.get("sep") else {})}]
    if b.kind == "group_flag":
        return [{"op": "group_flag", "by": a["by"], "out": t, "repeated": a["repeated"], "otherwise": a["otherwise"]}]
    if b.kind == "occurrence":
        return [{"op": "occurrence", "by": a["by"], "out": t}]
    if b.kind == "scale":
        ops_ = [{"op": "scale", "col": a["source"], "factor": a["factor"], "out": t, **({"offset": a["offset"]} if a.get("offset") else {}),
                 **({"where": a["where"]} if a.get("where") else {})}]
        if targets[t].type == "integer":
            ops_.append({"op": "parse_number", "cols": [t], "integer": True})
        return ops_
    if b.kind == "template":
        shifts: list[dict[str, Any]] = []
        tpl = a["template"]
        for name, off in dict.fromkeys(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)([+-]\d+)\}", tpl)):
            helper = f"__{name}{'m' if off.startswith('-') else 'p'}{abs(int(off))}"
            shifts += [{"op": "scale", "col": name, "factor": 1, "out": helper, "offset": int(off)},
                       {"op": "parse_number", "cols": [helper], "integer": True}]
            tpl = tpl.replace("{" + name + off + "}", "{" + helper + "}")
        op = {"op": "derive", "col": t, "template": tpl}
        if a.get("where"):
            op["where"] = a["where"]
        return [*shifts, op]
    return []


def _locate_keeping(locate: dict[str, Any], bindings: list[Binding]) -> dict[str, Any]:
    """`keep_rows` labels: drop the skip patterns (total / note rules) that would swallow them."""
    labels = [l for b in bindings if b.kind == "keep_rows" for l in b.args["labels"]]
    if not labels or not locate.get("skip_rows_matching"):
        return locate
    keep = [pat for pat in locate["skip_rows_matching"] if not any(re.search(pat, l, re.I) for l in labels)]
    out = {**locate, "skip_rows_matching": keep}
    if not keep:
        out.pop("skip_rows_matching")
    return out


def compile_spec(frames: list[Frame], per_frame: list[list[Binding]], targets: dict[str, TargetField], *, name: str,
                 filename: str, params: dict[str, str] | None = None, source_id: str = "") -> dict[str, Any]:
    """One spec for all sheets of a source: first sheet is primary, the others are appended with concat_source."""
    columns = {}
    for f in targets:
        columns[f] = {"const": ""}
    produced: set[str] = set()
    sheets: list[dict[str, Any]] = []
    for fr, bs in zip(frames, per_frame):
        ops = frame_ops(fr, bs, targets)
        sheets.append({"frame": fr, "ops": ops})
        produced |= {b.target for b in bs if b.target and b.kind != "unknown"}
    for f in targets:
        if f in produced:
            columns[f] = f
    spec_sources = []
    for k, sh in enumerate(sheets[1:], start=1):
        fr = sh["frame"]
        spec_sources.append({"name": f"s{k}", "locate": _locate_keeping(fr.locate, per_frame[k]), "map": fr.map, "shape": sh["ops"]})
    first = sheets[0]
    shape = list(first["ops"]) + [{"op": "concat_source", "source": f"s{k}"} for k in range(1, len(sheets))]
    return {"name": name, "workflow": "generic", "locate": _locate_keeping(first["frame"].locate, per_frame[0]), "map": first["frame"].map,
            "sources": spec_sources, "shape": shape,
            "template": {"filename": filename, "columns": columns}}


def validate_all(frame: Frame, bindings: list[Binding], targets: dict[str, TargetField]) -> list[tuple[Binding, str]]:
    """Return the bindings that do not hold, with the reason (nothing is raised: callers decide the fallback)."""
    ctx = frame.context(targets)
    bad: list[tuple[Binding, str]] = []
    for b in bindings:
        try:
            validate(b, ctx)
            if b.target:
                ctx.bound.add(b.target)
        except BindingError as exc:
            bad.append((b, str(exc)))
    return bad
