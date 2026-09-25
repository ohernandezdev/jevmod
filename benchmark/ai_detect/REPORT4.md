# Round four: the slop question through Judge, unpadded, no channel context, all nine categories

Run on 2026-09-25, after [REPORT3.md](REPORT3.md), which recommended shipping formulation D at 0.85 as a slop
rule. REPORT3 asked D as the only question in its request. A server that turns `ai_generated` on asks it
beside every other category it has enabled, in one request, and [REPORT2.md](REPORT2.md) showed that what
shares a request moves the score, so REPORT3's numbers were a premise until this run.

This run is D through Judge, unpadded, no channel context, all nine categories. It is not the full production
path: production pads a batch with the channel's recent messages, attaches the conversation before each message
and sends the channel's own topic, and none of the three was measured here.

**D was not shipped.** See the decision at the end.

The acceptance criteria were written before the run, in `odd/tasks/ai-slop-rule.md`, and are not changed here.

## The short answer

**D passes every criterion as written through `Judge`, twice, and the careful-human row passes by one text,
and only because that pool includes 300 non-native learners.** At 0.85, with all nine categories in the
request, it fires on 83 of 260 assistant answers (31.9%), on **0 of 90** real chat messages, **0 of 300**
non-native learners, **2 of 60** encyclopedia-style human texts and **20 of 697** careful humans (2.87%; the 3%
limit is 20.9, so 21 would fail). The same requests sent a second time give 85, 0, 0, 1 and 19.

None of the 300 non-native learners fires, so they only enlarge the denominator. **Careful humans without the
non-native learners: 20 of 397, 5.0%, above the 3% criterion** (19 of 397, 4.8%, on the second run).

Putting D in the request instead of the authorship question does not move the other eight categories: no
message crosses any of their thresholds.

## 1. Acceptance

`python -m benchmark.ai_detect.through_judge report`. 1047 texts, the same ones REPORT3 used, shuffled with
REPORT3's seed so every request of 25 holds the texts `slop ask-mixed` grouped together (about 25% AI), sent
through `jevmod.judge.Judge`, unpadded, no channel context, all nine categories: the lead filler at `m0`,
the default topic "general chat" on every message, nothing from a conversation.

| set | n | fires | rate | must be | result | same request again | D alone (REPORT3) | authorship through Judge |
|---|---|---|---|---|---|---|---|---|
| real chat | 90 | 0 | 0.000 | 0 | pass | 0 | 0 | 0 |
| non-native learners | 300 | 0 | 0.000 | at most 3 | pass | 0 | 0 | 0 |
| encyclopedia style | 60 | 2 | 0.033 | at most 3 | pass | 1 | 0 | 44 |
| careful humans | 697 | 20 | 0.029 | at most 3% | pass | 19 | 13 | 58 |
| careful humans without the non-native learners | 397 | 20 | 0.050 | at most 3% | **fail** | 19 | | 58 |
| assistant answers | 260 | 83 | 0.319 | at least 20% | pass | 85 | 75 | 244 |

The strata are REPORT3's: "careful humans" is all 697 texts of the trap corpus, so it contains the
encyclopedia-style (`hc3_human_wiki_csai`) and non-native (`nonnative_learner`) rows. The criterion was
written against that pool of 697, so the row passes as written; the extra row, not a criterion set before the
run, shows what the pass rests on. "Real chat" is the 90
`human_chat` texts of round one; "assistant answers" its 260 ChatGPT texts. "Authorship through Judge" is the
question `ai_generated` asked before D, put through the identical run for comparison.

Seven texts, all short chat or YouTube comments, are skipped by the pre-filter as under eight letters. In
production they would not be judged either, so they count as not firing. None of them is AI text.

## 2. The margin is the finding

Two numbers in that table should be read before the word "pass":

- **Careful humans: 20 of 697, one short of failing, and 5.0% once the 300 non-native learners, none of whom
  fires, are taken out.** 21 more careful humans sit between 0.80 and 0.85.
  Sending the identical requests again moved 9 of 1040 judged texts across 0.85 (mean absolute difference
  0.011) and the row came out at 19. A different grouping, a different filler at `m0`, or a model update can
  plausibly put it at 22.
- **Encyclopedia style: 2 of 60 through Judge, 0 of 60 alone.** Asked beside eight other questions, D reads
  higher on the AI and reference-style sides (table below), and that is enough to put two definitional openers
  over the line.
- **Medicine: 16 of 60 under D, 0 of 60 under the authorship question**, in the same run through `Judge`.
  Section 3 has who they are.

