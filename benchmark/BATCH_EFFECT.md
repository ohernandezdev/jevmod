# Does a message's score depend on its batch? Spam and harassment, measured.

Run on 2026-09-22 and 2026-09-23 against the live TypeSafe API from `benchmark/batch_effect.py`. 600
messages, ten conditions, 4,800 judgements, 822 requests, 6.0M input tokens, **$0.25**. Raw results are committed
at `benchmark/results/batch_effect.jsonl`, so every table below recomputes for free.

JEV-56. `ai_detect/REPORT2.md` measured this for `ai_generated` and then asserted, without measuring
it, that the effect belongs to the batching contract rather than to that category, so spam and
harassment would have it too. Those are the two categories whose action can be `delete`.

## The short answer

**Yes, and it is batch identity rather than batch composition.** Regrouping 150 spam messages into
different batches of 25, with every batch still entirely spam, moves 18 of them across the shipping
threshold of 0.85 against a floor of 4 when the request is repeated unchanged. Both arms that
randomise batch membership do this, by the same amount, and they are the only two results in this
experiment that survive correction for multiple comparisons.

What does **not** hold, and was the hypothesis this issue was written on: **what the neighbours are
does not matter.** With batch membership randomised equally in both arms, an all-spam batch and a
half-clean batch flip the same messages, 18 against 18, p = 1.000. Composition adds nothing to
membership.

Aggregate numbers survive and per-message verdicts do not. Recall at the shipped threshold moves 2.7
to 6.7 points across all six conditions, so the benchmark figures the site publishes are
reproducible. The verdict on one message is not: the same spam message gets a different answer about
one time in eight, decided by nothing but which other messages happened to be in flight with it.

That is the finding. jevmod's published averages are honest. What is not reproducible is the only
thing a server owner ever asks, which is why *this* message was acted on.

## 1. The conditions

Six, each holding everything constant but one thing. Neighbour overlap is measured, not assumed: the
mean number of the original 24 batch-mates a message keeps.

| condition | what changes | neighbours kept, of 24 |
|---|---|---|
| `pure` | the baseline, every batch all one side | 24.0 |
| `pure2` | nothing; the same request sent again | 24.0 |
| `reordered` | position only; the same 25 messages, shuffled inside the batch | 24.0 |
| `reshuffled` | membership; same side, regrouped | 3.79 (chance: 3.87) |
| `mixed` | composition, interleaved in pool order | 11.52 |
| `mixed_shuffled` | composition, membership randomised | 1.90 (chance: 1.93) |

**`mixed` is a trap and is reported only so nobody rebuilds it.** Interleaving two pools in pool
order leaves every message holding about half its original batch-mates. On the axis that matters it
is a *weaker* perturbation than `reshuffled`, not a stronger one, so "mixed is no larger than
reshuffled" proves nothing about composition. The first version of this report drew exactly that
conclusion and a red team refuted it. `mixed_shuffled` is the arm that isolates composition, because
it differs from `reshuffled` in composition alone.

One residual difference cannot be designed away: a 50/50 batch is drawn from a pool twice the size,
so its chance-level overlap is lower. Both arms sit on their own chance level, so both have
neighbours fully randomised, and the gap is a consequence of changing the composition rather than a
confound.

## 2. Messages crossing their shipping threshold

Counted against `pure`, n = 150 per cell. Thresholds from `DEFAULT_THRESHOLDS`: spam 0.85,
harassment 0.75.

| category | side | `pure2` | `reordered` | `reshuffled` | `mixed` | `mixed_shuffled` |
|---|---|---|---|---|---|---|
| spam | positive | 4 | 10 | **18** | 15 | **18** |
| spam | clean | 1 | 0 | 1 | 2 | 2 |
| harassment | positive | 2 | 4 | 6 | 8 | 7 |
| harassment | clean | 1 | 3 | 4 | 5 | 5 |

Significance matters more than the counts here, and most of the counts do not survive it. McNemar,
exact binomial on discordant pairs, each manipulation against the `pure2` floor, Holm-corrected over
the sixteen primary tests:

