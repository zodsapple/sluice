"""Bundle the exact Python engine and small demos for the browser; no independent JS engine."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "sites" / "playground"
ASSETS = SITE / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(ASSETS / "sluice.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((ROOT / "sluice").rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_file() and "tests" not in rel.parts and "__pycache__" not in rel.parts and path.suffix in (".py", ".yaml", ".md", ".csv", ".json"):
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
for filename in ("input.csv", "template.csv", "schema.json", "spec.yaml"):
    shutil.copyfile(ROOT / "sluice" / "examples" / "quickstart" / filename, ASSETS / filename)
fixture = ROOT / "sluice" / "tests" / "fixtures" / "transport"
shutil.copyfile(fixture / "input" / "DHL_DE_202604_Transport.xlsx", ASSETS / "transport.xlsx")
shutil.copyfile(fixture / "transport_template.xlsx", ASSETS / "transport-template.xlsx")
shutil.copyfile(ROOT / "sluice" / "specs" / "dhl_de_transport.yaml", ASSETS / "transport.yaml")
(ASSETS / "version.json").write_text(json.dumps({"version": "0.1.0", "sha256": hashlib.sha256((ASSETS / "sluice.zip").read_bytes()).hexdigest()}, indent=2) + "\n", encoding="utf-8")
print("Browser bundle ready:", ASSETS)
