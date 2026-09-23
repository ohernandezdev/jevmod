# Does a message's score depend on its batch-mates? Spam and harassment.

JEV-56, Urgent, Bug. <https://linear.app/jevmod/issue/JEV-56>

## Objective

Measure, for `spam` and `harassment`, the thing `benchmark/ai_detect/REPORT2.md` measured for
`ai_generated`: whether the same message scores differently depending on which other 24 messages
share its request.

## Why this one first

`ai_generated` was never shipped, so its instability cost nothing. `spam` and `harassment` are the
two categories whose default action can be `delete`. If a score moves with the company a message
keeps, then:

- the site publishes thresholds to two decimal places that do not mean what they appear to mean;
- the dashboard lets an owner tune them at that precision;
- labelling five thousand messages (JEV-11) against a moving score is labelling noise, which is why
  JEV-56 is ranked ahead of it.

REPORT2 already stated the expectation, without measuring it: *"It is a property of the batching
contract, not of the category, so it applies to spam, scam and harassment too, where it is
unmeasured and where the action is delete rather than flag."* An expectation written down is not a
measurement. This task turns it into one, in whichever direction it goes.

There is weak evidence on the other side, gathered on 2026-09-22 while building `judge_copy.py` and
again while dogfooding it: one question asked alone scored 0.80, and in company with four others
0.81 / 0.81 / 0.76; and changing one sentence of a text moved one question from 0.51 to 0.88 while
the other four stayed flat within 0.03. Both are single texts with a handful of questions, not the
25-message production batch. They are a reason to measure rather than assume, not a result.

## What counts as an answer

Not a mean. `REPORT2.md`'s `ALL` row drifted +0.001 because per-stratum drifts had opposite signs
and cancelled, while the false-positive rate for wiki-style text went 0.300 to 0.667 underneath it.
The number that decides this issue is **how many messages cross their shipping threshold between
two conditions**, reported per side, never pooled.

Shipping thresholds, from `DEFAULT_THRESHOLDS` in `jevmod/core/policy.py`: spam 0.85, harassment
0.75.

## Design

**As designed, three conditions.** It shipped with six. This table is left as written so the two
rounds of correction below have something to point at.

| condition | what it is | what it controls for |
|---|---|---|
| `pure` | every message in the batch is from the same side | the baseline |
| `pure2` | identical composition, asked a second time | run-to-run noise, the control's control |
| `mixed` | the batch is about half the other side | the effect under test |

Without `pure2` no drift is attributable: REPORT2 found 17 of 156 items flipping across the
threshold with nothing changed at all, so an effect only exists if it is bigger than that. That
sentence turned out to misread REPORT2, which is T6c.

The three conditions above cannot separate composition from membership, and `mixed` as specified
here is not even a membership perturbation of the right size. T3a and T5a are what fixed that.

Both sides are measured. A clean message drifting up into `delete` and a spam message drifting down
out of it are different failures and neither is visible in the other's number.

Pools, from `benchmark/data/items.jsonl` (2,531 labelled: 1,658 clean, 319 harassment, 250 spam):

- 150 `spam`, 150 `harassment`
- 150 clean paired with spam, a separate 150 clean paired with harassment, so no clean message is
  reused across the two arms

1,800 judgements, 72 requests. Cost is measured by a pilot before the full run, not estimated.

## Constraints that the harness has to respect

1. **The 24-hour cache would silently answer the whole experiment.** `Judge.cache` is keyed by
   normalised text plus topic plus category set (`judge.py:_key`). Asking the same text twice would
   return `reason="cache"` and a drift of exactly zero, which looks like a clean result and is not
   one. Every run constructs its `Judge` with `cache_ttl_s=0`, and the harness asserts that no
   verdict comes back with `reason="cache"`.
2. **The pre-filter drops messages before Jev sees them.** Under eight alphanumerics without a link
   returns `judged=False` and no scores. Those items are excluded from the pools up front and the
   count is reported, rather than being discovered as holes in the results.
