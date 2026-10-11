"""Write nhs.yaml. Tables 2 and 3 share one layout, so their column map and shape are built once and reused."""
from __future__ import annotations

from pathlib import Path

import yaml

OUT = Path(__file__).parent / "nhs.yaml"

t23_map = {
    "Data month": {"to": "data_month", "required": True},
    "Data type": {"to": "data_type", "required": True},
    "NHS England region code": {"to": "region_code", "required": True},
    "NHS England region name": {"to": "region_name", "required": True},
    "ICS code": {"to": "ics_code", "required": True},
    "Organisation code": {"to": "org_code", "required": True},
    "All staff groups": {"to": "value", "required": True},      # first "All staff groups" column (I) = grand total
}
common_tail = [
    {"op": "parse_date", "cols": ["data_month"], "as_text": True},
    {"op": "map_values", "col": "data_type", "into": "measure", "mapping": {"FTE": "fte", "Headcount": "headcount"}},
    {"op": "map_values", "col": "measure", "into": "unit", "mapping": {"fte": "full_time_equivalents", "headcount": "persons"}},
    {"op": "rename", "columns": {"data_month": "reference_date"}},
]
t23_shape = [
    {"op": "filter", "col": "ics_code", "equals": "All ICS areas", "reason": "nested ICS rows are outside the extraction scope"},
    {"op": "filter", "col": "org_code", "equals": "All organisations", "reason": "organisation rows are outside the extraction scope"},
    {"op": "cell_ref", "header": "All staff groups", "out": "source_cell"},
    *common_tail,
    {"op": "derive", "col": "breakdown_dimension", "template": "region"},
    {"op": "derive", "col": "sex", "template": "all"},
    {"op": "derive", "col": "category", "template": "{region_name}"},
    {"op": "derive", "col": "entity_code", "template": "{region_code}"},
    {"op": "derive", "col": "entity_name", "template": "{region_name}"},
    {"op": "derive", "col": "entity_level", "template": "nhs_england_region"},
    {"op": "derive", "col": "entity_level", "template": "country", "where": {"region_code": "England"}},
    {"op": "derive", "col": "aggregation_level", "template": "regional_total"},
    {"op": "derive", "col": "aggregation_level", "template": "national_total", "where": {"region_code": "England"}},
]
t15_shape = [
    {"op": "flag", "out": "is_group_total",
     "any_of": [{"staff_group": "All staff groups"}, {"staff_group": {"same_as": "category"}}]},     # "Unknown classification" has no sub-groups
    {"op": "filter", "col": "is_group_total", "equals": "True", "reason": "sub-groups are outside the extraction scope"},
    {"op": "filter", "col": "grade", "equals": "All grades", "reason": "grades are outside the extraction scope"},
    {"op": "melt_families", "families": [{"pattern": r"^(All genders|Female|Male)$", "value": "value"}],
     "id": ["data_month", "data_type", "category"], "index": "gender", "cell_col": "source_cell"},
    {"op": "map_values", "col": "gender", "into": "sex", "mapping": {"All genders": "all", "Female": "female", "Male": "male"}},
    *common_tail,
    {"op": "derive", "col": "breakdown_dimension", "template": "main_staff_group"},
    {"op": "derive", "col": "entity_level", "template": "country"},
    {"op": "derive", "col": "entity_code", "template": "England"},
    {"op": "derive", "col": "entity_name", "template": "England"},
    {"op": "derive", "col": "aggregation_level", "template": "main_staff_group_total"},
    {"op": "derive", "col": "aggregation_level", "template": "national_total", "where": {"category": "All staff groups"}},
]
cols = ["source_id", "reference_date", "population_scope", "breakdown_dimension", "category", "sex", "measure", "value", "unit",
        "value_status", "country_code", "entity_level", "entity_code", "entity_name", "aggregation_level"]
key = ["breakdown_dimension", "category", "sex", "measure", "entity_code"]
spec = {
    "name": "nhs_hchs_core",
    "workflow": "generic",
    "description": "Three tables, two layouts -> one observation table. Region headcounts exceed England by 714 and stay that way.",
    "locate": {"sheet": {"name": "2"}, "header": 5},
    "map": t23_map,
    "const": {
        "source_id": "nhs_hchs_core_2026_06",
        "population_scope": "Paid substantive HCHS staff directly employed in NHS trusts and other core organisations in England; "
                            "excludes primary care and bank/agency staff",
        "country_code": "GB", "value_status": "reported_numeric",
    },
    "sources": [
        {"name": "table3", "locate": {"sheet": {"name": "3"}, "header": 5}, "map": t23_map, "shape": t23_shape},
        {"name": "table15", "locate": {"sheet": {"name": "15"}, "header": 4},
         "map": {"Data month": {"to": "data_month", "required": True}, "Data type": {"to": "data_type", "required": True},
                 "Main staff group": {"to": "category", "required": True}, "Staff group 1": {"to": "staff_group", "required": True},
                 "Grade": {"to": "grade", "required": True}},
         "shape": t15_shape},
    ],
    "shape": [*t23_shape, {"op": "concat_source", "source": "table3"}, {"op": "concat_source", "source": "table15"}],
    "keys": {"cols": key},
    "template": {
        "filename": "nhs_out.csv",
        "columns": {**{c: c for c in cols}, "source_sheet": "_src_sheet", "source_cell": "source_cell"},
    },
    "controls": [
        {"name": "rows", "output": {"count_rows": True}, "expected": {"value": 46}},
        {"name": "england_headcount", "expected": {"value": 1536637},
         "output": {"sum": ["value"], "where": {"entity_level": "country", "measure": "headcount", "breakdown_dimension": "region"}}},
        {"name": "fte_regions_sum_to_england", "expected": {"value": 1370441.13073}, "tol": 0.00001,
         "output": {"sum": ["value"], "where": {"entity_level": "nhs_england_region", "measure": "fte"}}},
        # documented in the pack: people with several roles, so region headcounts do NOT add up to England
        {"name": "headcount_regions_sum_kept_as_published", "expected": {"value": 1537351},
         "output": {"sum": ["value"], "where": {"entity_level": "nhs_england_region", "measure": "headcount"}}},
    ],
    "rules": [{"type": "unique", "cols": key, "on_fail": "halt"},
              {"type": "not_null", "cols": ["reference_date", "value", "source_cell"], "on_fail": "halt"}],
}
OUT.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=140), encoding="utf-8")
print("wrote", OUT.name)
