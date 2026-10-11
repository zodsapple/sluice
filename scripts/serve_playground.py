"""Build and serve the browser playground on loopback; reuse verified runtime assets."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "sites" / "playground"


def runtime_ready():
    vendor = SITE / "vendor"
    try:
        manifest = json.loads((vendor / "sources.json").read_text())
        return (manifest["pyodide"] == "314.0.7" and (vendor / "pyodide-lock.json").is_file()
                and all(hashlib.sha256((vendor / f["file"]).read_bytes()).hexdigest() == f["sha256"]
                        for f in manifest["files"]))
    except (OSError, ValueError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        parser.error("Node.js is required to build the static playground")
    if not runtime_ready():
        subprocess.run([sys.executable, str(ROOT / "scripts/fetch_browser_runtime.py"), "--refresh"], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/bundle_playground.py")], check=True)
    subprocess.run([node, str(SITE / "build.mjs")], check=True)
    handler = partial(SimpleHTTPRequestHandler, directory=str(SITE / "dist"))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Sluice playground: http://127.0.0.1:{server.server_port}/\nPress Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
