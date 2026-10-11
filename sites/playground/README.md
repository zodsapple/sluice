# Sluice Playground

Static Sites frontend with the same Python engine running in a browser module Worker. No backend conversion or model calls.
Selected files live in Worker memory and disappear when the runtime is reset or the page closes.

From the repository root, with Python 3.11+ and Node.js installed:

```bash
python scripts/serve_playground.py
```

Open http://127.0.0.1:8766/ and try the included CSV or Excel example. On Windows, `py` can replace `python`.
The launcher verifies cached runtime assets, downloads missing or damaged ones, rebuilds the engine snapshot and frontend,
and serves only on loopback. The first setup needs internet; subsequent launches and file processing work offline.
Use `--port 8767` if the default port is occupied. Press Ctrl+C to stop. Serve over HTTP rather than opening `index.html` as a file.

For separate build and hosting steps:

```bash
python scripts/fetch_browser_runtime.py
python scripts/bundle_playground.py
node sites/playground/build.mjs
python -m http.server 8766 --directory sites/playground/dist
```

`fetch_browser_runtime.py` fetches pinned Pyodide 314.0.7 and Python wheels from the official Pyodide distribution and PyPI,
verifying the wheels against published SHA-256. The 22 MB runtime is served from the Site's own origin; no CDN or PyPI requests
are needed at execution time. `vendor/` is ignored in the main repository and reproducible with this script.
`vendor/sources.json` records the downloaded sources and hashes. The separate Sites source checkout includes the runtime assets.

`bundle_playground.py` produces a deterministic ZIP snapshot of `sluice/`, excluding tests, plus the small CSV and transport demos.
Re-run it after Python changes. The bundle's SHA-256 is displayed in the version tooltip and recorded in `assets/version.json`.
`build.mjs` only copies static assets and can run independently in the Sites source checkout.

The playground supports CSV/XLSX inputs up to 5 MB, 20,000 raw rows and a 32 MB uncompressed workbook.
It previews output, controls, issues and step traces; human review is required before conversion, with at least one halting reconciliation control.
Changing inputs, templates or YAML clears preview approval. Browser review is session-local; persistent, content-hashed approval is available in the CLI.
Hooks and external file references are disabled here. Files can still contain sensitive values; downloaded reports include issue details and local previews.

The JSON-schema drafter uses the generic workflow module and exports any unresolved copy-paste prompts.
Apply answers in the CLI or edit YAML and validate again. Large files, batch runs, additional external sources and API-based LLM calls belong in the local CLI.

Hosting configuration is in `.openai/hosting.json`. New Sites are owner-private by default.
Publication uses the Sites source helper to commit/push the exact source and package `dist/`; only saved versions are deployed.

Third-party runtime packages retain their licence metadata inside their wheel archives; see [runtime notices](THIRD_PARTY.md).
