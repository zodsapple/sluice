# Guided review POC

Open `review.html` after building or serving the playground. It is a separate
experience; the existing upload and JSON/YAML workflow remains available.

The bundled USDA workbook and expected CSV are the same pair used for the
validation-repair check. `assets/review-usda.json` contains previously imported
initial rules with 39 mismatched rows. There are no canned correction replies in
the deployed assets. Users bring replies from their own chatbot; no API key or
automatic model service call is required.

## Try the flow

1. Load the USDA example. Browser Python verifies the file/config identities and
   runs the actual engine and full expected-result validation.
2. Select a difference and enter feedback or use the example feedback. Generate
   and copy the prompt, paste it into a chatbot, and bring back its full reply.
3. Read the reply to stage its actual normalized changes. The backend validates
   the reply and dry-runs current/candidate configs, without committing either
   rules or output. Cards explain every conditional branch and show actual field
   changes and up to three examples. Confirm the whole reply or discard it.
4. Review the results and export CSV or the portable config. Reload the sample
   to restore the tab's session config and run fresh validation.

Normal use requires no configuration reading or editing. The advanced panel only
shows the already-applied config. Invalid replies can be returned to the chatbot
with a copyable repair prompt. Unknown/low-confidence decisions remain open and
block conversion; users provide further business information in feedback.

Feedback is escaped in HTML and quoted as data in the prompt. Editing feedback,
replacing a reply, or loading another config discards the pending proposal.
Confirmation checks that the original config and feedback still match. Replies
apply atomically as a whole, so dependent field rules are not selectively dropped.
The session cache stores only confirmed config, never candidates or approval.
Impact counts cover the full outputs and use only unique source provenance; when
pairing is ambiguous, the UI does not claim per-field or positional row changes.

The prototype currently supports only this fixed sample. Provider login,
automatic chatbot transport and arbitrary-file guided review remain future work.
The original Playground supports other files.

Validation: `test_guided_usda_review_requires_correct_business_choice` in
`sluice/tests/test_browser_adapter.py` exercises the bundled files, staged reply,
unknown/wrong/correct business choices, config round trip and exact CSV comparison.
Additional tests cover staged actual replies, no-op/invalid replies, scope changes,
low confidence, provenance ambiguity, and readable summaries. Known-good chatbot
reply fixtures live under `sluice/tests/fixtures/review`, not deployed assets.
