# Browser config workflow

Start the local playground with `python scripts/serve_playground.py`, or open the hosted Site.

1. Upload an input and the complete expected CSV/XLSX. The target's first row contains unique column names.
   The page generates JSON containing file hashes, the target schema, source sheets, decisions and executable rules.
2. Open **Resolve open questions**. Each worksheet has a prompt with source facts, target examples and the JSON reply shape.
   **Copy prompt** lets you use your own chatbot; Sluice does not call a model. Review the facts before copying them elsewhere.
3. Paste the chatbot JSON into **Chatbot JSON reply** and choose **Parse reply & update config**. JSON fences are accepted.
   Replies may be partial. Valid decisions accumulate; unknown or low-confidence answers remain open. Invalid question IDs,
   targets or sources reject the whole reply and preserve the previous config. Field dependencies do not depend on reply order.
   `grid_scope` and `row_scope` select source columns/rows, so omit their `target` or use `null`. For scope questions,
   the browser removes a chatbot's extra text `target` with a notice; the actual dimension, values and conditions must still validate.
   Rejected replies show the question ID and reason without a Python traceback.
   Once all questions are resolved, **Revise saved decisions** keeps the reply box available. If validation finds a mismatch,
   paste a correction using the original question IDs; corrected decisions replace their previous bindings and clear approval.
4. Enter remaining decisions manually. Forms support constants, source columns, conditional text, dates, banner cells,
   row scope and value-column scope. The advanced form accepts bindings JSON. Apply each decision explicitly.
5. **Preview & validate** checks the current config and the complete expected result. Unresolved questions, failed rules
   or any expected-row mismatch block conversion. Review the mapping, scope and controls, then approve and convert.
   On failure, **Fix validation with a chatbot** provides **Copy repair prompt** and a text download, per worksheet.
   The prompt contains file facts, current bindings, failed checks and a few expected/output differences. Review it before
   sharing. Differences are paired only when source provenance uniquely identifies rows; other examples are unpaired.
   Paste the returned JSON in the existing reply box and validate again. Partial corrections replace the whole binding
   list for each mentioned question, preserving other decisions. Corrections can include initially confident fields or
   additional row scopes. An unknown or low-confidence correction reopens the decision for manual entry.
   Config/file changes clear the feedback prompt along with validation, so it cannot describe an outdated run.
6. **Export config JSON** saves a portable browser config; **Import config** reloads it against the original file pair.
   **Advanced: YAML rules** also exports the executable spec for CLI use. Browser config JSON is not a standalone CLI spec.

Configs are cached by SHA-256 of both files, file formats, schema and drafting options. Re-uploading the same pair in the
same tab session restores decisions, including after a page refresh. Filenames alone do not identify a file. The cache
retains at most ten configs and never stores source files, output files, validation results or approvals. If session storage
is unavailable or full, it falls back to in-memory caching. Export JSON for use after the tab session ends.

Changing a config, reply, input, target or drafting option clears validation and approval. Config imports check the actual
file hashes and target role. Use **Apply config edits** after editing JSON, or **Revert edits** to return to the applied config.
Unapplied edits cannot be converted. A schema/example describes format; **Expected result** asserts the complete output.

The **One open decision** example exercises prompt, reply and manual-entry flows without uploading a file.
