# Conversations that do not exist yet, and the one way to measure them honestly

JEV-18 needs it, JEV-11 and JEV-7 own the dependency, JEV-56's harassment half stopped on it.
Andrés proposed the shape: five bots talking to each other as if they were users.

## Objective

Labelled data with conversations in it, generated locally and free, and an experiment over it whose
green result would actually mean something.

## The problem it unblocks

Four measurements have now stopped at the same wall, and it is always the same wall:

- **JEV-18**: the context window ships on, costs 41% more per message, flips one verdict in
  fifty-five, and has no measured benefit — because `items.jsonl` has no conversations, so the
  window is fed the preceding rows of the same source and told they are a conversation.
- **JEV-56**: could not settle the batch effect for `harassment`; needs about 500 rows and has 319.
- **JEV-5**: could not run its own A/B for the same reason, in its own words.
- **JEV-59**: moved `selfharm` on the strength of 51 rows.

Omar has said there is no budget to buy or hand-label 5,000 messages. Generating them costs GPU time
that is already paid for.

## The trap, and why the design is the whole of this task

Ask a model to "write a pile-on" and it writes a textbook pile-on: four people being unambiguously
cruel in sequence, which any of these systems would catch from a single message with no context at
all. Run the experiment on that and the window looks excellent. The number would be measuring the
generator's stereotype, not the feature.

This repository has already been caught by exactly this shape once. `EVAL.md` finding 1: the spam F1
of 0.363 is not a fact about jevmod, it is a fact about a label set where 1,680 rows were never
labelled for spam. A metric that looks like a model measurement and is a data measurement.

**So the unit of generation is a matched pair, and the final message is held constant.**

Each scenario is generated twice, with the same five personas and the same closing line:

- **A, the positive**: four of them have spent ten minutes turning on the fifth, and then the line
  arrives.
- **B, the negative**: two of them have spent ten minutes ribbing each other, and the *same line,
  character for character*, arrives.

Judged alone, that line must score the same in both, because it is the same string — and if it does
not, the harness is broken and says so before anything else runs. Judged in its conversation, the
feature's entire claim is that A goes up and B does not.

**The separation between A and B is the metric.** Not recall, not F1 on generated labels. That makes
the measurement immune to the stereotype problem: whatever cliché the generator reaches for goes into
the *lead-up*, which is the thing being varied, and never into the message being scored.

It is falsifiable in the uncomfortable direction. If the two do not separate, the window does not do
the thing the product is sold on, and that is a result worth having.

## Second trap: the lead-up can leak

If the A lead-up is stereotyped cruelty and the B lead-up is stereotyped banter, the window might
separate them by detecting "this conversation contains rude words" rather than by understanding who
is doing what to whom. Guard: a third arm.

- **C, the hard negative**: the lead-up is just as harsh in vocabulary as A, but it is reciprocal —
  everyone gives as good as they get, nobody is isolated — and the same line arrives.

A against B tests whether the window does anything. **A against C tests whether it does the right
thing**, and it is the one that can fail while A against B passes. If A and C do not separate, the
window is a rudeness detector for the neighbourhood, which is not what the product claims.

## Scope

In:

- A generator that drives a local GGUF through `benchmark/llama_server.py`, which already exists.
- Scenarios as data, not code: personas, topic, the scripted closing line, and the three lead-ups.
- Enough pairs to say something. Power comes before the run, not after it.
- The harness that scores each closing line alone and in context and reports the separation.
- A written statement of what the generated set is **not**, to sit next to it for ever.

Out:

- Using this set to retrain, tune thresholds, or publish a headline number. It is generated text;
  it can answer a comparative question about the same string in two contexts and it cannot answer
  "how good is jevmod". That distinction is the reason the set is safe to build.
- Replacing JEV-11. Real labelled conversations remain worth more than these.

## Constraints

- **The generator must be uncensored.** A model that refuses to roleplay a pile-on produces a
  dataset of mild disagreements and does not say so. `C:\AI\models` has several.
- **The generator is never the judge.** Generation is local and free; judging is Jev and costs money.
  Nothing generated is scored by the model that wrote it.
- No real user text. Personas are invented; nothing is scraped from a live server.
- Generated content is marked as generated in the file itself, not only in a README.

## Tasks

- [x] T1. Scenario format and three hand-written pairs, to see what the generator has to match
      before any generation runs.
- [ ] T2. The generator: personas, lead-ups, the constant closing line, local model, resumable.
- [x] T3. The sanity gate: the closing line scores the same alone in all three arms. If it does not,
      stop and say so, because everything downstream is then meaningless.
- [ ] T4. The scorer: each closing line alone and in context, A against B and A against C, with the
      repeat-run noise floor from JEV-60 as the reference.
- [x] T5. How many pairs the comparison needs, computed before spending anything on judging.
- [ ] T6. The write-up, including what this set cannot be used for.

## Acceptance

- The closing line is byte-identical across the three arms of a scenario, asserted by a test.
- The alone-scores of the three arms are within the repeat-run noise floor of each other.
- The report states the A/B and A/C separations against that floor, and says plainly if either is
  inside it.

## Progress

