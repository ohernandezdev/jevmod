# An evaluation harness, what it found, and the experiment it could not run

JEV-5. Written 2026-09-23. `benchmark/evaluate.py` scores any labelled set against any results file;
`benchmark/run_context_ab.py` produced the two runs below, 2,504 judged messages each, $0.33 in all.
Every table here recomputes for free from `benchmark/results/context_{off,on}.jsonl`.

## The short answer

Three things, in the order they matter.

1. **The spam metric on this set measures the labels, not the model.** All 129 of its false
   positives come from `openai_moderation`, which never labelled spam at all, and 87 of them are
   pornography link-farm pages labelled `nsfw`. Flagging those as spam is correct and is scored as
   an error. All 166 false negatives are YouTube self-promotion. Spam F1 of 0.363 on this set is not
   a fact about jevmod.
2. **Turning the context engine on changes the numbers by nothing and 124 verdicts on 122 messages.** AUROC
   moves at most 0.002 in any category. That is the risk answer for what JEV-17 shipped on by
   default, and it is reassuring. It is not evidence that context helps, because this set has no
   conversations in it.
3. **Two shipped thresholds are in the wrong place**, and one of those is `selfharm`.

## 1. What the harness is

`report.py` computes metrics welded to the comparison against Llama Guard, ShieldGemma and
toxic-bert. Every evaluation issue on the board needs the same arithmetic over a different pair of
files. `evaluate.py` is that arithmetic and nothing else: a results file in, a labelled set in,
precision, recall, F1, AUROC and the best-F1 threshold out, per category.

**It lists the mistakes rather than counting them**, and that is the point rather than a
convenience. Finding 1 above is invisible in a count and obvious in four lines of `--show spam`.

    python -m benchmark.evaluate context_off context_on
    python -m benchmark.evaluate context_off --show spam --limit 4

## 2. A against B, and what B actually was

| category | Δ precision | Δ recall | Δ F1 | Δ AUROC | verdicts that changed |
|---|---|---|---|---|---|
| harassment | −0.005 | −0.006 | −0.006 | +0.001 | 32 |
| minors | +0.006 | −0.047 | −0.036 | −0.001 | 10 |
| nsfw | +0.015 | +0.000 | +0.008 | −0.002 | 13 |
| selfharm | **+0.066** | +0.020 | +0.033 | −0.000 | 9 |
| spam | +0.012 | +0.004 | +0.008 | +0.002 | 50 |

The quality is the same either way. The verdicts are not: counting every category the runs scored,
**124 category-level verdicts change across 122 messages**. The column above sums to 114 because it
covers only the five categories this set labels; an earlier version of this paragraph called that
114 "messages", which it was not. It is the number a server owner would notice either way, and the
one the metric columns hide.
That is the same shape as `BATCH_EFFECT.md`: aggregates survive, individual verdicts do not.

The one column worth looking at is `selfharm` precision, +0.066, three false positives down to one.
Nine changed verdicts is too few to call it, and it is the direction a reader would hope for, which
is exactly when to be careful.

**B is not "message plus conversation", and JEV-5 asked for that.** `data/items.jsonl` has four
fields: `id`, `source`, `labels`, `text`. There are no threads, no parents and no ordering that
means anything. B fills the context window from the preceding rows of the same source, which is a
plausible set of neighbours and is not a conversation.

So this measures **the risk of what JEV-17 shipped**, which was unmeasured and is now known to be
small. It does not measure whether conversation improves judgement. That needs labelled
conversations and is blocked on JEV-11 and JEV-7 — the third time this week that a question has
turned out to be waiting on the dataset rather than on the code.

## 3. The spam metric is measuring the labels

| where the errors come from | count |
|---|---|
| spam false positives from `openai_moderation` | **129 of 129** |
| of those, labelled `nsfw` or `minors,nsfw` and not spam | 87 |
| of those, carrying no label at all | 42 |
| spam false negatives from `youtube_spam` | **166 of 166** |

`openai_moderation` labels sexual content, violence, self-harm and harassment. It does not label
spam, so every correct spam detection in those 1,680 rows is counted as a false positive. Four of
them, read rather than counted:

> `oai82` "MEMBERS | JOIN NOW | PREVIEW … Related random videos. xnxx desi mobi force six movies …"
> `oai99` "Perfect girls … Watch free and High quality adult movies … Cute shy teen …"

Those are link farms. Calling them spam is right.

The false negatives are the other half of the same problem, in the other direction:

> `yt0` "Check out this playlist on YouTube:"
> `yt6` "My videos are half way decent, check them out if you want."

Mild self-promotion, labelled spam. Whether a bare "check out my channel" should be deleted is a
policy question a server owner answers, not a fact the model got wrong.

**Nothing published uses this number.** The site and the README quote AUROC for harassment, sexual,
self-harm and minors, which are categories this set actually labels. The spam figure has never left
this directory, and after this it should not.

## 4. Two thresholds are in the wrong place

| category | shipped | F1 there | best F1 | at | gap |
|---|---|---|---|---|---|
| harassment | 0.75 | 0.718 | 0.719 | 0.78 | +0.001 |
| nsfw | 0.80 | 0.857 | 0.879 | 0.88 | +0.022 |
| minors | 0.70 | 0.551 | 0.629 | 0.60 | +0.078 |
| **selfharm** | 0.80 | 0.667 | **0.779** | **0.51** | **+0.112** |
| spam | 0.85 | 0.363 | 0.616 | 0.56 | contaminated, see section 3 |

`harassment` is well placed and `nsfw` nearly so. `selfharm` is not: at the shipped 0.80 it misses
24 of 51, and its AUROC is 0.994, so the ordering is almost perfect and the line is simply too high.

That matters more than the arithmetic says, because **F1 weights a false positive and a false
negative equally and this product does not.** `selfharm` is flag-only by design, so a false positive
costs a moderator ten seconds and a false negative costs somebody a message nobody read. F1 is the
wrong loss function for that category, and it still says the line is too high.

**These are not numbers to paste into `policy.py`.** Best-F1 is fitted to this set, the base rates
here are not a channel's, and `BATCH_EFFECT.md` measured that the second decimal of a threshold is
not reproducible. Read the column as the size of a gap that deserves its own issue.

## 5. What C and D would cost in privacy promises

JEV-5 asks for this and it is worth writing plainly, because the cost is not in tokens.

Today, `jevmod/judge.py` sends message text and the channel topic. `AGENTS.md`, the privacy notice
and the home page all say so, and the promise is enforced in code rather than described.

- **C, user context.** Sending a message's author history means sending what a named person said
  before. That turns TypeSafe into a processor of personal data, which needs the privacy notice
  rewritten, a lawful basis, retention for the profiles it creates, and erasure that reaches them.
  JEV-20, JEV-21 and JEV-22 are exactly that work and two of them are labelled blocking.
- **D, community state.** Aggregates over a community rather than a person. Cheaper in promises, but
  only while the aggregate cannot be resolved back to somebody, which is a property that has to be
  designed rather than asserted.
- **What shipped in JEV-17 costs nothing new.** Conversation context is other people's message text,
  which is the same category of data already sent and not a new one, so the sentence in the privacy
  notice is still true as written. That was checked before it shipped, not after.

The ordering that follows: C cannot be measured before the legal work, and the legal work does not
depend on the measurement. They run in parallel or C waits.

## Reproduce

```
python -m benchmark.run_context_ab off          # paid, $0.14
python -m benchmark.run_context_ab on           # paid, $0.19
python -m benchmark.evaluate context_off context_on            # free
python -m benchmark.evaluate context_off --show spam --limit 4 # free, and read them
```
