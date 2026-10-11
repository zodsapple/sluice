# Sluice

A framework for converting Excel/CSV files into a template you define, with reconciliation, review and structured failure reports.

Three principles:

1. **The runtime is deterministic.** A conversion is driven by a YAML spec (data, not code). The same input always gives the same output.
2. **A person approves the spec.** Rules draft it first; whatever is left over is put as questions to a person or a chat LLM. The LLM sees only headers, a few sample values and one example row, **never the file**.
3. **Validate declared constraints.** Control totals reconcile (`source - dropped = output`). Validation failures write reports instead of output. A person still confirms the mapping and business meaning; matching totals alone cannot prove them correct. Row issues are capped at 500 per drop operation, with a summary for the remainder.

## Five-minute example

Python 3.11+. From a source checkout (on Windows, `py` can replace `python`):

```bash
python -m pip install .
sluice demo --out quickstart
sluice run quickstart/spec.yaml quickstart/input.csv --out quickstart/output
```

This self-contained synthetic example has three deliveries and an independently declared total of `165.5`.
It needs no API key or evaluation pack. Output is `quickstart/output/deliveries.csv`; the JSON run report is in `_etl_logs/`.
See [the five-minute walkthrough](docs/quickstart.md), including a deliberately failed conversion.

For your own record schema and input, use the reviewed workflow:

```bash
sluice prepare quickstart/input.csv --schema quickstart/schema.json --work quickstart/work
sluice review quickstart/work
sluice approve quickstart/work --by YOUR_NAME --reason "Reviewed mapping, scope and control totals"
sluice convert quickstart/work --out quickstart/reviewed-output
```

The workspace contains editable YAML, open questions, review items and copy-paste prompts. `sluice answer` applies saved chat replies.
Approval records input, schema and spec hashes; changing any of them requires review again. See [workflow and schema reference](docs/workflow.md).

## Status (read this first)

| Capability | State |
|---|---|
| Run an existing spec (`run` / `run-dir`) | Works. `sluice/specs/` has 2 real examples |
| Draft a spec for a new format | Rules alone pass rarely: blind test on 8 public files, 0/8 with frozen rules, still 0/8 after generic fixes; 8/8 once human answers are added |
| LLM drafting | Copy-paste mode (chat window, no API) is tested; an API client exists but has not been run against a real model |
| Precedent store (reuse confirmed answers) | Works. On a simulated next-year file: 13 of 20 fields wrong -> 0 |
| End-user CLI (prepare -> answer -> review -> approve -> convert) | Available for users' files with a JSON target schema; does not require benchmark scripts |

Drafting is semi-automatic (rules + a person/LLM answering questions). Detailed historical evaluation: `sluice/README.md` and `sample/etl_blind_holdout_20261011/README.md`.

## Install

Python 3.11+ (developed on 3.13; on Windows use `py`).

```bash
python -m pip install -e ".[dev]"
python -m pytest -q       # core tests run without external evaluation packs
```

Optional extras: `.[llm]` for the HTTP client and `.[xls]` for legacy Excel. `.ods` is not supported by the runtime reader.
CI checks Python 3.11/3.13 on Windows/Linux, builds distributions, and runs the installed-package quickstart outside the checkout.

## Three ways to use it

### A. Convert a file with an existing spec

```bash
py -m sluice run sluice/specs/dhl_de_transport.yaml sluice/tests/fixtures/transport/input/DHL_DE_202604_Transport.xlsx --out OUT_DIR --template sluice/tests/fixtures/transport/transport_template.xlsx
py -m sluice dry-run  SPEC INPUT.xlsx --template T.xlsx     # step trace only, nothing written
py -m sluice run-dir  FOLDER --template T.xlsx              # picks the spec by file name
```

On failure nothing is written. Read `_etl_logs/<run>.feedback.md` (plain-language reason) and `dq_logs/issues.csv`.

### B. Draft a spec for a new format (header-only template)

