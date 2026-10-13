# Guided review POC

Open `review.html` after building or serving the playground. It is a separate
experience; the existing upload and JSON/YAML workflow remains available.

The bundled USDA workbook and expected CSV are the same pair used for the
validation-repair check. `assets/review-usda.json` contains an initial reply with
39 mismatched rows and a reviewed four-field correction. Chatbot responses are
explicitly simulated; arbitrary natural-language feedback is added to a prompt,
not interpreted by this prototype. No model service is called.

## Try the flow

1. Load the USDA example. Browser Python verifies the file/config identities and
   runs the actual engine and full expected-result validation.
2. Select a difference, enter feedback or use the example feedback, and inspect
   the three suggested changes. Apply all or a subset.
3. Answer the reporting-year question. The report-year option completes the
   correction; the label-year option leaves mismatches; unknown reopens the
   engine's question and blocks conversion.
4. Review the results and export CSV or the portable config. Reload the sample
   to restore the tab's session config and run fresh validation.

The advanced panel accepts real JSON replies using the existing atomic reply
parser. User feedback is escaped in HTML and quoted as data in the repair prompt.
Changing feedback discards its proposal. Any config change invalidates approval.
The cache stores config and business decisions, not validation or approval.

The prototype currently supports only this fixed sample. Provider login,
automatic chatbot transport, general-purpose rule summaries and arbitrary-file
guided review remain future work. The original Playground supports other files.

Validation: `test_guided_usda_review_requires_correct_business_choice` in
`sluice/tests/test_browser_adapter.py` exercises the bundled files, staged reply,
unknown/wrong/correct business choices, config round trip and exact CSV comparison.