| comparison | flips vs floor | raw p | Holm p | |
|---|---|---|---|---|
| spam positive, `reshuffled` | 18 vs 4 | 0.0026 | **0.041** | survives |
| spam positive, `mixed_shuffled` | 18 vs 4 | 0.0026 | **0.041** | survives |
| spam positive, `mixed` | 15 vs 4 | 0.0074 | 0.103 | does not |
| harassment positive, `mixed` | 8 vs 2 | 0.031 | 0.406 | does not |
| spam positive, `reordered` | 10 vs 4 | 0.146 | 1.000 | does not |
| harassment positive, `reshuffled` | 6 vs 2 | 0.219 | 1.000 | does not |
| the other ten cells | — | ≥ 0.125 | 1.000 | do not |

The two survivors are exactly the two arms that randomise batch membership, and section 2.1 shows
they cannot be told apart from each other. Whatever this is, it is membership.

Two things follow, and the second one corrects the first draft of this report.

- **Regrouping beats reordering**, directly tested: `reshuffled` against `reordered` on spam
  positives is b/c = 10/2, p = 0.039.
- **Position alone is not distinguishable from noise.** 10 flips against 4 is a coin flip at n = 150.
  The first draft said reordering produced "nearly the whole effect". It does not: on excess over the
  noise floor it is 6 of 14, and it is not significant at all.

### 2.1 The composition test

Both arms with neighbours randomised to chance, so composition is the only difference:

| category | side | `reshuffled` | `mixed_shuffled` | b/c | p |
|---|---|---|---|---|---|
| spam | positive | 18 | 18 | 10/10 | 1.000 |
| spam | clean | 1 | 2 | 1/0 | 1.000 |
| harassment | positive | 6 | 7 | 6/5 | 1.000 |
| harassment | clean | 4 | 5 | 3/2 | 1.000 |

Four cells, p = 1.000 in each. Whatever moves these scores, it is not what the neighbours contain.

## 3. How far the scores move

Absolute movement against `pure`. The last column is the share of messages moving more than the
±0.03 that `AGENTS.md` tells every test author to allow for.

| category | side | condition | mean | p95 | max | share > 0.03 |
|---|---|---|---|---|---|---|
| spam | positive | `pure2` | 0.011 | 0.040 | 0.060 | 9.3% |
| spam | positive | `reshuffled` | 0.073 | 0.200 | 0.420 | 65.3% |
| spam | positive | `mixed_shuffled` | 0.086 | 0.300 | 0.550 | 63.3% |
| spam | clean | `pure2` | 0.004 | 0.020 | 0.060 | 2.7% |
| spam | clean | `reshuffled` | 0.018 | 0.090 | 0.360 | 15.3% |
| harassment | positive | `pure2` | 0.011 | 0.050 | **0.110** | 12.0% |
| harassment | positive | `reshuffled` | 0.044 | 0.160 | 0.400 | 45.3% |
| harassment | clean | `pure2` | 0.006 | 0.020 | 0.040 | 1.3% |
| harassment | clean | `reshuffled` | 0.030 | 0.150 | 0.260 | 26.0% |

A repeated identical request is nearly deterministic **in the mean** and not in the tail: one
harassment message, `oai170`, moved 0.71 to 0.60 with the request unchanged, and 12% of harassment
positives moved further than 0.03.

Changing the request multiplies that by **3.3 to 6.7 times**, per side, not by an order of magnitude.
The band quoted for the positive strata does not apply to the clean ones, which move about half as
much because their scores sit pinned near zero.

**This is not threshold jitter.** Restricting to messages far from their line, |score − threshold| >
0.10 in `pure`, makes the movement larger rather than smaller: spam positives move 0.085 far from the
line against 0.055 near it. Items near the line are 61 of 150 and account for under a third of the
total movement.

## 4. What this contradicts

### `AGENTS.md`

> Assert against thresholds with margin, never exact values: Jev's probabilities move about plus or
> minus 0.03 between runs.

True for a byte-identical repeated request, and only there. A rerun in a different batch, which is
what "between runs" means to anyone reading that sentence, moves the p95 to 0.15 to 0.20 and the max
to 0.55. The rule needs the distinction written into it.

### The precision the site implies

