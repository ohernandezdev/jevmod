# ai_generated becomes a slop rule

**Not shipped, by decision on 2026-09-25: see the end.** `ai_generated` keeps the authorship question.

Written 2026-09-25. Omar approved shipping formulation D (`benchmark/ai_detect/REPORT3.md`) as the
behaviour of the `ai_generated` category ("AI slop" on the site), because AI slop is the reason many
communities that do not want AI will pick jevmod.

## Objective

`ai_generated` asks Jev the slop question (does this text use the writing patterns of an AI assistant,
whoever typed it) instead of the authorship question (was it written by a model). Same key, same threshold
0.85, still off by default, still flag only, still experimental.

## Why

The shipped authorship question flags people who write like an encyclopedia (74% of Wikipedia-style human
answers at 0.85) and projects to 0.19 to 0.37 precision at a realistic rate. Formulation D, measured alone
in shuffled 25%-AI batches, fired on 0 of 90 real chat messages, 0 of 60 encyclopedia texts, 0 of 300
non-native learners, 13 of 697 careful humans (all doctors writing assistant boilerplate), and 288 of 1000
assistant answers.

## The premise that is not yet proven

REPORT3 asked D as the only question in the request. In the product it is asked beside eight other
categories in one batch, and REPORT2 showed that what shares a request moves the score. So D is measured
again **through Judge, unpadded, no channel context, all nine categories**, shuffled 25%-AI batches of 25, on the same 1047
texts, before anything ships.

## Acceptance criteria, written before the run

At threshold 0.85, through `Judge` with all nine categories:

| set | n | must be |
|---|---|---|
| real chat | 90 | 0 fires |
| non-native learners | 300 | at most 3 (1%) |
| encyclopedia style | 60 | at most 3 (5%) |
| careful humans | 697 | at most 3% |
| assistant answers | 260 | at least 20% fire |

If any row fails, nothing ships; the result is written here and reported. Cost estimate: under $0.10.

## Scope

`jevmod/categories.json` (label, instructions, criteria, warning), the JS package copy through its sync
script, the policy comment, docs that describe the category (README, BENCHMARK, CHANGELOG, AGENTS where they
describe it), tests. The threshold, the off default, the flag-only limit and `EXPERIMENTAL` stay.

Not committed from the agent session (`AGENTS.md`). Delivery (push to main rebuilds the production bot) is
Omar's call.

## Tasks

- [x] T1 run the measurement through `Judge` with D in place, against the criteria
- [x] T2 if it passes: the category change, JS sync, docs, tests; ruff, mypy, pytest, JS tests.
  **Reverted by decision, 2026-09-25**: every T2 edit to the category, the JS copy, the policy comments,
  CHANGELOG, AGENTS, the OpenAPI description and the test is back to HEAD. REPORT4, `through_judge.py` and its
  raw results stay.
- [x] T3 red-team: its corrections to what the pass rests on are in the T1 evidence below and in REPORT4
- [ ] ~~T4 re-measure the site's `aislop` sample~~ dropped with the decision: the site describes the question
  that ships

## Evidence

### T1, 2026-09-25: passes, the careful-human row by one text

D put into `ai_generated` in `jevmod/categories.json` (instructions templated with `{m}`, criteria from
`slop.py` with `PATTERNS` inlined; checked equal to `questions_d("{m}")` in code). Measured with
`benchmark/ai_detect/through_judge.py`: the 1047 texts of `slop.load_items()`, shuffled with REPORT3's seed
20260919 into requests of 25 (same membership as `slop ask-mixed`), through Judge, unpadded, no channel
context, all nine categories: `m0` the lead filler, default topic. Production also pads with the channel's recent
messages, attaches the conversation and sends the channel's own topic; none of that was in these requests. Strata are REPORT3's (`side`/`stratum` from
`load_items`). Full write-up: `benchmark/ai_detect/REPORT4.md`.

| set | n | fires at 0.85 | must be | result | same requests again |
|---|---|---|---|---|---|
| real chat | 90 | 0 | 0 | pass | 0 |
| non-native learners | 300 | 0 | at most 3 | pass | 0 |
| encyclopedia style | 60 | 2 | at most 3 | pass | 1 |
| careful humans | 697 | 20 (2.87%) | at most 3% (20.9) | pass | 19 |
| assistant answers | 260 | 83 (31.9%) | at least 20% | pass | 85 |

- `python -m benchmark.ai_detect.through_judge ask d`: 42 requests, 1,469,519 input tokens, $0.0617.
- `ask d_retest` (identical requests again): $0.0617, 9 of 1040 texts change side of 0.85.
- `ask authorship` (old question, same run, for comparison): $0.0594. Fires 44/60 encyclopedia, 58/697 careful,
  244/260 AI. The other eight categories: 0 texts cross any threshold between the two runs.
- 7 short texts skipped by the pre-filter, as production would; none AI.
- The careful-human row passes only on the pool that includes the 300 non-native learners, none of whom fires.
  Careful humans excluding non-native: 20/397 = 5.0% (retest 19/397 = 4.8%), above the 3% criterion.
- Medicine stratum: 16/60 fire under D vs 0/60 under the authorship question. Health and support communities
  will see flags on replies that refer people to a professional.
- Margin: 21 more careful humans sit between 0.80 and 0.85. All 20 human fires were read: 16 medical Q&A
  doctors (greeting boilerplate, referral to a specialist), 3 encyclopedia definitional openers, 1 finance
  staging.

### T2, 2026-09-25 (reverted by decision the same day)

Label, warning (numbers from REPORT4), `policy.py` comments, JS copy via `node scripts/sync-categories.mjs`
(byte-identical), `policy.ts` comment, `AGENTS.md` defaults line, `CHANGELOG.md` Unreleased, `gen_openapi.py`
description and `api/openapi.json` regenerated, `benchmark/ai_detect/README.md` and REPORT3 pointers, test
`test_ai_generated_asks_the_slop_question_not_authorship` added and the old authorship assertion removed.
README and BENCHMARK.md do not describe what the category asks, so they are unchanged.

- `ruff check .`: All checks passed
- `mypy jevmod`: Success, no issues found in 21 source files
- `pytest tests -q -k "not redteam"`: 282 passed, 10 deselected
- `packages/jevmod-js`: `npm run typecheck` clean; `npm test` 6 files, 46 tests passed

## Decision, 2026-09-25: not shipped

Omar chose recall. Through Judge the authorship question catches 244/260 assistant answers vs D's 83/260, at
the cost of 58 human fires vs 20, of the same 787 human texts (90 real chat, 697 careful humans; recounted from
`results/raw_judge_*.jsonl`). `ai_generated` keeps the authorship question, off by default, experimental and
flag-only. Because it stays aggressive, the flag-only limit is now enforced on every path, not only in
`set_category`: `Policy.from_dict`, `decide()`, the JS `fromJSON` and `decide`, the hosted `PUT /internal/policy`
and the dashboard menu.