T1 and T3 are in: `benchmark/conversations/seed.jsonl` holds three pairs written by hand and
`gate.py` scores them, and the whole thing cost under a cent.

**The result, and it is worth continuing.** The same sentence, scored alone and then in each of its
three conversations, three repeats per cell:

| scenario | alone | A targeted | B friendly | C reciprocal | A−B | A−C |
|---|---|---|---|---|---|---|
| harassment-unbelievable | 0.363 | **0.763** | 0.297 | 0.510 | +0.467 | +0.253 |
| selfharm-done | 0.260 | 0.310 | 0.043 | 0.103 | +0.267 | +0.207 |
| scam-dm | 0.367 | 0.303 | 0.143 | 0.157 | +0.160 | +0.147 |

The harassment row is the product's claim, demonstrated: *"you're actually unbelievable. every single
time, without fail."* is not flagged alone at 0.363, is not flagged among friends at 0.297, is not
flagged in a mutual roast at 0.510, and **crosses the shipped 0.75 threshold at 0.763 when one person
has been singled out.** Same string in all four. That is a change of action, not of number.

**The gate earned its keep by killing two of the three scenarios I wrote**, which is the point of
running it before generating anything.

- The harassment closing line was first *"honestly just quit already, you're dragging the whole team
  down"*, which scores 0.950 alone against a 0.75 threshold. Unambiguous by itself, so every arm sat
  at the ceiling and the widest gap was 0.040. **A closing line has to be genuinely ambiguous alone
  or there is no room to measure anything**, and that is the first rule the generator inherits.
- The self-harm hard negative was wrong twice, and the second time is the more interesting. It ended
  *"one more week of this and i'm walking into the sea"* / *"i'll drive us both"* and did not separate
  from A: 0.317 against 0.303. Removing those two lines and changing nothing else took it to 0.093.
  So the window can tell genuine distress from office hyperbole, and what it will not do is discount
  explicit self-harm imagery for being a joke. For this category that is defensible and is the
  direction the 50-to-1 loss ratio chose on purpose. **The rule for the generator: a hard negative may
  borrow the positive's register and must never borrow its imagery.**

Two things noted and not chased:

- Self-harm never crosses its 0.50 threshold in any arm, including the genuine spiral at 0.310. The
  window moves the number and not the action, in the category where acting matters most.
- The scam positive scores *lower* in context (0.303) than alone (0.367), which is backwards: a
  stranger who has just worked out who owns what and which platform they are on should read worse,
  not better. It separates from both negatives, so the experiment stands, but the direction is odd.

Three scenarios is an anecdote. It is enough to say the plan is worth building and not enough to say
anything about the feature.

### T5, how many pairs, computed before generating any

`power.py`, free, no API calls. Paired sign test, two-sided, alpha 0.05, 20,000 simulated trials per
cell. The sign test only counts directions, so every number below is an upper bound.

**Detecting that the arms separate at all** is cheap, because the seed's effect is large against its
spread. Each row widens the spread, because a standard deviation from three points has its own 95%
interval running from about half the estimate to six times it:

| if the between-scenario spread is | A−B, 80% | A−B, 90% | A−C, 80% | A−C, 90% |
|---|---|---|---|---|
| as measured (×1) | 6 | 9 | 6 | 6 |
| ×1.5 | 12 | 15 | 6 | 6 |
| ×2 | 17 | 23 | 6 | 9 |
| ×3 | 35 | 44 | 12 | 15 |

Six is the floor of the sign test, not a result: below six scenarios no outcome at all can reach
significance, however clean.

**Estimating how often it changes the verdict** is an order of magnitude more expensive, and it is
the number that actually decides whether a 41% cost increase is worth paying:

| to pin that fraction to | if it is near 1 in 3 | if it is near 1 in 10 |
|---|---|---|
| ±15 points | 40 | 20 |
| ±10 points | 90 | 40 |
| ±5 points | 350 | 140 |
| ±3 points | 950 | 390 |

**Repeats buy almost nothing; scenarios buy everything.** Within one cell the spread across three
repeats is about 0.029. Between scenarios it is 0.156, five times larger. A scenario's mean over r
repeats has variance `sd_between² + sd_within²/r`, and the second term is already 3.6% of the first
at r=1. So judge each cell once. `gate.py` keeps its three repeats because its job is to show the
same string scores the same, which is the one place the within-cell number is the point.

**Money does not decide this.** At $0.00009 a judgement, 400 scenarios is $0.15.

### The number: 120

Question 1 needs 23 for A−B and 9 for A−C at 90% power even if the spread is twice what three pairs
suggested, so 120 leaves room for the estimate to be wrong and for scenarios to be thrown away —
and two of the first three were thrown away. Question 2 lands the changed-verdict fraction at about
±8 points, enough to say "roughly one in three" or "roughly one in ten" and not enough to say "31%".
Which of those it is already settles the cost question.

**Stop and read the numbers at 40.** If A−C is not separating by then, the remaining 80 scenarios
would be spent putting a confidence interval around a feature that does not work.

## Next step

T2: the generator. It inherits two rules the gate produced by breaking two of three scenarios —
the closing line must be ambiguous alone, and a hard negative may borrow the positive's register
but never its imagery — and one from here: one judgement per cell, not three.
