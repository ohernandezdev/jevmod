# An evaluation harness, and the experiment it was supposed to run

JEV-5, Urgent. <https://linear.app/jevmod/issue/JEV-5>

## What the issue asks for, in three parts

1. Generalise `benchmark/report.py` into a command over any labelled set, with precision, recall, F1
   and the actual false positives and negatives per category.
2. Run A (message alone) against B (message plus conversation) over the 2,531 items.
3. Write the delta, and what experiments C and D would cost in privacy promises.

## Part 2 cannot be run as written, and saying so is the honest half of this task

`benchmark/data/items.jsonl` has four fields: `id`, `source`, `labels`, `text`. **There is no
conversation in it.** 1,680 rows are single prompts from OpenAI's moderation eval, 351 are isolated
Civil Comments rows, 500 are YouTube spam comments with no thread, no parent and no ordering that
means anything.

JEV-11 already said this: *"son mensajes sueltos sin hilo, así que no sirven para los experimentos C
y D tal cual"*. It applies to B as well, and nothing has changed since.

Running it anyway, by calling the neighbouring rows "the conversation", would produce a number with a
name it has not earned. That is the exact failure this week has spent its time correcting.

**What is run instead, and it is not a substitute:** the context engine shipped in JEV-17 is on by
default and its effect on the full labelled set was never measured. Feeding it the preceding rows
from the same source is not a conversation, but it is a plausible set of neighbours, and what it
measures is real and decision-relevant: **how much does turning the context engine on move the
verdicts?** If it moves a lot, shipping it on by default is a bigger change than it looked.

That answers "what is the risk of what I shipped", not "does conversation improve judgement". The
second question stays open and belongs to JEV-11 and JEV-7, which is the same dependency JEV-56's
harassment half ran into.

## Scope

In:

- `benchmark/evaluate.py`: one command, any labelled set, any results file. Per category: precision,
  recall, F1, AUROC, and the ids of every false positive and false negative so a person can read
  them rather than trust a number.
- The A/B over the 2,531 with the context engine off and on.
- The report, including what part 2 needs before it can be run.
- What C and D cost in privacy promises.

Out:

- Claiming a conversation-context result from a set with no conversations.

## Tasks

- [x] T1. `benchmark/evaluate.py`, with the false positives and negatives listed and not just counted.
- [x] T2. Run A and B over the full set.
- [x] T3. `benchmark/EVAL.md`: the numbers, the delta, and what part 2 is blocked on.
- [x] T4. The privacy cost of C and D, written against `AGENTS.md` and the privacy notice as they
      stand rather than in the abstract.

## Acceptance

- The harness runs on a labelled set it was not written for, without editing it.
- Every table can be recomputed from committed results for free.
- The report says plainly which question was answered and which was not.

## Progress

All four done, `benchmark/EVAL.md`. 2,504 messages judged twice, $0.33.

**The harness found something bigger than the experiment it was built for.** All 129 spam false
positives come from `openai_moderation`, which never labelled spam; 87 of them are pornography link
farms labelled `nsfw`, and flagging those as spam is correct and is scored as an error. All 166
false negatives are YouTube self-promotion. **Spam F1 of 0.363 on this set is a fact about the
labels.** It was invisible in the counts and obvious in four lines of `--show`, which is the whole
argument for listing the mistakes rather than totalling them.

Nothing published uses that number: the site and the README quote AUROC for the four categories the
set actually labels.

The A/B: the context engine changes AUROC by at most 0.002 and changes 114 verdicts. That answers
the question JEV-17 left open about what it shipped on by default, and it is reassuring. It does not
answer whether conversation helps, because the set has no conversations. Third time this week a
question has turned out to be waiting on JEV-11 and JEV-7.

Also found, and it needs its own issue: **`selfharm` ships at 0.80 and its best-F1 threshold is
0.51**, worth 0.112 of F1, on an AUROC of 0.994. The ordering is nearly perfect and the line is too
high. It matters more than F1 says, because F1 weights a false positive and a false negative equally
and a flag-only category does not.
