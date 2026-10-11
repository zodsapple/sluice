# Sluice

A framework for converting Excel/CSV files into a template you define, and proving the conversion is right.

Three principles:

1. **The runtime is deterministic.** A conversion is driven by a YAML spec (data, not code). The same input always gives the same output.
2. **A person approves the spec.** Rules draft it first; whatever is left over is put as questions to a person or a chat LLM. The LLM sees only headers, a few sample values and one example row, **never the file**.
3. **Validation is the judge.** Every dropped row is logged, control totals must reconcile (`source - dropped = output`), and a failed run writes no output, only a structured error report.

## Status (read this first)

| Capability | State |
|---|---|
| Run an existing spec (`run` / `run-dir`) | Works. `sluice/specs/` has 2 real examples |
| Draft a spec for a new format | Rules alone pass rarely: blind test on 8 public files, 0/8 with frozen rules, still 0/8 after generic fixes; 8/8 once human answers are added |
| LLM drafting | Copy-paste mode (chat window, no API) is tested; an API client exists but has not been run against a real model |
| Precedent store (reuse confirmed answers) | Works. On a simulated next-year file: 13 of 20 fields wrong -> 0 |
| End-user CLI (draft -> review -> approve -> run in one flow) | **Not built yet.** Today you go through the scripts under `benchmark/` |

Bottom line: the engine and the validation can be trusted; "automatic drafting" is currently semi-automatic (rules + a person/LLM answering questions). Detailed numbers: `sluice/README.md` and `sample/etl_blind_holdout_20261011/README.md`.

## Install

Python 3.11+ (developed on 3.13; on Windows use `py`).

```bash
pip install -r requirements.txt
py -m sluice test          # 48 core tests; 36 more need the sample data packs and are skipped without them (84 with the packs, ~2 min)
```

`.ods` input needs `odfpy` (not in requirements).

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

### C. Record template (the template carries one example row) + chat-LLM drafting

The more general route. The target is "field names + types + written rules + one example row". Flow:

1. Generate the prompt (structure only, no file):
   `py benchmark/realworld/prompt.py dump <case>`
2. Paste it into a chat window, copy the JSON answer back into a file.
3. Validate and score:
   `py benchmark/realworld/prompt.py apply <case> --reply "<sheet>=answer.txt"`
   This prints a **review list** (what a scope filter keeps/discards, constants copied from the example, low-confidence rule guesses), riskiest first.
4. Once confirmed, save to the precedent store so the next file with the same structure needs no questions:
   `... --store s.json --save --by YOUR_NAME [--confirm "reason"]`

These commands are written per case of the sample packs (`benchmark/realworld/rw.py`). To use your own file you need to write a case description; see `benchmark/realworld/` and the manifest format in `sample/`. **This layer is not productised yet; it is the next piece of work.**

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

* `etl_realworld_noisy_20261011_v3`: `python sample/.../fetch_originals.py` downloads the 20 cases again.
* `etl_blind_holdout_20261011`: source URLs and SHA-256 are in `SOURCES.json`; templates and expected outputs are generated by `tools/build_holdout.py`.
* Run the evaluation: `py benchmark/realworld/rw.py run all --human` (needs the matching pack first).

## Blind-test protocol (to check generalisation yourself)

Freeze a code hash first. Expected outputs come from an independent script that does not import this repo. Round 1 uses no answers. Any fix made afterwards must be labelled "not blind". Example: `sample/etl_blind_holdout_20261011/frozen.json`.

## Contributing

* New capabilities arrive as a binding kind (`sluice/infer/bindings.py`) or an op (`@register_op`). They **must have tests** and are validated before `compile`.
* Do not hard-code a special case for one sample into the rules; express human judgement through `answers/` or the precedent store.
* After changing drafting rules, run `py -m sluice test` and `py benchmark/realworld/rw.py run all --human` to check for regressions.

## License

Code is MIT-licensed (see `LICENSE`). The public data used in the blind test is not included in the repo; its sources and licences are in `sample/etl_blind_holdout_20261011/SOURCES.json`, and its licence terms apply when you use it.
