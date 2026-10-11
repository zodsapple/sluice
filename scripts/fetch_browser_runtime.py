"""Fetch pinned official runtime assets and verify each wheel's published SHA-256."""
import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

VERSION = "314.0.7"
BASE = f"https://cdn.jsdelivr.net/pyodide/v{VERSION}/full/"
DEST = Path(__file__).resolve().parents[1] / "sites" / "playground" / "vendor"
DEST.mkdir(parents=True, exist_ok=True)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--refresh", action="store_true", help="download again instead of reusing cached files")
args = parser.parse_args()


def fetch(url):
    try:
        with urlopen(url, timeout=60) as response:
            return response.read()
    except URLError as exc:
        raise RuntimeError(f"Download failed: {url}: {exc}") from exc


lock = json.loads(fetch(BASE + "pyodide-lock.json"))
packages = lock["packages"]
for package in packages.values():
    package["depends"] = [name.replace("_", "-") for name in package["depends"]]
urls = {}
for name, version, deps, imports in [("openpyxl", "3.1.5", ["et-xmlfile"], ["openpyxl"]),
                                      ("et-xmlfile", "2.0.0", [], ["et_xmlfile"])]:
    metadata = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
    wheel = next(f for f in metadata["urls"] if f["packagetype"] == "bdist_wheel")
    packages[name] = {"name": name, "version": version, "file_name": wheel["filename"],
                      "sha256": wheel["digests"]["sha256"], "depends": deps, "imports": imports,
                      "install_dir": "site", "package_type": "package"}
    urls[name] = wheel["url"]

needed = set()
def require(name):
    if name in needed:
        return
    needed.add(name)
    for dependency in packages[name]["depends"]:
        require(dependency)
for name in ("pandas", "pydantic", "pyyaml", "openpyxl"):
    require(name)

jobs = [(name, urls.get(name, BASE + packages[name]["file_name"]), packages[name]["file_name"], packages[name]["sha256"])
        for name in sorted(needed)]
jobs += [(name, BASE + name, name, None) for name in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip")]


def download(job):
    name, url, filename, expected = job
    path = DEST / filename
    data = path.read_bytes() if path.exists() and not args.refresh else fetch(url)
    digest = hashlib.sha256(data).hexdigest()
    if expected and digest != expected:
        raise ValueError(f"SHA-256 mismatch: {filename}")
    path.write_bytes(data)
    return {"file": filename, "url": url, "sha256": digest, "bytes": len(data)}


with ThreadPoolExecutor(max_workers=4) as pool:
    manifest = list(pool.map(download, jobs))
lock["packages"] = {name: packages[name] for name in sorted(needed)}
(DEST / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")
(DEST / "sources.json").write_text(json.dumps({"pyodide": VERSION, "files": manifest}, indent=2) + "\n", encoding="utf-8")
print(f"Runtime ready: {len(jobs)} verified assets, {sum(f['bytes'] for f in manifest) / 1024**2:.1f} MB")