| side | n | mean p alone | mean p through Judge | drift | fires alone | fires through Judge |
|---|---|---|---|---|---|---|
| assistant answers | 260 | 0.671 | 0.691 | +0.019 | 75 | 83 |
| careful humans | 697 | 0.266 | 0.274 | +0.008 | 13 | 20 |
| encyclopedia style | 60 | 0.561 | 0.597 | +0.037 | 0 | 2 |
| real chat | 90 | 0.039 | 0.036 | -0.003 | 0 | 0 |

19 of 1040 judged texts cross 0.85 between D alone and D through `Judge`. Asking it beside the other eight questions makes D slightly
more willing to fire on text that is already close, which buys 8 assistant answers and costs 7 careful humans,
and leaves chat where it was.

## 3. Who the 20 are

Read one by one. Every one does at least one thing on D's list.

| how many | stratum | what it does |
|---|---|---|
| 16 | medicine | doctors on a medical Q&A site. 15 open with a greeting or the site's house line ("Hi, Welcome to healthcaremagic", "Dear patient", "I can understand your concern"), 14 refer the patient to a doctor or a specialist, and several define the condition first ("Hygroma means the accumulation of...") |
| 2 | wiki_csai | encyclopedia openers: "A cognitive architecture refers to both a theory...", "Deep structure and surface structure (also D-structure and S-structure...)" |
| 1 | open_qa | "State schools (also known as public schools or government schools) generally refer to..." |
| 1 | finance | "I will answer this question broadly for various jurisdictions, and also specifically for the US", staging the answer before giving it |

Under a slop label these are defensible: pasted encyclopedia text and templated consultation boilerplate are
what the rule names. The medicine stratum is the cost, though: **16 of its 60 texts fire under D and 0 of 60
under the authorship question.** Handing the question off to an authority is on D's list, so **health and
support communities will see flags on replies that refer people to a professional**, which in those servers is
the right reply.

## 4. The other eight categories do not notice

The same 1047 texts, the same batches, and only the `ai_generated` question changed between the two runs.

| category | threshold | mean abs diff | fires, authorship | fires, D | crossed |
|---|---|---|---|---|---|
| spam | 0.85 | 0.002 | 0 | 0 | 0 |
| scam | 0.75 | 0.000 | 0 | 0 | 0 |
| harassment | 0.75 | 0.003 | 14 | 14 | 0 |
| nsfw | 0.8 | 0.001 | 2 | 2 | 0 |
| offtopic | 0.9 | 0.016 | 0 | 0 | 0 |
| selfharm | 0.5 | 0.001 | 0 | 0 | 0 |
| doxxing | 0.8 | 0.001 | 0 | 0 | 0 |
| minors | 0.7 | 0.001 | 0 | 0 | 0 |

This corpus has almost no positives for those categories, so it shows only that D does not push clean text over
their lines. It says nothing about their recall.

## 5. Cost

42 requests per run, about 35,000 input tokens each (25 texts plus the filler, nine questions each).

| run | input tokens | cost at $0.042/M |
|---|---|---|
| D | 1,469,519 | $0.0617 |
| D, same requests again | 1,469,519 | $0.0617 |
| authorship | 1,413,255 | $0.0594 |
| **total** | 4,352,293 | **$0.183** |

D's question costs 56,264 tokens more than the authorship question over 1047 texts, 4% of the request. The
task estimated under $0.10 for one run; one run was $0.062, and the retest and the comparison tripled it.

## What this does not settle

Everything REPORT3 listed as unmeasured still is: precision at a real base rate (every label here is
authorship), modern models asked to sound casual, text run through a humanizer. Add three: **the careful-human
row has no headroom** (5.0% without the non-native learners), so the next measurement of it, on any corpus,
should be expected to land on either side of 3%; the padding, conversation context and channel topic
production sends were not in these requests; and health and support communities were not measured, only 60 medical Q&A answers.

## Reproduce

```
python -m benchmark.ai_detect.through_judge ask d            # paid, $0.062
python -m benchmark.ai_detect.through_judge ask d_retest     # paid, $0.062
python -m benchmark.ai_detect.through_judge ask authorship   # paid, $0.059
python -m benchmark.ai_detect.through_judge report
```

Raw scores for all nine categories per text are in `results/raw_judge_<variant>.jsonl`. `jevmod/categories.json`
asks the authorship question, so `through_judge.py` swaps D in memory from `slop.questions_d("{m}")` for `d`
and `d_retest`, which is what the file held during the run.

## Decision, 2026-09-25: not shipped

Omar chose recall. Through Judge the authorship question catches 244/260 assistant answers vs D's 83/260, at
the cost of 58 human fires vs 20, of the same 787 human texts (90 real chat, 697 careful humans). `ai_generated`
keeps the authorship question, off by default, experimental and flag-only.
