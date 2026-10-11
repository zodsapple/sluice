# Blind holdout: 8 public files, found on the open web on 2026-10-11

Purpose: test whether the sluice drafter generalises to files nobody tuned it on. Sources are official statistical bodies and
open-data repositories (see `SOURCES.json`: URL, licence, SHA-256 of every raw file). Each case sits in `cases/<id>/`.

Protocol
1. `frozen.json` records the hash of the engine / infer / agent code at download time.
2. For each file I wrote a template (field names, types, one example row, a few plain-language rules), a validation scope and
   `expected.csv` with `tools/build_holdout.py` - plain csv / openpyxl code that does not import sluice.
3. Round 1 (`results/round1_frozen_rules_only.txt`): frozen rules, no human answers, no change to any rule.
4. The failures were then fixed in general mechanisms (list: `frozen.json` -> `post_freeze_changes`), after which rounds 2 and 3
   are NOT blind any more: rules only (`round2_after_fixes_rules_only.txt`) and rules + human answers (`round2_human_answers.txt`,
   answers in `benchmark/realworld/answers/<case>.json`).
5. `prompts/` holds the chat-model prompts of the cases that have open questions (not yet sent to a model).

Reproduce: `RW_PACK=<this folder> py benchmark/realworld/rw.py run <case ...> [--human]`
