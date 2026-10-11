"""Gold answers: what a perfect responder (human or model) returns, in the closed binding vocabulary.

They are the upper bound of the 'rules + responder' pipeline and measure whether the vocabulary is SUFFICIENT: if gold
answers cannot reach a PASS, the vocabulary (or the engine behind it) is missing something.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent


def const(t, v): return {"kind": "constant", "target": t, "args": {"value": v}}                          # noqa: E704
def col(t, src, mapping=None, **kw): return {"kind": "column", "target": t, "args": {"source": src, **({"mapping": mapping} if mapping else {}), **kw}}  # noqa: E704,E501
def tpl(t, template, where=None): return {"kind": "template", "target": t, "args": {"template": template, **({"where": where} if where else {})}}  # noqa: E704,E501
def cell(t, ref, mapping): return {"kind": "sheet_cell", "target": t, "args": {"cell": ref, "mapping": mapping}}  # noqa: E704
def unk(t, why="the source has no such dimension"): return {"kind": "unknown", "target": t, "args": {"reason": why}}  # noqa: E704
def rows(conds, mode="all"): return {"kind": "row_scope", "args": {"conditions": conds, "mode": mode}}  # noqa: E704
def grid(dim, include): return {"kind": "grid_scope", "args": {"dimension": dim, "include": include}}  # noqa: E704


# ---- NHS ---------------------------------------------------------------------------------------------------------------
POP = ("Paid substantive HCHS staff directly employed in NHS trusts and other core organisations in England; "
       "excludes primary care and bank/agency staff")
nhs_common = [const("period_basis", "point_in_time"), const("population_scope", POP), const("sex", "all"),
              unk("category_code"), unk("category_level"), unk("employment_area"), unk("employment_relationship")]


def nhs_region(types: dict[str, str]):
    return [grid("dim1", ["All staff groups"]), grid("dim2", ["All staff groups"]),
            rows([{"column": "ics_code", "equals": "All ICS areas"}, {"column": "organisation_code", "equals": "All organisations"}]),
            const("breakdown_dimension", "region"), col("category", "nhs_england_region_name"),
            col("unit", "data_type", types),
            tpl("entity_level", "nhs_england_region"), tpl("entity_level", "country", {"nhs_england_region_code": "England"}),
            col("entity_code", "nhs_england_region_code"), col("entity_name", "nhs_england_region_name"),
            tpl("aggregation_level", "regional_total"), tpl("aggregation_level", "national_total", {"nhs_england_region_code": "England"}),
            *nhs_common]


gold_nhs = {
    "2": nhs_region({"FTE": "fte"}),
    "3": nhs_region({"Headcount": "persons"}),
    "15": [rows([{"column": "staff_group_1", "equals": "All staff groups"}, {"column": "staff_group_1", "same_as": "main_staff_group"}], "any"),
           rows([{"column": "grade", "equals": "All grades"}]),
           col("sex", "dim1", {"All genders": "all", "Female": "female", "Male": "male"}),
           const("breakdown_dimension", "main_staff_group"), col("category", "main_staff_group"),
           col("unit", "data_type", {"FTE": "fte", "Headcount": "persons"}),
           const("entity_level", "country"), const("entity_code", "England"), const("entity_name", "England"),
           tpl("aggregation_level", "main_staff_group_total"), tpl("aggregation_level", "national_total", {"main_staff_group": "All staff groups"}),
           const("period_basis", "point_in_time"), const("population_scope", POP),
           unk("category_code"), unk("category_level"), unk("employment_area"), unk("employment_relationship")],
}

# ---- DE ----------------------------------------------------------------------------------------------------------------
CIVIL = "Beamte/Beamtinnen, Richter/-innen, Berufs- und Zeitsoldaten/-soldatinnen"


def de_sheet(measure_word: str, measure: str, unit: str):
    return [cell("measure", "A2", {measure_word: measure}), cell("unit", "A2", {measure_word: unit}),
            const("period_basis", "point_in_time"), const("population_scope", "DE_public_service"),
            const("breakdown_dimension", "functional_area_haushaltssystematik_2012"),
            col("category", "unnamed_2"), col("category_code", "unnamed_1"),
            tpl("category_level", "subfunction"), tpl("category_level", "major_function", {"unnamed_1": {"regex": r"^\d$"}}),
            tpl("category_level", "total", {"unnamed_1": "0-8"}),
            col("employment_area", "aufgabenbereich_haushaltssystematik_2012",
                {"Öffentlicher Dienst insgesamt": "all_public_service", "dar. Bundesbereich": "federal",
                 "dar. Landesbereich": "state", "dar. kommunaler Bereich": "municipal"}),
            col("employment_relationship", "dim1", {"Insgesamt": "all", CIVIL: "civil_servants_judges_military", "Arbeitnehmer": "employees"}),
            col("sex", "dim2", {"Insgesamt": "all", "Zusammen": "all", "Männer": "male", "Frauen": "female"}),
            unk("entity_level"), unk("entity_code"), unk("entity_name"), unk("aggregation_level")]


gold_de = {"74111-03": de_sheet("Beschäftigte", "headcount", "persons"), "74111-04": de_sheet("Vollzeitäquivalent", "fte", "fte")}

# ---- FR ----------------------------------------------------------------------------------------------------------------
HEAD, FTE = "Effectif au 31 décembre1", "Volume de travail en équivalent temps plein2"
gold_fr = {"Figure 1": [
    grid("dim1", ["2022", "2023"]),
    col("measure", "g1", {HEAD: "headcount", FTE: "fte"}), col("unit", "g1", {HEAD: "persons", FTE: "fte"}),
    col("period_basis", "g1", {HEAD: "point_in_time", FTE: "annual_work_volume"}),
    col("population_scope", "dim2", {"Y compris contrats aidés": "FR_excluding_Mayotte__including_supported_contracts",
                                     "Hors contrats aidés": "FR_excluding_Mayotte__excluding_supported_contracts"}),
    tpl("reference_date", "{dim1}-12-31", {"g1": HEAD}),
    tpl("period_start", "{dim1}-12-31", {"g1": HEAD}), tpl("period_start", "{dim1}-01-01", {"g1": FTE}),
    tpl("period_end", "{dim1}-12-31"),
    const("breakdown_dimension", "public_service_branch"), const("sex", "all"),
    col("category", "versant_de_la_fonction_publique"),
    col("category_code", "versant_de_la_fonction_publique",
        {"Fonction publique de l'État (FPE)": "FPE", "Fonction publique territoriale (FPT)": "FPT",
         "Fonction publique hospitalière (FPH)": "FPH", "Ensemble de la fonction publique": "ALL"}),
    col("category_level", "versant_de_la_fonction_publique",
        {"Fonction publique de l'État (FPE)": "branch", "Fonction publique territoriale (FPT)": "branch",
         "Fonction publique hospitalière (FPH)": "branch", "Ensemble de la fonction publique": "total"}),
    unk("employment_area"), unk("employment_relationship"), unk("entity_level"), unk("entity_code"), unk("entity_name"),
    unk("aggregation_level")]}

for name, doc in (("nhs", gold_nhs), ("de", gold_de), ("fr", gold_fr)):
    (OUT / f"gold_{name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, sum(len(v) for v in doc.values()), "answers")
