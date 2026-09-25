# benchmark/ai_detect — can Jev detect AI-generated text?

A self-contained experiment asking whether jevmod should ship an `ai_generated` category. The answer, the
numbers and the recommendation are in [REPORT.md](REPORT.md). This file is how to re-run it.

[REPORT2.md](REPORT2.md) found the batch moves the score, [REPORT3.md](REPORT3.md) proposed changing the
question to AI slop (`slop.py`, formulation D), and [REPORT4.md](REPORT4.md) measured D through Judge, unpadded,
no channel context, all nine categories (`through_judge.py`). D was not shipped: Omar chose the recall of the
authorship question, and `ai_generated` still asks it.

## What it does

520 short texts (260 written by ChatGPT, 260 written by people), each truncated to 400 characters and
normalised through `jevmod.judge.normalize`, are sent to Jev **three different ways** so we learn which question
wording works, not just whether it works at all:

| id | primitive | question |
|---|---|---|
| A | one `Noul` | "Was this written by a language model rather than typed by a person in a chat?" — criteria name the tells on both sides, and explicitly say that careful grammar, a formal register or non-native English are **not** evidence of a model. |
| B | one `Score`, 4 levels | "How much does this read like a person typing quickly in a chat?" Score is mapped to p(AI) as `1 - score/3`. An indirect proxy: it never mentions AI. |
| C | two `Noul`s | "does it read as edited, polished prose rather than a chat message" and "does it contain personal, specific detail a model would not know". Reported alone, as a mean, and as a product. |

Batching follows the `jevmod.judge.Judge` contract exactly: one request per 25 messages, state is a dict keyed
`m0..mN`, one question per message per formulation, **criteria on every question**.

## Running it

Needs the repo venv (typesafe-sdk, datasets, httpx), `TYPESAFE_API_KEY` in the environment and `PYTHONUTF8=1`.

    set PYTHONUTF8=1
    .venv\Scripts\python -m benchmark.ai_detect.run prepare     # builds results/dataset.jsonl (downloads HC3 + JFLEG)
    .venv\Scripts\python -m benchmark.ai_detect.run ask A B C   # resumable; appends to results/raw_<F>.jsonl
    .venv\Scripts\python -m benchmark.ai_detect.run report      # prints every table in REPORT.md

`ask` is resumable: it skips ids already present in `results/raw_<F>.jsonl`, prints the running input-token count
and cost after every request, and aborts if the run passes 5,000,000 input tokens. The full run measured
**463,499 input tokens, $0.0195** at the $0.042/M list price.

## Data

Built by `prepare`, provenance recorded per item, seed fixed (`20260918`):

| stratum | n | source |
|---|---|---|
| `ai_chatgpt` | 260 | `Hello-SimpleAI/HC3` `all.jsonl`, first ChatGPT answer, even spread over finance / medicine / open_qa / reddit_eli5 / wiki_csai |
| `human_hc3_formal` | 130 | the human answer to the *same* 130 HC3 questions — topic-matched pairs, so the classifier cannot win on subject matter |
| `human_chat` | 90 | 60 UCI YouTube non-spam comments + 30 short Civil Comments (toxicity ≤ 0.1), reused from `benchmark/data/items.jsonl` |
| `human_formal_long` | 20 | Civil Comments ≥ 200 chars, toxicity ≤ 0.1 — the formal-but-human false-positive risk |
| `human_nonnative` | 20 | `jhu-clsp/jfleg` validation, uncorrected English-learner sentences — the non-native false-positive risk |

Base rate 0.500. HC3 does not load through `load_dataset("Hello-SimpleAI/HC3")` any more (dataset scripts are no
longer supported); `prepare` reads `hf://datasets/Hello-SimpleAI/HC3/all.jsonl` directly.

## Files

| file | what it is |
|---|---|
| `run.py` | prepare / ask / report, all three formulations, all the metrics |
| `results/dataset.jsonl` | the 520 items with `label`, `provenance`, `stratum` and the topic-match `pair` id |
| `results/raw_A.jsonl`, `raw_B.jsonl`, `raw_C.jsonl` | one row per message per formulation: raw answers plus the batch's measured input tokens and latency |
| `REPORT.md` | the findings and the ship/do-not-ship recommendation |

## What is reported

Per formulation and per subset (`all`, chat-shaped only, topic-matched pairs): AUROC, F1 / precision / recall at
0.5 and at the best threshold, ECE (10 bins) and Brier. Then the moderation-specific parts: the lowest threshold
reaching precision ≥ 0.90 at recall ≥ 0.50, the false-positive rate per human stratum and per source, precision
projected at realistic AI base rates (10% down to 1%, using the rule-of-three upper bound on FPR where zero were
observed), the 10 worst false positives and false negatives with their text, and cost and latency from measured
input tokens.

Nothing in this directory changes jevmod itself. `categories.json` is untouched; adding `ai_generated` is a
decision REPORT.md argues for, not one this experiment makes.
