# Context engine: buffer, assembler, cache key

JEV-17, Urgent. <https://linear.app/jevmod/issue/JEV-17>

## Objective

Give the engine a notion of what came before a message: a bounded rolling window per channel, an
assembler with a hard cap on what it sends, and a cache key that stops returning a verdict computed
from somebody else's conversation.

## Why the three are one change

The issue says it and it is right: a buffer with no assembler does nothing, and either of them with
the old cache key returns a verdict from a different conversation. Shipping one of the three is a
bug rather than a third of a feature.

## What the measurements already decided

Three numbers from this week's work remove three decisions from this task.

1. **The window is ten, not twenty-five.** `benchmark/BATCH_EFFECT.md` section 7: spam recall at the
   shipped threshold is 17.3% at one message, 32.0% at five, 38.7% at ten, 37.3% at twenty-five. It
   saturates at ten and twenty-five costs 2.5 times more for nothing. Whatever the assembler's cap
   is in tokens, its cap in messages is ten.
2. **The cache is already returning verdicts from another composition**, before this task exists.
   JEV-56 measured that regrouping the same messages into different batches moves 12% of spam
   positives across their threshold, against 2.7% for a repeated request. The old key admits that
   two scorings of the same text are interchangeable and they are measurably not.
3. **This is not the JEV-57 fix, and must not be built as if it were.** `benchmark/batch_context.py`
   measured that context present in the state but *not asked about* recovers only 23% of the
   batch-size gap. JEV-57 needs padding that is judged and discarded. JEV-17 needs context that is
   read. They share the buffer and nothing else, and conflating them would give each half of what it
   needs.

## The tension this task has to resolve, not dodge

A cache key that includes the context is correct and almost never hits: every message has a slightly
different window, so a spam wave posting identical text 200 times pays 200 times. A key that ignores
context keeps the hit rate and lies.

The decision, and the reason, go in the code next to the key. The starting position is that
correctness wins and the hit rate is bought back by making the context **coarse on purpose**: the
key hashes the window, so identical text arriving in an identical window still hits, which is
exactly what a spam wave looks like.

## Scope

In:

- `jevmod/core/context.py`: the buffer and the assembler, with bounds on messages per channel, age,
  and total channels held.
- `Message.context`, populated by `ModerationService` so every adapter gets it without changing.
- `judge()` sends the context; `_key()` includes it.
- Tests, offline for the bounds and the key, and one real-API test that the context reaches Jev and
  changes an answer it should change.

Out:

- **Measuring whether context improves judgement.** That is JEV-18 and JEV-5, and it needs the
  harness those issues own. This task ships the capability and the ability to measure it, not the
  verdict on whether it helps.
- Sending anything but message text and channel topic to TypeSafe. `AGENTS.md` and the privacy
  notice both promise no author names or ids, and JEV-20 to JEV-22 gate changing that. **Context is
  other people's message text, which is the same category of data already sent, and nothing in this
  task adds a new kind.**

## Tasks

- [x] T1. `jevmod/core/context.py`: `ConversationBuffer` with bounded memory, `window()`, and an
      assembler with a hard token cap and a documented estimate.
- [x] T2. `Message.context`; `judge()` puts it in the state; `_key()` hashes it.
- [x] T3. `ModerationService.moderate` fills it, so the five adapters inherit it unchanged.
- [x] T4. Offline tests: the bounds hold, the key changes with the context, and the key still hits
      when the window repeats.
- [x] T5. One real-API test that context reaches Jev and moves an answer it ought to move.
- [x] T6. `AGENTS.md`, and a note in `BATCH_EFFECT.md` pointing JEV-57 at the buffer it now has.

## Acceptance

- Memory is bounded in all three directions and a test proves each bound by exceeding it.
- A verdict cached under one window is not returned under a different one.
- Nothing but text and channel topic leaves the process.
- `ruff`, `mypy`, and the full suite green, with the real-API tests skipping without a key.

## Progress

All six done. 270 tests green, `ruff` and `mypy` clean.

Measured while writing T5, three runs, so the test's margin is evidence rather than a guess:
"yeah she totally deserves it lol" scores harassment 0.14 to 0.16 alone and 0.57 to 0.64 under a
three message pile-on. Delta +0.43 to +0.48; the test asserts +0.25.

Two things worth carrying:

- **The model attributes correctly.** "just do it already, nobody is stopping you" under three
  self-harm messages went harassment 0.12 to 0.75 and selfharm 0.04 to 0.20. It read the message as
  somebody egging on a person in trouble rather than as the person in trouble, which is the right
  reading and not the one a keyword would reach.
- **Whether context improves accuracy overall is still unmeasured**, and deliberately: that is
  JEV-18 and JEV-5. This shipped the capability and one demonstration, not the claim.
