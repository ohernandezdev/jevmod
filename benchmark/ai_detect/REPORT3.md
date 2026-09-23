# Round three: the right question, and why the false positives were not false

Run on 2026-09-19, after [REPORT2.md](REPORT2.md), which recommended not shipping `ai_generated` at all.
Two changes, both Omar's:

1. **The product question is not authorship.** Nobody cares who typed it. What a server owner wants gone is
   assistant slop: the scaffolding, the unasked-for definitions, the tidy summary. A human who writes that
   way is producing the same thing a pasted assistant answer produces.
2. **Regex over the humanizer's phrase lists is brittle**, so ask Jev to match those same patterns by
   meaning instead. That is formulation D, in `slop.py`.

Both turned out to be right, and the second changed less than the first.

## The short answer

**The rule to ship is `D >= 0.85`, and it is a slop rule, not a detector.** On 1047 texts in shuffled
25%-AI batches it fires on 288 of every 1000 assistant answers, on **0 of 90** real chat messages, on
**0 of 60** copied-encyclopedia texts and on **0 of 300** non-native learners. It fires on 13 of 697
careful humans, and **all 13 are human doctors writing assistant boilerplate**. Under a slop label those
are correct.

Its recall is low and that is the honest cost. It is a rule that almost never bothers anyone and catches
under a third of what it is looking for.

## 1. Jev matching by meaning beats the regex, by less than expected

`regex` is any of the five mined assistant-register patterns or any staging tell, exactly what
`deterministic.py` computes. Scores from shuffled 25%-AI batches.

| rule | fires on AI | careful human | wiki-style | non-native | real chat |
|---|---|---|---|---|---|
| regex (phrase lists) | 0.485 | 0.070 | 0.200 | 0.043 | **0.000** |
| D >= 0.7 | 0.554 | 0.100 | 0.317 | 0.007 | **0.000** |
| **D >= 0.85** | 0.288 | **0.019** | **0.000** | **0.000** | **0.000** |
| D >= 0.9 | 0.177 | 0.003 | 0.000 | 0.000 | 0.000 |
| regex OR D >= 0.85 | 0.531 | 0.083 | 0.200 | 0.043 | **0.000** |
| regex AND D >= 0.85 | 0.242 | 0.006 | 0.000 | 0.000 | 0.000 |

The regex is blind on 134 of 260 AI texts, and asking Jev by meaning recovers some of them: 28% at 0.7,
9% at 0.85. It is a real gain and a small one. `regex OR D >= 0.85` buys 4.6 points of recall over the
regex alone for 1.3 points of false-positive rate.

What the regex misses and Jev catches is the paraphrase, exactly as predicted. This one contains no phrase
from any list and Jev gives it 0.95:

> It is difficult to accurately diagnose a medical condition without examining a patient in person and
> obtaining a full medical history. However, there are a few possible explanations for the symptoms you
> have described. One possibility is endometriosis, a condition in which tissue similar to the lining of
> the uterus [...]

Three patterns, none of them literal: hedge scaffolding, announcing a list, an unasked-for definition.

## 2. The false positives were not false

`D >= 0.85` fires on 13 of 697 careful humans. All 13, read one by one:

| id | stratum | D | how it opens |
|---|---|---|---|
| thmedicine16 | medicine | 0.91 | "Hello, Your symptoms are suggestive of [...] Hope I have answered your question. Let me know if I can assist you further." |
| thmedicine14 | medicine | 0.87 | "Hi Thanks for using healthcare magic" |
| thmedicine21 | medicine | 0.86 | "Hi Welcome to healthcaremagic I have gone through your query and understand your concern." |
| thmedicine13 | medicine | 0.86 | "Welcome to Healthcare Magic" |
| thfinance16 | finance | 0.87 | "I will answer this question broadly for various jurisdictions, and also specifically for the US" |
| *(9 more)* | medicine | 0.85-0.90 | the same HealthcareMagic consultation boilerplate |

Twelve are doctors on HealthcareMagic answering patients in a house style that **is** assistant register:
a greeting, a restatement of the concern, a numbered recommendation, a closing offer to help further. The
thirteenth stages its answer before giving it. They are labelled human because a human typed them, and the
label is authorship. Against a slop label every one of them is a hit.

So the measured false-positive rate of `D >= 0.85` on 1047 texts is somewhere between **1 in 697 and 0**,
and the rate on the population that matters, people talking in a channel, is **0 in 90**.

This is the part of REPORT2 that the reframing repairs. Its conclusion, "humans who write like an
encyclopedia are flagged as machines", was the fatal objection to an authorship detector. It is not an
objection to a slop rule: a person pasting encyclopedia text into #general is doing the thing the rule
exists to catch.

## 3. The batch instability shrinks but does not go away

The same 1047 texts were asked twice: once in the order they load, which puts every AI text in an all-AI
batch, and once shuffled so every batch is about 25% AI.

| side | n | mean p, sorted | mean p, shuffled | drift | fires@0.7 sorted | fires@0.7 shuffled |
|---|---|---|---|---|---|---|
| AI | 260 | 0.712 | 0.671 | -0.041 | 0.619 | 0.554 |
| careful human | 697 | 0.265 | 0.266 | +0.002 | 0.083 | 0.100 |
| **wiki-style** | 60 | 0.511 | 0.561 | +0.050 | **0.067** | **0.317** |
| real chat | 90 | 0.037 | 0.039 | +0.002 | 0.000 | 0.000 |

65 of 1047 items cross 0.7 between the two runs, 6.2%, against 11% for the authorship question in REPORT2.
Asking about patterns present in the text is more stable than asking who wrote it, which is what you would
expect: one is a property of the text and the other is a guess about the world.

It is still not zero, and the wiki-style row is why: its fire rate at 0.7 goes from 0.067 to 0.317 on the
same 60 texts. **At 0.85 that row is 0.000 in both runs**, which is the other reason to ship 0.85 rather
than a threshold that buys recall.

## What to ship

`D >= 0.85`, flag only, off by default, described as a slop rule and not as AI detection. Optionally
`OR regex`, which trades a near-zero false-positive rate for 24 more points of recall; that trade needs
the disagreement set read, and `results/disagree_D.md` has all 129 items.

What is still not measured, and cannot be until someone labels slop directly:

- **Precision at a real base rate.** Every precision number in REPORT.md and REPORT2.md is computed against
  an authorship label, so none of them apply to this rule.
- **Modern slop.** The AI side is 2022-era ChatGPT answering questions, which is dense in these patterns.
  Newer models asked to sound casual are untested, and so is text run through a humanizer.
- **The other categories.** The batching instability is smaller here but it is the same mechanism, and
  spam and harassment remain unmeasured. They delete messages. That is still the more important work.
  **Done on 2026-09-22: `benchmark/BATCH_EFFECT.md`.** Spam behaves the same way and the cause is not
  what these reports assumed: regrouping the batch matters, what the neighbours *are* does not.
  Harassment could not be settled, for want of labelled messages rather than for want of a run.

## Reproduce

```
python -m benchmark.ai_detect.slop ask        # sorted batches      (paid, $0.016)
python -m benchmark.ai_detect.slop ask-mixed  # shuffled 25%-AI     (paid, $0.016)
python -m benchmark.ai_detect.slop report
```

Nothing in `benchmark/ai_detect/` imports from or modifies `jevmod/`.
