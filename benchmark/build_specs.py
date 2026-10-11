"""Generate the two landing-table specs (01 sales, 03 pharmacy) whose control lists come from expected/controls.json."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "sample" / "etl_starter_20261010"
OUT = Path(__file__).parent / "specs"
controls = json.loads((PACK / "expected" / "controls.json").read_text(encoding="utf-8"))


def dump(name: str, doc: dict) -> None:
    (OUT / name).write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")


# ---- 01 Online Retail: landing table, nothing dropped, anomalies counted not removed -------------------------------
c1 = controls["01_sales"]
cols = {"InvoiceNo": "invoice_no", "StockCode": "stock_code", "Description": "description", "Quantity": "quantity",
        "InvoiceDate": "invoice_date", "UnitPrice": "unit_price", "CustomerID": "customer_id", "Country": "country"}
dump("01_sales_retail.yaml", {
    "name": "sales_retail_landing",
    "workflow": "generic",
    "description": "Raw transaction landing. Anomalies (cancellations, negatives, duplicates) are counted, not removed.",
    "locate": {"header": 0},
    "map": {k: {"to": v, "required": True} if k in ("InvoiceNo", "StockCode") else v for k, v in cols.items()},
    "shape": [
        {"op": "strip", "cols": ["invoice_no", "stock_code", "customer_id"]},
        {"op": "parse_date", "cols": ["invoice_date"]},
    ],
    "keys": {"cols": []},          # no stable row id in the source: InvoiceNo + StockCode is not unique
    "template": {
        "filename": "01_sales_landing.csv",
        "columns": {
            "invoice_no": "invoice_no", "stock_code": "stock_code", "description": "description",
            "quantity": {"col": "quantity", "type": "int"}, "invoice_date": "invoice_date",
            "unit_price": {"col": "unit_price", "type": "number"}, "customer_id": "customer_id", "country": "country",
        },
    },
    "controls": [
        {"name": "rows", "output": {"count_rows": True}, "expected": {"value": c1["rows"]}},
        {"name": "null_description", "output": {"count_rows": True, "where": {"description": {"isnull": True}}},
         "expected": {"value": c1["null_counts"]["Description"]}},
        {"name": "null_customer", "output": {"count_rows": True, "where": {"customer_id": {"isnull": True}}},
         "expected": {"value": c1["null_counts"]["CustomerID"]}},
        {"name": "negative_quantity", "output": {"count_rows": True, "where": {"quantity": {"lt": 0}}},
         "expected": {"value": c1["negative_quantity_rows"]}},
        {"name": "cancellation_invoices", "output": {"count_rows": True, "where": {"invoice_no": {"startswith": "C"}}},
         "expected": {"value": c1["cancellation_prefix_rows"]}},
        {"name": "zero_price", "output": {"count_rows": True, "where": {"unit_price": {"eq": 0}}},
         "expected": {"value": c1["zero_unit_price_rows"]}},
        {"name": "negative_price", "output": {"count_rows": True, "where": {"unit_price": {"lt": 0}}},
         "expected": {"value": c1["negative_unit_price_rows"]}},
        {"name": "exact_duplicates_observed", "output": {"duplicates": True},
         "expected": {"value": c1["exact_duplicate_rows_after_first"]}},
    ],
})

# ---- 03 NHS pharmacy: typed landing table, per-month/per-unit controls straight from controls.json -----------------
c3 = controls["03_pharmacy"]
head = (PACK / "inputs" / "03_pharmacy_monthly_long.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(",")
measures = ["ITEMS", "TOTAL_QUANTITY", "NIC"]
dims = [h for h in head if h not in measures]
ctl = [{"name": "rows", "output": {"count_rows": True}, "expected": {"value": c3["input_rows"]}}]
for month, m in c3["by_month"].items():
    where = {"YEAR_MONTH": month}
    ctl += [
        {"name": f"{month}_rows", "output": {"count_rows": True, "where": where}, "expected": {"value": m["rows"]}},
        {"name": f"{month}_items", "output": {"sum": ["ITEMS"], "where": where}, "expected": {"value": m["items"]}},
        {"name": f"{month}_nic_gbp", "output": {"sum": ["NIC"], "where": where},
         "expected": {"value": float(m["nic_gbp"])}},
        {"name": f"{month}_null_snomed", "output": {"count_rows": True, "where": {**where, "SNOMED_CODE": {"isnull": True}}},
         "expected": {"value": m["null_snomed_code"]}},
        {"name": f"{month}_null_supplier",
         "output": {"count_rows": True, "where": {**where, "SUPPLIER_NAME": {"isnull": True}}},
         "expected": {"value": m["null_supplier_name"]}},
    ]
    for unit, qty in m["total_quantity_by_unit"].items():      # quantity only ever summed WITH its unit
        ctl.append({"name": f"{month}_qty_{unit}",
                    "output": {"sum": ["TOTAL_QUANTITY"], "where": {**where, "UNIT_OF_MEASURE": unit}},
                    "expected": {"value": float(qty)}})
dump("03_pharmacy_monthly.yaml", {
    "name": "pharmacy_monthly_landing",
    "workflow": "generic",
    "description": "NHSBSA monthly prescription cost analysis. IDs stay text; quantity is only meaningful with its unit.",
    "locate": {"header": 0},
    "map": {"YEAR_MONTH": {"to": "YEAR_MONTH", "required": True},
            "BNF_PRESENTATION_CODE": {"to": "BNF_PRESENTATION_CODE", "required": True}},
    "shape": [],
    "keys": {"cols": dims, "nullable": ["SNOMED_CODE", "SUPPLIER_NAME"]},
    "template": {
        "filename": "03_pharmacy_landing.csv",
        "columns": {**{h: h for h in dims},
                    "ITEMS": {"col": "ITEMS", "type": "int"},
                    "TOTAL_QUANTITY": {"col": "TOTAL_QUANTITY", "type": "number"},
                    "NIC": {"col": "NIC", "type": "number"}},
    },
    "controls": ctl,
})
print("wrote", sorted(p.name for p in OUT.glob("*.yaml")))
