"""Write cnam.yaml; the control list is copied from the pack's own independent controls (benchmark_manifest.json)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "sample" / "etl_external_reports_20261010_v2"
manifest = json.loads((PACK / "benchmark_manifest.json").read_text(encoding="utf-8"))
pack_controls = manifest["scoring_cases"]["pharmacy_cnam"]["sum_controls"]

text_cols = ["product_short_name", "product_name", "ephmra_label", "atc5_label", "atc2_label"]
header = {  # raw header (whitespace/newlines are ignored when matching) -> internal name
    "CIP13": ("source_cip13", True), "NOM COURT": ("product_short_name", False), "PRODUIT": ("product_name", False),
    "Code EphMRA": ("ephmra_code", False), "Classe EphMRA": ("ephmra_label", False), "Code ATC": ("atc5_code", False),
    "Classe ATC": ("atc5_label", False), "Code ATC 2": ("atc2_code", False), "Libellé ATC 2": ("atc2_label", False),
}
spec = {
    "name": "cnam_medicam_prescriber",
    "workflow": "generic",
    "description": "CNAM MedicAM, all-prescriber sheet: 6 months x 3 metrics across -> one row per product and month.",
    "locate": {"sheet": {"name": "MedicAM_2025mois_tous_presc"}, "header": 0, "skip_rows_matching": ["^total$"]},
    "map": {k: {"to": v, "required": req} for k, (v, req) in header.items()},
    "const": {"country_code": "FR", "prescriber_group": "all", "quantity_unit": "reimbursed_box", "currency": "EUR"},
    "shape": [
        {"op": "strip", "cols": ["source_cip13"]},
        {"op": "melt_families", "id": ["source_cip13", *text_cols, "ephmra_code", "atc5_code", "atc2_code"],
         "index": "reporting_month",
         "families": [
             {"pattern": r"^Base de remboursement\s*(\d{4}-\d{2})$", "value": "reimbursement_base_eur"},
             {"pattern": r"^Nombre de boites remboursées\s*(\d{4}-\d{2})$", "value": "reimbursed_box_count"},
             {"pattern": r"^Montant remboursé\s*(\d{4}-\d{2})$", "value": "reimbursed_amount_eur"}]},
        {"op": "derive", "col": "row_kind", "template": "product"},
        {"op": "derive", "col": "row_kind", "template": "other_aggregate", "where": {"source_cip13": "Autres"}},
        {"op": "derive", "col": "product_code", "template": "{source_cip13}", "where": {"source_cip13": {"ne": "Autres"}}},
        {"op": "flag", "out": "has_negative_measure",
         "any_of": [{c: {"lt": 0}} for c in ("reimbursement_base_eur", "reimbursed_box_count", "reimbursed_amount_eur")]},
        {"op": "flag", "out": "has_suspect_source_text", "any_of": [{c: {"regex": "[¿�]"}} for c in text_cols]},
        {"op": "flag", "out": "has_missing_atc", "any_of": [{"atc5_code": {"isnull": True}}]},
    ],
    "keys": {"cols": ["source_cip13", "reporting_month", "row_kind"], "pattern": r"^(\d{13}|Autres)$"},
    "template": {
        "filename": "cnam_out.csv",
        "columns": {
            "source_sheet": "_src_sheet", "source_row": {"col": "_src_row", "type": "int"}, "country_code": "country_code",
            "reporting_month": "reporting_month", "prescriber_group": "prescriber_group", "row_kind": "row_kind",
            "source_cip13": "source_cip13", "product_code": "product_code", "product_short_name": "product_short_name",
            "product_name": "product_name", "ephmra_code": "ephmra_code", "ephmra_label": "ephmra_label",
            "atc5_code": "atc5_code", "atc5_label": "atc5_label", "atc2_code": "atc2_code", "atc2_label": "atc2_label",
            "reimbursement_base_eur": "reimbursement_base_eur", "reimbursed_amount_eur": "reimbursed_amount_eur",
            "reimbursed_box_count": {"col": "reimbursed_box_count", "type": "int"}, "quantity_unit": "quantity_unit",
            "currency": "currency", "has_negative_measure": "has_negative_measure",
            "has_suspect_source_text": "has_suspect_source_text", "has_missing_atc": "has_missing_atc",
        },
    },
    "controls": [{"name": "rows", "output": {"count_rows": True}, "expected": {"value": 87762}},
                 {"name": "autres_rows", "output": {"count_rows": True, "where": {"row_kind": "other_aggregate"}},
                  "expected": {"value": 6}}] + [
        {"name": c["name"], "output": {"sum": [c["field"]], "where": c["filter"]},
         "expected": {"value": float(c["expected"])}, "tol": max(float(c["absolute_tolerance"]), 0.01)}
        for c in pack_controls],
    "rules": [{"type": "unique", "cols": ["source_cip13", "reporting_month", "row_kind"], "on_fail": "halt"}],
}
(Path(__file__).parent / "cnam.yaml").write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=140), encoding="utf-8")
print("wrote cnam.yaml with", len(spec["controls"]), "controls")