Every score observed in this run, all 28,800 of them, sits exactly on a 0.01 grid: 101 distinct
values, none off-grid. The number has **two-decimal resolution and one-decimal reproducibility**. A
threshold set to 0.85 rather than 0.84 is a distinction the score cannot carry for an individual
message, although it carries fine in aggregate.

### `ai_detect/REPORT2.md`, section 1

REPORT2 reports 17 of 156 items, 11%, flipping across the threshold "with nothing changed at all",
against the 2.7% measured here. Both are right, and REPORT2's sentence is wrong.
`cmd_ask_retest` in `deterministic.py:618` calls `rng.shuffle(sample)` before it chunks: its rerun
held the *composition* constant and regrouped the *membership*. Its 11% is this report's
`reshuffled` (12%), not its `pure2`. Nobody had measured a truly repeated request until now.

That correction strengthens REPORT2's conclusion rather than weakening it, and it removes the only
number in it that suggested the model was unstable for no reason at all.

**Its composition finding also stands, and the contrast with this report is the interesting part.**
REPORT2 had a membership-only control all along, once its middle column is read correctly: regrouping
moved the wiki-style false-positive rate from 0.300 to 0.250, nothing, while the 50/50 arm moved it
to 0.667. Composition carries the whole effect there and none of it here.

The likely reason is the one REPORT3 section 3 noticed without following up: `ai_generated` asks *who
wrote this*, and a batch full of machine text is real evidence about the world for that question, so
a model that uses it is behaving sensibly rather than erratically. Spam and harassment ask *what is
this text*, where a neighbour is evidence for nothing, and the movement that remains is not about the
neighbours at all. **That predicts which future questions will be composition-sensitive: the ones
that ask about the author or the world rather than about the message.** `doxxing` and `minors` are
the two shipped categories closest to that shape, and both are unmeasured.

## 5. What this does not measure, and one claim withdrawn

**The direction of failure is not established.** The first draft of this report said the dominant
production failure is missed action rather than wrongful action, on 9 spam messages falling under the
line against 6 rising over it. That is p = 0.607, and no lost-against-gained asymmetry in any of the
sixteen cells reaches significance. It is also mechanically explained without any directional effect:
in `pure`, 22 spam positives sit in [0.85, 0.90) against 12 in [0.80, 0.85), so symmetric jitter on
that distribution produces more falls than rises. **The claim is withdrawn.**

**This design cannot compare the two failure directions at all.** The positive pools have 61 and 32
messages within 0.10 of their threshold; the clean pools have 6 and 7. The clean side has almost no
mass where a flip is possible, so "false positives barely move" is a floor effect and not evidence.

**The pools are not chat.** The spam pool is 150 of 150 YouTube comments, median 65 characters. The
clean pools are about three quarters OpenAI-moderation prose, median 330 and 358 characters. A
false-positive rate measured on long prose does not transfer to a Discord channel, and `mixed`
confounds composition with a genre and length contrast on top of it.

**Harassment is unresolved and cannot be resolved with the data that exists.** At the observed effect
size, 5 discordant pairs against 1 in 150, the power to reach p < 0.05 is:

| n | power |
|---|---|
| 150 (this run) | 19% |
| 319 (every harassment message in `items.jsonl`) | 60% |
| 500 | 84% |

The labelled set holds 319 harassment messages. Even using all of them, this experiment would fail to
reach significance four times in ten. **JEV-56 cannot be closed for harassment until the labelled set
grows**, which reverses the ordering the issue itself argues for: it says measuring the batch effect
must come before labelling 5,000 messages, and for harassment the dependency runs the other way. For
spam it does not, and spam is answered.

**One rival explanation survives.** Absolute movement correlates with each score's own uncertainty,
`p(1−p)` in `pure`, at r = +0.60 to +0.68 — including within `pure2`, where nothing changed.
Mid-range scores move and confident ones do not. Whether batching destabilises mid-range scores or
mid-range scores are simply unstable cannot be separated here: it needs an arm that asks one message
per request, which this design never had. That arm is the obvious next experiment and it is cheap.

## 6. The Discord reaction loop, which this data also condemns

