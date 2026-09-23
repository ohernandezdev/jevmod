# Round two: the score is not a property of the message

> **Superseded by [REPORT3.md](REPORT3.md).** This report answered "was this written by a
> model". That is the wrong question: the product wants slop gone, whoever typed it. Asked the right
> way, the encyclopedic-human failure mode below stops being a failure. Read REPORT3.md first.

Run on 2026-09-19, after [REPORT.md](REPORT.md). Three questions it left open, answered with new data:

1. Does a deterministic, model-free signal rescue the precision?
2. Does the false-positive rate hold up on a corpus built to trigger it, rather than on 260 incidental humans?
3. The first report measured everything in 50/50 batches. **A real channel is not 50% AI.** Does that matter?

The answer to the third question invalidates part of the first report, so it comes first.

## The short answer

**Do not ship `ai_generated`, not even as opt-in.** Not because the AUROC is bad, but because
**the same message scores differently depending on which other 24 messages share its batch**, and the
drift is large enough to move items across the shipping threshold. A detector whose verdict depends on
the company a message keeps cannot be explained to a server owner, and cannot be appealed.

The deterministic layer does not rescue it: it adds +0.006 AUROC and makes the false-positive rate
slightly worse.

## 1. Batch composition moves the score

156 human texts, asked three times: once in all-human batches, once again in all-human batches, and
once in 50/50 batches next to AI text.

> **Corrected 2026-09-22.** The middle column was described below as "plain run-to-run noise" and as
> "nothing changed at all". It is neither. `cmd_ask_retest` calls `rng.shuffle(sample)` before it
> chunks (`deterministic.py:618`), so the re-ask held the batch *composition* constant and regrouped
> the *membership*. It is a regrouping measurement, not a repetition. A genuinely byte-identical
> repeat was never run here; it was run in `benchmark/BATCH_EFFECT.md` and flips 2.7% of spam
> positives, against the 11% below. The two agree once the middle column is read correctly: that
> report's regrouping arm flips 12%.

| stratum | n | p, all-human | p, regrouped | p, 50/50 batch | regrouping drift | composition drift |
|---|---|---|---|---|---|---|
| hc3_human_wiki_csai | 60 | 0.812 | 0.802 | 0.848 | -0.010 | **+0.046** |
| hc3_human_medicine | 10 | 0.545 | 0.491 | 0.432 | -0.054 | -0.059 |
| nonnative_learner | 48 | 0.214 | 0.197 | 0.170 | -0.016 | -0.028 |
| formal_long_comment | 13 | 0.182 | 0.179 | 0.129 | -0.003 | -0.050 |
| ALL | 156 | 0.517 | 0.501 | 0.502 | -0.016 | +0.001 |

The `ALL` row looks harmless because the drift has opposite signs per stratum and cancels. It does not
cancel for the people affected:

| condition | FPR@0.85 all | FPR@0.85 wiki-style | items flipping across 0.85 |
|---|---|---|---|
| all-human batch | 0.115 | 0.300 | 0/156 |
| regrouped, all-human | 0.096 | 0.250 | **17/156** |
| 50/50 batch | **0.256** | **0.667** | **24/156** |

Two readings, both bad:

- **Composition:** putting the same encyclopedic human paragraph in a batch that also contains AI text
  more than doubles its false-positive rate, 0.300 to 0.667.
- **Regrouping:** with the batch composition held constant and only the membership regrouped, asking
  again flips 17 of 156 items across the threshold. That is 11% instability without changing what any
  batch is made of. (This bullet said "with nothing changed at all" until 2026-09-22; see the
  correction above.)

This was measured for `ai_generated`. **It is a property of the batching contract, not of the
category**, so it should apply to spam, scam and harassment too, where the action is delete rather
than flag. That is the finding worth carrying out of this whole exercise.

**Measured for spam and harassment on 2026-09-22**, in `benchmark/BATCH_EFFECT.md`. The prediction
holds and the reason given for it does not:

- Regrouping moves 12% of spam positives across 0.85 against 2.7% for a repeated request, the one
  result there that survives correction for multiple comparisons.
- **Composition does nothing.** With neighbours randomised to chance in both arms, an all-spam batch
  and a half-clean batch flip the same messages, 18 against 18, p = 1.000 in all four cells. The
  first bullet above, the doubling of the wiki-style false-positive rate, does not reproduce outside
  `ai_generated`: what matters is *which* messages share the batch, not *what* they are.
- Harassment is not settled and cannot be with this dataset: 60% power using all 319 harassment
  messages in `items.jsonl`.

**The composition finding above survives for `ai_generated`, and the contrast is the interesting
part.** The regrouped column is the membership-only control this section always had: it moved the
wiki-style false-positive rate from 0.300 to 0.250, which is nothing, while the 50/50 arm moved it to
0.667. So for this question composition really does carry the effect. For spam and harassment it
carries none of it. The likely reason is the one REPORT3 section 3 already noticed: `ai_generated`
asks *who wrote this*, and a batch full of machine text is genuine evidence about the world for that
question. Spam asks *what is this text*, and a neighbour is not evidence either way.

## 2. At a realistic AI rate, the numbers collapse

REPORT.md measured recall in 50/50 batches. Re-run with 60 AI and 315 human texts in batches that are
16% AI, closer to a channel where someone occasionally pastes a ChatGPT answer:

| rule | recall (16% batches) | recall (50/50 batches) | FPR (n=315) | FPR wiki-style | P@2% |
|---|---|---|---|---|---|
| A >= 0.85 (REPORT.md's shipping rule) | 0.867 | 0.933 | 0.070 | **0.645** | 0.202 |
| A >= 0.88 | 0.650 | 0.900 | 0.035 | 0.355 | 0.275 |
| A >= 0.90 | **0.400** | 0.750 | 0.019 | 0.194 | 0.300 |
| A >= 0.85 AND register_total >= 1 | 0.350 | 0.400 | 0.013 | 0.129 | 0.360 |
| register_total >= 1 alone (no Jev) | 0.400 | 0.400 | 0.057 | 0.161 | 0.125 |

Recall at 0.90 goes from 0.750 to **0.400** purely by changing the company the AI text keeps. Every
recall figure in REPORT.md is measured in the friendly condition and should be read as an upper bound.

**Nothing in this table reaches precision 0.90 at any useful recall.** The best precision at a 2% base
rate is 0.36, and it costs recall 0.35. Two flags in three are wrong in the best case.

### The trap that looked like a win

Scored in all-human batches, `A >= 0.90` looked like the answer: TPR 0.804 with **zero** false positives
across all 697 careful/formal/non-native humans and all 90 chat messages, projected precision 0.79 at a
2% base rate. That number is an artefact of 50/50 batching on the positive side. In realistic batches
the same rule reaches recall 0.400. It is recorded here because it is exactly the kind of result that
gets shipped if nobody re-runs it under the condition that matters.

## 3. The deterministic layer does not help

46 model-free features over the message text, each mined from a numbered section of
<https://github.com/blader/humanizer/blob/main/SKILL.md> (itself derived from Wikipedia's "Signs of AI
writing"), so a flag could be explained to a server owner in one line. Out-of-fold, grouped 5-fold CV,
topic-matched pairs kept in the same fold, compared at a fixed recall of 0.954:

| detector | AUROC (oof) | FPR all human | FPR wiki-style | P@2% |
|---|---|---|---|---|
| Jev A alone | 0.970 | 0.096 | 0.852 | 0.169 |
| deterministic alone | 0.903 | 0.531 | 0.852 | 0.036 |
| Jev A + deterministic | **0.976** | 0.104 | 0.667 | 0.158 |

+0.006 AUROC, and the false-positive rate goes **up**. The fitted model leans almost entirely on Jev:
`logit(A)` carries a standardised coefficient of +3.50 against +0.69 for the best feature.

### Which tells are real, and which are folklore

Of the 46 features, only 10 clear AUROC 0.60 alone, and most of the famous ones are worthless on chat:

| feature | AUROC all | AUROC matched | fires on AI | fires on careful human |
|---|---|---|---|---|
| `d_words` (length) | 0.813 | 0.698 | 0.508 | 0.271 |
| `d_hedge_rate` | 0.728 | 0.675 | 0.500 | 0.218 |
| `d_register_total` (5 mined assistant-register tells) | 0.725 | 0.711 | 0.485 | 0.059 |
| `d_emdash_any` | **0.492** | 0.485 | **0.000** | 0.024 |
| `d_ai_words` (delve, crucial, robust...) | 0.515 | 0.515 | 0.031 | 0.000 |
| `d_mechanically_clean` | 0.377 | 0.383 | 0.065 | 0.335 |

- **The em dash is not a tell.** Zero of 260 AI texts contain one; the only em dashes in the corpus were
  typed by humans. AUROC 0.492, indistinguishable from a coin.
- **The "AI vocabulary" list is not a tell either**, AUROC 0.515. It fires on 3% of AI texts.
- `d_mechanically_clean` (fully punctuated, no slang, no typos, no emoji) is a *real* signal pointing the
  **wrong** way, AUROC 0.377: it fires on 34% of careful humans and 7% of AI. This is the encyclopedic-human
  failure mode showing up in a feature.
- Six layout features (`d_bold_markers`, `d_heading_markers`, `d_bullet_markers`, `d_decorative_arrows`
  and two more) are unmeasurable here: `jevmod.judge.normalize` strips newlines before judging, so
  markdown layout is gone by the time anything sees the text. On a platform that preserved layout they
  might work. Not on this one.

The strongest single feature is **message length**. The rest of the folklore is noise.

## What this means for the product

1. **`ai_generated` does not ship.** Not as a default category, not as an opt-in one. The page must not
   promise it.
2. **The batch-composition instability is a jevmod-wide problem**, not an `ai_generated` one. Measure it
   for spam and harassment before trusting any threshold to two decimal places, because those two delete
   messages. That is the next piece of work, and it is more important than this category.
3. **Nothing was tested against a humanizer.** The humanizer SKILL.md was mined for features, not used to
   generate evasive text. The AI side of the corpus is 2022-era ChatGPT. A modern model told to write like
   a Discord user, or the same text run through a humanizer, is untested and would be the obvious attack.

## What was spent and how to reproduce

All commands read `results/dataset.jsonl` and `results/raw_A.jsonl` from round one. Only `ask-*` spends money.

```
python -m benchmark.ai_detect.deterministic features      # per-feature table, free
python -m benchmark.ai_detect.deterministic trap          # deterministic FPR on the 697-item corpus, free
python -m benchmark.ai_detect.deterministic ask-trap      # Jev on the trap corpus         (paid)
python -m benchmark.ai_detect.deterministic ask-control   # 156 items, all-human batches   (paid)
python -m benchmark.ai_detect.deterministic ask-retest    # the same 156 again             (paid)
python -m benchmark.ai_detect.deterministic ask-dilute    # 60 AI + 315 human, 16% batches (paid)
python -m benchmark.ai_detect.deterministic combine       # 5-fold CV, free
python -m benchmark.ai_detect.deterministic composition   # the drift table, free
python -m benchmark.ai_detect.deterministic realistic     # the 16%-batch table, free
```

Nothing in `benchmark/ai_detect/` imports from or modifies `jevmod/`.