```bash
py -m sluice draft NEW_FILE.xlsx --template TARGET.csv --key col1,col2 --filename out.csv --out spec.yaml
```

Whatever cannot be inferred is listed as a question; nothing is guessed silently. A person reviews the draft, then runs it.

### C. Your own file and target schema

The target JSON describes field names, types, written rules and example values. The generic CLI saves prompts, answers, review evidence and an approval tied to the exact inputs:

```bash
sluice prepare INPUT.xlsx --schema target.json --work work
sluice answer work --reply "Sheet1=answer.json"
sluice review work
sluice approve work --by YOUR_NAME --reason "Reviewed mappings, scope and controls"
sluice convert work --out output
```

Read [the five-minute example](docs/quickstart.md) and [the workflow guide](docs/workflow.md). Drafts need human review; conversion requires passing halting reconciliation controls. Changing the input, schema, spec or referenced template invalidates approval. Historical evaluation helpers remain in `benchmark/`.

## Browser playground

`sites/playground/` runs the same Python engine in a browser Worker using Pyodide. Upload an input plus a CSV/XLSX expected result or template to draft YAML automatically; a JSON schema is optional. A complete expected result checks every output row and blocks mismatches. Review unresolved questions and mappings before conversion. Files stay in browser memory. The first run loads about 22 MB of runtime assets. The playground caps each file at 5 MB and raw input at 20,000 rows; use the CLI for larger jobs or custom Python hooks.

With Python 3.11+ and Node.js, run `python scripts/serve_playground.py` and open http://127.0.0.1:8766/.
Initial setup downloads the runtime; later launches and conversions work offline with the verified cached assets.
See [its README](sites/playground/README.md) for building and hosting.

## Layout

```
sluice/            engine, drafting, review, precedent store, tests (core; details in sluice/README.md)
  engine/          locate -> map -> shape (ops) -> keys -> template -> controls -> write
  infer/           profile (reads structure), frame, columns, bindings, compile, blind (drafting), precedent_store
  agent/           LLM seam: prompts, response validation, review, self-healing (may change how a file is read, never control values)
  specs/           2 real spec examples
  tests/fixtures/  small synthetic transport files used by the tests
benchmark/         evaluation and copy-paste tooling (realworld/ blind/ external/ specs/)
sample/            only the blind holdout's source records, frozen hash, round results and build scripts; the data itself is not in the repo
```

## Sample packs

The public evaluation packs (about 270 MB) are not in the repo. To get them:

* `etl_realworld_noisy_20261011_v3`: the pack and its fetch script are not distributed here; the historical 20-case results cannot yet be reproduced from a clean clone.
* `etl_blind_holdout_20261011`: source URLs and SHA-256 are in `SOURCES.json`; after restoring the matching raw files in `cases/`, templates and expected outputs are generated by `tools/build_holdout.py`. Current URLs may return newer data, so verify the hashes first.
* Run the evaluation: `py benchmark/realworld/rw.py run all --human` (needs the matching pack first).

## Blind-test protocol (to check generalisation yourself)

Freeze a code hash first. Expected outputs come from an independent script that does not import this repo. Round 1 uses no answers. Any fix made afterwards must be labelled "not blind". Example: `sample/etl_blind_holdout_20261011/frozen.json`.

## Contributing

* New capabilities arrive as a binding kind (`sluice/infer/bindings.py`) or an op (`@register_op`). They **must have tests** and are validated before `compile`.
* Do not hard-code a special case for one sample into the rules; express human judgement through `answers/` or the precedent store.
* After changing drafting rules, run `py -m sluice test` and `py benchmark/realworld/rw.py run all --human` to check for regressions.

## License

Code is MIT-licensed (see `LICENSE`). The public data used in the blind test is not included in the repo; its sources and licences are in `sample/etl_blind_holdout_20261011/SOURCES.json`, and its licence terms apply when you use it.
