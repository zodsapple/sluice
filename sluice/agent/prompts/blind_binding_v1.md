# Responder contract `blind-binding-v1`

What a model (or a human) sees, what it returns, and what happens when it does not deliver. Code: `sluice/agent/prompting.py`,
`sluice/infer/bindings.py`, `sluice/agent/blind_resolve.py`. The fixed prompt text is `SYSTEM_PROMPT` in `prompting.py`.

## 1. What goes in (one request per sheet)
Everything is structure or distinct values; **never data rows**.

| block | content | limit |
|---|---|---|
| `schema` | only the target fields being asked: name, type, allowed values, one-line meaning | meaning <= 90 chars |
| `questions` | `{"id","targets"}` per open field; scope questions carry one sentence | - |
| `sources` | the 7 most plausible sources for the asked fields (name, kind, #distinct, header it came from, distinct values) | <= 8 values, **all** values when a source has <= 12 (a mapping needs every key) |
| `header_dimensions` | the distinct labels of each header level (`dim1`, `dim2`) | <= 12 each |
| `cells` | banner cells above the table (title, unit, date) by A1 address | <= 4 cells, <= 160 chars |
| `unit_texts`, `non_numeric_value_tokens` | e.g. `in 1 000`, `X` | <= 3 / <= 6 |
| `already_bound` | field -> short description of how rules bound it | no values |
| `conventions` | the target schema's dimension / date rules | <= 2 sentences |

Whole packet budget: 6,000 characters (shrunk in steps: fewer cells, fewer values). Measured: 4.9-5.9k chars = 1.2-1.5k tokens
per sheet; fixed prompt + two tiny few-shot examples = ~0.5k tokens. Columns named in `healing.redact_columns` send **no values**.
Temperature 0; replies are cached by packet hash, so a re-run costs nothing.

## 2. What must come back
```json
{"answers":[{"question_id":"2:measure","status":"answered","bindings":[
  {"kind":"column","target":"measure","args":{"source":"data_type","mapping":{"FTE":"fte","Headcount":"headcount"}},
   "confidence":0.95,"evidence":["data_type"]}]},
 {"question_id":"2:employment_area","status":"unknown","bindings":[]}]}
```
Binding kinds are closed: `constant`, `column(+mapping)`, `sheet_cell(+mapping|date)`, `template(+where)`, `scale`, `row_scope`,
`grid_scope`, `unknown`. No code, no regex (except inside `where`), no new ops. `unknown` is a first-class answer.

## 3. What is checked before anything is used
1. JSON parses (prose around it is tolerated) and answers only questions that were asked.
2. Each binding against the file: source names exist, mapping keys occur in that source (whitespace-insensitive), cited cells exist,
   `where` columns exist, templates only use known names.
3. Each binding against the target schema: enum values allowed; known vocabularies (`sex`: male/female/all in en/de/fr) cannot be mapped to the wrong class.
4. Confidence >= 0.6, otherwise the binding is **deferred** to a human (kept as a suggestion, not applied).
5. The draft with the accepted bindings must run and every sheet must still yield rows; if not, the smallest set of answers that breaks it is reverted.

## 4. Fallback ladder (nothing here is fatal)
| situation | behaviour |
|---|---|
| no key / endpoint down / timeout | circuit breaker: one failed call, no further calls; deterministic draft + human questions |
| reply is a refusal / no JSON braces | not retried (a retry would cost the same again) |
| reply is broken JSON | **one** repair call with the error text only; then give up on that sheet |
| binding invalid (unknown source, bad enum, invented cell, wrong vocabulary) | that binding is dropped with the reason; the rest of the reply is kept |
| low confidence | deferred to the human with the model's suggestion attached |
| `unknown` | stays an open question (a human decides: leave empty or answer) |
| accepted answers break the run or empty a sheet | reverted one by one, reported |
| call / token budget exhausted | remaining sheets go to the human |

Every binding keeps `origin` (`rule` | `llm` | `human`), `confidence` and `evidence`, so the review screen can show only what the
model decided. A syntactically valid, schema-valid but **semantically wrong** answer (e.g. male/female swapped in a language the
guard does not know) cannot be detected by validation: review of `llm`-origin bindings and declared controls are the defence.