3. **The question set must be production's.** Asking only `spam` and `harassment` would be a
   different prompt from the one the product sends. The runs ask the same categories
   `benchmark/run_jevmod.py` asks, so the number transfers.
4. **Ordering must be deterministic.** Seeded shuffles, written down, so the run can be repeated.

## Tasks

- [x] T1. `benchmark/batch_effect.py`: pools, seeded batch construction, resumable JSONL output with
      the id, condition, scores and per-batch token count. Commit `0a8682f`.
- [x] T2. Pilot: one batch per condition. No cache hits, no pre-filtered items, $0.0013 per batch of
      25, so the full design costs about $0.16 rather than the estimate.
- [x] T3. The full run. Grew from three conditions to five, for the reason in T3a.
- [x] T3a. **Two controls the original design did not have, added after the first result.** Between
      `pure` and `mixed` two things change at once: what the batch is made of, and which messages are
      in it. `reshuffled` (same side, regrouped) separates those. `reordered` (the same 25 messages,
      different positions) separates a third: `Judge` keys its state by position, and the byte-
      identical `pure2` request turned out to be nearly deterministic, so `pure2` alone would have
      flattered the noise floor. Without these two the report would have credited composition with an
      effect that is not composition's.
- [x] T4. The analysis, including which way the flips go and how far from the threshold they start.
- [x] T5. `benchmark/BATCH_EFFECT.md`: the report.
- [x] T5a. **Red team, and what it cost me.** A separate agent attacked the measurement against HEAD.
      It refuted two of the five claims I was about to publish and weakened two more. Every number it
      returned was recomputed here before being accepted. What it found:
      - `mixed` was not a composition test at all. Interleaving the pools in pool order leaves every
        message holding 11.5 of its 24 original batch-mates against 3.8 for `reshuffled`, so it is a
        *weaker* neighbour perturbation, and "mixed is no larger than reshuffled, therefore
        composition does not matter" was a non sequitur. Fixed by adding `mixed_shuffled`, which is
        the arm that actually isolates composition. The conclusion happens to survive, for a reason
        the original design could not have shown.
      - Nothing except spam/positive membership survives Holm correction. `reordered` at 10 flips
        against 4 is p = 0.146, so "reordering produces nearly the whole effect" was a coin flip
        published as a finding.
      - The lost-against-gained direction is p = 0.607 and follows mechanically from 22 items sitting
        in [0.85, 0.90) against 12 in [0.80, 0.85). Withdrawn.
      - The `pure2` maximum is 0.110, not 0.060; the 0.060 held only by dropping harassment.
      - A latent resume defect: re-asking a batch because one id was missing would have written a
        second row for ids already scored, and `_rows()` keeps the last silently. It never fired
        (3,600 rows, 3,600 unique pairs) and is fixed.
- [x] T5b. `analyse()` rewritten so the tool cannot reprint the refuted framing: it computes the
      McNemar tests and the Holm correction itself, labels only `reshuffled` against `mixed_shuffled`
      as the composition test, and prints no lost-against-gained table.
- [ ] T6. Carry it back: JEV-56, and the two documents this contradicts, in T6a and T6b.
- [x] T6a. `AGENTS.md` says "Jev's probabilities move about plus or minus 0.03 between runs". That
      holds in the mean for a repeated identical request and not in the tail (max 0.110, and 12% of
      harassment positives move further than 0.03), and not at all for anything else (p95 0.15 to
      0.20, max 0.55). The rule that depends on it, how tests assert with margin, needs the
      distinction written into it.
- [ ] T6b. The site publishes thresholds to two decimals and a harassment recall figure. The recall
      figure survives: aggregate recall moves 2.7 to 6.7 points across conditions. The per-message
      verdict does not. Decide what, if anything, the site owes a reader about that.
