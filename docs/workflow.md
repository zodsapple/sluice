# Reviewed conversion workflow

Use `sluice prepare INPUT --schema SCHEMA.json --work WORK` with your own file. CSV/XLSX are supported; `.xls` needs the `xls` extra.
All profiled sheets are selected by default. Repeat `--sheet NAME` to choose the extraction scope yourself.
Use `--param NAME=VALUE` for known run parameters, `--filename out.csv` for the target name, and `--store STORE.json` to reuse confirmed bindings.

The JSON schema is exemplified by `sluice/examples/quickstart/schema.json`:

- `fields`: objects containing `name`, `type` (`string`, `decimal`, `integer`, `date`), `nullable`, and optional `enum` and `description`.
- `example_row`: one example showing the intended output.
- `rules`: written business instructions for the reviewer/responder; these are not automatically executable assertions.
- `mode`: `record` by default; use `stats` for dimensional statistical tables.
- `keys`, `controls`, `output_rules`: explicit executable checks, using the pipeline spec vocabulary.

The workspace contains `spec.yaml`, `questions.json`, `review.md`, `review.json`, `bindings.json`, `context.json` and numbered files in `prompts/`.
Prompts include headers, sample values, sheet facts, written rules and the example row. Review their contents before sending them to an external chat service.
Sluice does not call a model in this workflow.

Paste each prompt into your chat model, save its reply, and apply it by the sheet name shown in the packet:

```bash
sluice answer WORK --reply "Data=reply.txt" --reply "Other sheet=other-reply.txt"
sluice review WORK
```

`answer` regenerates the draft and invalidates approval. Apply all sheet replies together; regeneration overwrites manual YAML edits.
Responses use the existing closed binding vocabulary and are validated against the source and schema.
Rejected, unknown and unanswered responses are recorded in `review.json`. Review regenerated YAML and every unresolved question.
If you resolve questions by editing YAML, describe those decisions in the approval reason; `questions.json` remains the drafting record.

```bash
sluice approve WORK --by ana --reason "Confirmed row scope, mappings, and independently supplied controls"
sluice convert WORK --out OUTPUT
```

`run SPEC INPUT` remains available for already reviewed specs without a workspace. Custom Python hooks execute trusted local code;
only use specs you trust. The browser playground disables these hooks and external file references.

## Validation details

Control sums and differences keep their precision. `tol` is a finite nonnegative absolute tolerance; missing declared values,
non-numeric text and infinities in control sums and grouped output sums fail validation. Missing numeric cells are omitted from sums; require `not_null`
or mark a template column `required: true` when emptiness is forbidden. A numeric `range` rule rejects non-numeric text and infinity,
while allowing actual missing values unless another rule forbids them.

Each additive source control records its own dropped values after its declared source stage, respecting `where`.
When shaping changes column names, declare the aggregate over removed rows explicitly, for example:

```yaml
source: {sum: [base_freight, fuel_surcharge]}
dropped: {sum: [cost]}
output: {sum: [Transport Cost, Fuel Cost]}
```

Distinct and duplicate counts are not additive, so no dropped adjustment is inferred for them.
Writes use a temporary sibling file and replace the destination only after successful completion.