`discord_bot.py:358` lets a moderator react to a log entry: ❌ raises that category's threshold by
0.03, ✅ lowers it by 0.02. Nothing had measured what that does over time. It can be answered from
the results above without spending anything, because they hold 150 real spam messages and 150 real
clean ones scored five times each. `benchmark/nudge_loop.py` runs it; the moderator in it is
idealised, reacting to every flag and never wrong about whether it was spam, which is the most
favourable case the mechanism can be given.

**The loop aims at a number nobody chose.** At equilibrium the ups and downs cancel, 0.03 × false
positives = 0.02 × true positives, so it settles where precision is 0.60. That is the ratio of two
constants, identical for a support forum and a meme channel.

**And at a realistic spam rate that equilibrium does not exist.** Precision has to pass through 0.60
somewhere between the clamps for the loop to have anything to settle on:

| spam rate | P@0.50 | P@0.70 | P@0.85 | P@0.95 | P@0.99 | crosses 0.60? |
|---|---|---|---|---|---|---|
| 2% | 0.20 | 0.20 | 0.19 | 0.21 | none | **no** |
| 5% | 0.39 | 0.39 | 0.37 | 0.41 | none | **no** |
| 10% | 0.58 | 0.58 | 0.56 | 0.60 | none | **no** |
| 25% | 0.80 | 0.80 | 0.79 | 0.82 | none | yes |
| 50% | 0.92 | 0.92 | 0.92 | 0.93 | none | yes |

Simulated over 4,000 messages, drawing each score from the five real observations of that message:

| spam rate | where the line ends up | precision | recall |
|---|---|---|---|
| 2% | **0.99** | — | **0.00** |
| 5% | **0.99** | — | **0.00** |
| 10% | 0.99 | 0.58 | 0.16 |
| 25% | 0.50 | 0.83 | 0.81 |
| 50% | 0.50 | 0.94 | 0.83 |

**0.99 is an absorbing state.** No message in 1,800 observations scored 0.99 or higher, the highest
being 0.98. Once the line reaches the ceiling the category stops flagging, so it stops receiving
reactions, so it never comes back down. **The feature turns the category off, silently, in exactly
the channels it was built for**, and the owner is never told. At a high spam rate it runs to the
other clamp instead and flags everything. There is no setting where it does the thing it promises.

Two more things fall out of the same table:

- **One reaction is smaller than the message that provoked it.** The same spam message spans 0.16
  across its five batches, median, and 87% of them span more than the 0.03 a reaction applies. A
  moderator is correcting a number that would have been different had the message arrived a second
  earlier, by less than the difference.
- **The threshold knob buys almost nothing here.** Precision moves 0.02 between 0.50 and 0.95 at
  every base rate in the table. Reading that with section 5's caveat about the pools: the spam pool
  is YouTube comments and the clean pool is long prose, so the absolute precisions do not transfer
  to a Discord channel, but the flatness is a property of the two score distributions rather than of
  the base rate, and it deserves its own measurement on chat-shaped text.

This does not belong in this branch to fix. JEV-12 already exists to capture human decisions
properly and already names `policy.nudge()` as the thing that moves a threshold without storing
anything. This is the measurement that says how urgent that is.

## 7. The rival explanation is dead, and what killed it is worse

Section 5 left one explanation standing: absolute movement correlates with each score's own `p(1−p)`
at r = +0.60 to +0.68, *including inside `pure2` where nothing changed*, so "mid-range scores are
intrinsically unstable" was not separated from "batching destabilises them". Two more arms settle it.
Each of the 150 spam and 150 clean messages was asked **entirely alone**, one message per request,
and then asked alone again.

| comparison | mean abs. movement | p95 | flips across 0.85 |
|---|---|---|---|
| the same batch of 25, asked twice | 0.011 | 0.040 | 4/150 |
| **the same message alone, asked twice** | **0.011** | **0.030** | **2/150** |
| regrouped into different batches of 25 | 0.073 | 0.200 | 18/150 |

A message asked alone, twice, is exactly as steady as a batch repeated. **Mid-range scores are not
intrinsically unstable**, and regrouping against a single-message repeat is b/c = 18/2, p = 0.000.
Batching is the cause, and the question is closed.

### What the single-message arm found instead

The same 300 messages, in batches of four different sizes. Nothing else changed.