- [x] T6c. `REPORT2.md` section 1 says 17 of 156 items flip "with nothing changed at all". Its rerun
      control calls `rng.shuffle(sample)` before chunking (`deterministic.py:618`), so it regrouped
      the membership: its 11% is this report's `reshuffled`, not its `pure2`. Corrected in the report,
      in `deterministic.py`'s docstring and in the column headings its `composition` command prints,
      so the table and the prose cannot disagree again.
- [x] T6f. **REPORT2's composition finding stands, and that is the more interesting half.** Read
      correctly, its middle column is a membership-only control: regrouping moved the wiki-style
      false-positive rate 0.300 to 0.250, nothing, while the 50/50 arm moved it to 0.667. Composition
      carries the whole effect for `ai_generated` and none of it for spam and harassment. The reason
      is probably the one REPORT3 noticed and did not follow: `ai_generated` asks who wrote this, and
      a batch full of machine text is real evidence for that question. Spam asks what this text is.
      **That predicts which categories will be composition-sensitive**: the ones asking about the
      author or the world. `doxxing` and `minors` are the two shipped categories closest to that
      shape and both are unmeasured. Written into both reports.
- [x] T6d. The reaction nudge. It needed its own look and the look found worse than imprecision.
      `benchmark/nudge_loop.py`, free, no API calls, section 6 of the report. The loop's equilibrium
      is where precision is 0.60, set by the ratio 0.03 to 0.02 and nothing else. At a 2% or 5% spam
      rate precision never reaches 0.60 anywhere between the clamps, so there is no equilibrium: the
      line ratchets to 0.99, nothing in 1,800 observations scores that high, the category stops
      flagging, so it stops being corrected, and it stays off. **The feature silently disables the
      category in exactly the channels it was built for.** Belongs to JEV-12, which already names
      `policy.nudge()` as the thing that moves a threshold without storing anything.
- [ ] T6e. The one-message-per-request arm. Absolute movement correlates with each score's own
      `p(1-p)` at r = +0.60 to +0.68, including inside `pure2` where nothing changed, so "mid-range
      scores are intrinsically unstable" is not yet separated from "batching destabilises them".

## Acceptance

- Every table reports `pure` against `pure2` against `mixed`, never a composition drift without its
  noise floor beside it.
- Threshold-crossing counts are per side. No pooled row stands alone.
- The report states what it did not measure, in the shape REPORT2 and REPORT3 do.
- The run reproduces from the documented commands.

## Progress

3,000 judgements, 120 requests, $0.16, measured not estimated. Raw results in
`benchmark/results/batch_effect.jsonl`, committed, so the tables can be recomputed for free.

**The effect is real, it is batch membership, and it is not composition.** After the red team, the
claims that stand:

- Regrouping 150 all-spam messages into different all-spam batches moves 18 of them across 0.85,
  against a floor of 4 for a repeated identical request. Holm p = 0.041 over sixteen tests. The two
  arms that randomise membership are the only two cells that survive correction, and they are
  indistinguishable from each other.
- **Composition does nothing.** `reshuffled` against `mixed_shuffled`, both with neighbours at
  chance: 18 against 18, p = 1.000, and p = 1.000 in all four cells. The hypothesis this issue was
  written on does not hold.
- Position alone is not distinguishable from noise, p = 0.146.
- Aggregate metrics survive, per-message verdicts do not. Recall moves 2.7 to 6.7 points across all
  six conditions; the same spam message gets a different verdict about one time in eight.
- **Harassment cannot be settled with the data that exists.** 60% power using every one of the 319
  harassment messages in the labelled set. It needs about 500, which reverses this issue's own
  argument that it must come before JEV-11.

T6a and T6c done: `AGENTS.md` now says how much margin a test may assume and why it depends on
whether the request repeats, and `REPORT2.md`, `REPORT3.md` and `deterministic.py` no longer describe
a regrouping as "nothing changed at all".

T6d done, and it was the biggest thing this measurement turned up that is not about batching.

Next: T6b (what the site owes a reader) and T6e (the one-message-per-request arm).
