"""Write de.yaml: Destatis tables 74111-03 (headcount) and 74111-04 (FTE) share one layout."""
from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).parent / "de.yaml"
CIVIL = "Beamte/Beamtinnen, Richter/-innen,\nBerufs- und Zeitsoldaten/-soldatinnen"

locate = lambda sheet: {"sheet": {"name": sheet}, "header_rows": [2, 3],            # noqa: E731
                        "skip_rows_matching": ["^in 1 000", "^Ende der Tabelle"]}
the_map = {
    "Aufgabenbereich (Haushaltssystematik 2012)": {"to": "area_label", "required": True},
    "Unnamed: 1": {"to": "category_code", "required": True},
    "Unnamed: 2": {"to": "category", "required": True},
}


def shape(measure: str, unit: str) -> list[dict]:
    return [
        {"op": "strip", "cols": ["area_label", "category_code", "category"]},
        {"op": "fill_down", "cols": ["area_label"]},          # the block label sits on the first row of each block only
        {"op": "melt_families", "id": ["area_label", "category_code", "category"], "index": ["relationship_raw", "sex_raw"],
         "cell_col": "source_cell",
         "families": [{"pattern": rf"^(Insgesamt|{CIVIL.replace('/', '/')}|Arbeitnehmer)\|(Insgesamt|Zusammen|Männer|Frauen)$"
                                  .replace("\n", r"\n"), "value": "source_value", "type": "text"}]},
        {"op": "map_values", "col": "relationship_raw", "into": "employment_relationship",
         "mapping": {"Insgesamt": "all", "Arbeitnehmer": "employees", CIVIL: "civil_servants_judges_military"}},
        {"op": "map_values", "col": "sex_raw", "into": "sex",
         "mapping": {"Insgesamt": "all", "Zusammen": "all", "Männer": "male", "Frauen": "female"}},
        {"op": "map_values", "col": "area_label", "into": "employment_area",
         "mapping": {"Öffentlicher Dienst insgesamt": "all_public_service", "dar. Bundesbereich": "federal",
                     "dar. Landesbereich": "state", "dar. kommunaler Bereich": "municipal"}},
        {"op": "derive", "col": "category_level", "template": "subfunction"},
        {"op": "derive", "col": "category_level", "template": "major_function", "where": {"category_code": {"regex": r"^\d$"}}},
        {"op": "derive", "col": "category_level", "template": "total", "where": {"category_code": "0-8"}},
        {"op": "derive", "col": "measure", "template": measure},
        {"op": "derive", "col": "unit", "template": unit},
        {"op": "derive", "col": "value_status", "template": "reported"},
        {"op": "derive", "col": "value_status", "template": "not_applicable", "where": {"source_value": "X"}},
        {"op": "scale", "col": "source_value", "factor": 1000, "out": "value"},
    ]


cols = ["source_id", "country", "reference_date", "period_start", "period_end", "period_basis", "population_scope",
        "breakdown_dimension", "category", "category_code", "category_level", "employment_area", "employment_relationship",
        "sex", "measure", "value", "unit", "value_status", "source_value", "source_unit", "source_scale"]
key = ["category_code", "employment_area", "employment_relationship", "sex", "measure"]
spec = {
    "name": "de_destatis_personal_2024",
    "workflow": "generic",
    "description": "Destatis public-service staff, two sheets (headcount, FTE) in thousands -> persons / FTE; X stays empty and not_applicable.",
    "locate": locate("74111-03"),
    "map": the_map,
    "const": {
        "source_id": "de_destatis_personal_2024", "country": "DE", "reference_date": "2024-06-30",
        "period_start": "2024-06-30", "period_end": "2024-06-30", "period_basis": "point_in_time",
        "population_scope": "DE_public_service", "breakdown_dimension": "functional_area_haushaltssystematik_2012",
        "source_unit": "thousands", "source_scale": "1000",
    },
    "sources": [{"name": "fte", "locate": locate("74111-04"), "map": the_map, "shape": shape("fte", "fte")}],
    "shape": [*shape("headcount", "persons"), {"op": "concat_source", "source": "fte"}],
    "keys": {"cols": key},
    "template": {"filename": "de_out.csv",
                 "columns": {**{c: c for c in cols}, "source_sheet": "_src_sheet", "source_cell": "source_cell"}},
    "controls": [
        {"name": "rows", "output": {"count_rows": True}, "expected": {"value": 1728}},
        {"name": "not_applicable_cells", "output": {"count_rows": True, "where": {"value_status": "not_applicable"}},
         "expected": {"value": 108}},
        {"name": "not_applicable_have_no_value", "output": {"count_rows": True, "where": {"value_status": "not_applicable", "value": {"notnull": True}}},
         "expected": {"value": 0}},
        {"name": "headcount_public_service_total", "expected": {"value": 5380000},
         "output": {"sum": ["value"], "where": {"category_code": "0-8", "employment_area": "all_public_service",
                                                "employment_relationship": "all", "sex": "all", "measure": "headcount"}}},
        {"name": "fte_public_service_total", "expected": {"value": 4702500},
         "output": {"sum": ["value"], "where": {"category_code": "0-8", "employment_area": "all_public_service",
                                                "employment_relationship": "all", "sex": "all", "measure": "fte"}}},
        {"name": "leading_zero_codes_kept", "expected": {"value": 36},
         "output": {"count_rows": True, "where": {"category_code": "01", "measure": "headcount"}}},
    ],
    "rules": [{"type": "unique", "cols": key, "on_fail": "halt"},
              {"type": "not_null", "cols": ["category_code", "source_cell", "reference_date"], "on_fail": "halt"}],
}
OUT.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=160), encoding="utf-8")
print("wrote", OUT.name)