| messages per request | mean score, spam | mean, clean | **recall@0.85** | FPR@0.85 |
|---|---|---|---|---|
| **1** | 0.499 | 0.075 | **17.3%** | 2.7% |
| 5 | 0.654 | 0.114 | 32.0% | 4.0% |
| 10 | 0.703 | 0.109 | 38.7% | 4.0% |
| **25** | 0.719 | 0.106 | **37.3%** | 3.3% |

**Spam recall more than doubles between a message judged alone and the same message judged with
twenty-four others.** 139 of the 150 spam messages score higher in a batch than alone, 6 lower, 5
tied; sign test p = 2.35e-12, median shift +0.19, quartiles +0.04 to +0.37. It is not a handful of
outliers and it is not drift: it is a systematic shift of the whole distribution, and it saturates
around ten messages.

The false-positive rate barely moves, 2.7% to 4.0%, so this is not inflation. It is **real
discrimination the model only has when it can see a stream.** That is not misbehaviour; spam is a
judgement about what is normal here, and one message is not a here.

**`harassment` does not move at all**, 0.037 against 0.036 on the same messages. Which fits: an
insult is an insult on its own, and the spam question is the one that needs neighbours.

### Why this matters in production, today

`Batcher` collects messages per tenant for a fixed time window and sends whatever arrived
(`core/service.py`; the Discord bot uses 2 seconds). **Batch size is not a setting. It is the
server's traffic.**

So a quiet Discord server, where messages arrive one at a time, runs its spam category at 17%
recall. A busy one runs the same category, on the same threshold, at 37%. Neither owner is told, and
the quiet server is the one least able to notice. Every published spam figure in this repository was
measured at batch 25 (`run_jevmod.py`, `BATCH = 25`), which is the favourable end.

This is a bigger defect than the one this report set out to measure, and it has a direction: the fix
is to stop the batch size varying with traffic, not to tell owners to have busier servers. Tracked
as **JEV-57**, which carries the three candidate fixes and the reason none of them is obvious.

### One thing the controls turned up against themselves

The repeated-identical-batch control is not perfectly unbiased: `pure2` scores higher than `pure` on
70 messages and lower on 20, p = 0.000, a systematic +0.007. That is a twenty-seventh of the batch
effect and does not change any conclusion here, but "a repeated request is deterministic" is not
quite true and should not be written as though it were.

## What to do

1. **Fix the `AGENTS.md` rule** so it distinguishes a repeated request from a rerun.
2. **Do not tune a threshold on a single message's score**, in the dashboard or in a bot command.
   The reaction nudge moves a threshold by 0.03 on one reaction, inside the noise for 65% of spam
   messages. Section 6 is that look, and it found worse than imprecision.
3. ~~Run the one-message-per-request arm before believing this is about batching at all.~~ Done,
   section 7. It is about batching, and the arm found something larger on the way.
4. **Do not close JEV-56 for harassment.** Re-run it when the labelled set passes 500 harassment
   messages, and link it to JEV-11 and JEV-7 as a dependency in that direction.
5. **Treat the reaction loop as a live defect**, not a rough edge: section 6. It drives the category
   to a state it cannot leave, at the spam rates real servers have. It belongs to JEV-12.
6. **Stop batch size varying with traffic**, section 7, JEV-57. The largest effect in this report and
   the only one already costing real servers something.

## Reproduce

```
python -m benchmark.nudge_loop                           # free, section 6, no API calls
python -m benchmark.batch_effect ask single              # paid; likewise single2, batch5, batch10
python -m benchmark.batch_effect pools                   # free: pools, drops, batch counts
python -m benchmark.batch_effect pilot                   # paid, one batch per condition
python -m benchmark.batch_effect ask pure                # paid, and likewise pure2, reordered,
python -m benchmark.batch_effect ask mixed_shuffled      # reshuffled, mixed, mixed_shuffled
python -m benchmark.batch_effect analyse                 # free, from the committed jsonl
```

`cache_ttl_s=0` is load-bearing. With the default 24 hours every condition after the first comes back
`reason="cache"` with a drift of exactly zero, which looks like a clean result and is not one. The
harness refuses a cached or unjudged verdict rather than recording it.
