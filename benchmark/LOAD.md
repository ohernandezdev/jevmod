# Throughput, concurrency and what a message costs at scale

JEV-6. Measured 2026-09-23 with `benchmark/load.py` against the live API, from one machine on one
key. Raw rows in `benchmark/results/load.jsonl`.

## The two numbers

**The service starts pushing back between 16 and 32 simultaneous streams.** At sixteen, nothing is
rate limited and the median is flat. At thirty-two, 45 of 128 requests come back 429.

**The conversation window costs 41% more per message.** 1,305 input tokens without it and 1,842 with
it, measured on the same 2,504 messages rather than estimated from a rate card.

## Concurrency

Batches of 25, eight categories asked, four requests per worker, **retries turned off** so a 429
surfaces as an error instead of becoming a longer latency. Production keeps its three retries; this
arm removes them because they hide the number it is looking for.

| streams at once | requests | p50 ms | p95 ms | max ms | rate limited |
|---|---|---|---|---|---|
| 1 | 4 | 515 | 548 | 548 | 0 |
| 2 | 8 | 585 | 664 | 664 | 0 |
| 4 | 16 | 551 | 683 | 683 | 0 |
| 8 | 32 | 528 | 692 | 695 | 0 |
| 16 | 64 | 528 | 725 | 768 | 0 |
| **32** | 128 | 562 | 987 | 1102 | **45** |

**The median does not move up to sixteen**, 515 ms to 528, and the p95 grows by about a third, 548
to 725. That is a service that is not queuing behind itself at that load; the wait is the request,
not the contention.

At sixteen streams the sustained rate is about **750 messages per second**: sixteen requests of
twenty-five, each taking 528 ms. That is the ceiling worth designing against, not the 429 line.

### What it means for the hosted service

`Batcher` groups per tenant on a two second window, so a concurrent stream is a server that is busy
right now. **Thirty-two simultaneously busy servers is where 429s begin.** In production the three
retries absorb them, so the symptom is not an error, it is latency: a moderation decision that
arrives late, which on a raid is the only time it matters.

The ramp stopped at the first level that pushed back rather than climbing to find how much worse it
gets. This is a paid API and the question was where the ceiling is.

**What this does not say.** One machine, one key, one region. Rate limits are usually per key, so a
second key may or may not double this, and that is a question for TypeSafe rather than for a
script. Nothing here measured a sustained hour; four requests per worker finds a ceiling, not a
thermal one.

## Cost

Priced from tokens that were actually billed, in the two arms of `benchmark/run_context_ab.py`.

| request | tokens per message | $ per 1K | $ per 100K | $ per 1M |
|---|---|---|---|---|
| without the conversation window | 1,305 | $0.05 | $5.48 | $54.82 |
| with it | 1,842 | $0.08 | $7.74 | $77.37 |

**41% more per message with the window.** JEV-6 exists to decide how much context the engine can
afford, and that is the answer: at a million messages the window is $22.55.

Read against the plan, $3.99 per server per month, a server would have to send about **51,000
messages a month** before the model cost with the window reaches the price. Without it, 73,000.

### And this is the second cost the window has

`benchmark/EVAL.md` measured what the window buys on the labelled set: AUROC moves at most 0.002 in
any category, and 114 verdicts change. So today it costs 41% and buys nothing measurable, **on a set
that has no conversations in it**. Whether it buys anything on real conversations is JEV-11 and
JEV-7's dependency, not a reason to keep or drop it yet.

That is an honest argument for leaving it on and revisiting, not for either certainty.

## Reproduce

```
python -m benchmark.load concurrency          # paid, ramps 1 2 4 8 16
python -m benchmark.load concurrency 32 64    # paid, continues a ramp that found no ceiling
python -m benchmark.load cost                 # free
```
