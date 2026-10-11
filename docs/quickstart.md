# Five minutes with Sluice

Install from the repository with `python -m pip install .`. On Windows you can use `py` in place of `python`.
If your shell does not expose the installed `sluice` command, use `python -m sluice`.

```bash
sluice demo --out quickstart
sluice run quickstart/spec.yaml quickstart/input.csv --out quickstart/output
```

The input is three synthetic deliveries. The spec copies identifiers and suppliers, parses dates, and validates the
published amounts without rounding them to cents. The independent declared total is 165.5; the declared row count is 3.
The output is `quickstart/output/deliveries.csv`. Its report is `quickstart/output/_etl_logs/<run>.json`.

Try changing `100.125` to `100.129` in a copy of the input and run into a **new output directory**.
The difference of 0.004 exceeds the declared tolerance of 0.000001. The run returns a nonzero exit code,
writes a feedback report and produces no new CSV. An existing output file is preserved on validation or write failure;
check the current run's status rather than assuming an older file is the latest result.

## Draft and approve

```bash
sluice prepare quickstart/input.csv --schema quickstart/schema.json --work quickstart/work
sluice review quickstart/work
sluice approve quickstart/work --by ana --reason "Checked each mapping and the independent total and row count"
sluice convert quickstart/work --out quickstart/reviewed-output
```

`prepare` profiles the file and drafts YAML using the JSON schema's field names, types, rules and one example row.
The example's column names match, so this case needs no answers. For another layout, read `questions.json` and `review.md`,
edit `spec.yaml`, or use the saved prompts and `sluice answer`. Do not treat the example row as a source of per-row constants.

`review` is a dry run. `approve` requires passing validation, at least one halting reconciliation control, a reviewer name
and a written conclusion. This is a local review record, not a cryptographic signature or proof of business correctness.
Changing the source, schema, spec, referenced source files or configured output template invalidates approval.

See [workflow reference](workflow.md) for your own files.
