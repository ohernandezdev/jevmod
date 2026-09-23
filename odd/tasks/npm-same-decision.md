# The npm package and the Python one stopped agreeing

JEV-58, High. <https://linear.app/jevmod/issue/JEV-58>

## Objective

`AGENTS.md` says the npm package asks the same questions and returns the same decision as the Python
one. Make the second half true again, or say plainly where it is not.

## What was actually wrong, and it was worse than the ticket said

The ticket was about the conversation buffer: JEV-17 added `Message.context`, `core/context.py` and
a cache key that folds the window in, and npm got none of it. That is real, and it is a question
about a feature.

Reading the code to answer it turned up three things the ticket did not know about, all of which are
the same text scoring differently in the two packages for reasons nobody chose:

1. **npm put real messages at `m0`.** `judge.ts` wrote `stateMessages[\`m${i}\`]` from `i = 0`.
   `jevmod/judge.py` has never done that since the measurement: a message at `m0` gains +0.014 from
   its neighbours where every other position gains about 0.22, and the cost is asymmetric, spam
   positives losing 0.15 while clean text moves 0.01 (`benchmark/position_zero.py`, 300 messages).
   Shipping the filler took spam recall from 17.3% to 29.3%. A single-message `check()`, which is
   the npm package's ordinary use, landed on that position every single time.
2. **The cache key had already diverged and a test said otherwise.** Python's `_key` moved from a
   `|`-joined string to a JSON payload, for a demonstrated reason: the separator was a character
   "no message text contains", `normalize` does not strip it, and one message containing it served
   a verdict from one conversation into another. npm still joined on `|`, where a channel topic
   containing a pipe is enough. The npm test that claimed the keys matched asserted a hash somebody
   had computed once in Python and pasted in as a literal, so it went on passing against a function
   that no longer existed.
3. **npm did not sort the categories** into the key, so the same two categories in either order
   were two cache entries.

## Decisions

**Port the `m0` discipline and the padding. Do not port `ConversationBuffer`.** The filler is
twenty lines, needs no state, and its benefit is measured. The buffer is a stateful subsystem whose
benefit is still unmeasured — JEV-18 has not run — and the package's own future is open (JEV-48,
JEV-49). Porting the measured part and leaving the unproven part is the split that survives either
answer to those.

**The padding is an argument, not a field on `Message`.** There is no buffer here to fill a field
from, and a field nobody fills is a worse lie than an argument nobody passes.

**The padding goes in the cache key**, which the first version of this port left out. The argument
for leaving it out was that padding has no conversational identity, so keying on it only costs hit
rate. That is the wrong test. The key's job is not to identify a conversation; it is to stop a score
computed beside one set of neighbours being handed back beside another, and JEV-56 measured that
difference at 12% of spam positives crossing their threshold against 2.7% for a repeated request.
Python's `_key` docstring makes exactly this argument and it transfers whole.

**The keys are byte-identical and that is now checked rather than claimed.** `json.dumps` puts a
space after each comma and `JSON.stringify` does not, which is the entire reason `pyJson` exists.
Nothing shares the two caches today, so the property has no consumer — but the claim was already in
the repository, and a claim that cannot notice being broken is how the last three drifts happened.

## Tasks

- [x] T1. `PAD_TO`, `LEAD_FILLER`, `MAX_CONTEXT_TOKENS`, `CHARS_PER_TOKEN` and `assemble`, ported
      with the measurements in the comments rather than the numbers alone.
- [x] T2. `judge(messages, categories, customRules, padding)`: normalise, dedupe, drop what is
      already being judged, trim to the budget, oldest item to `m0`, real messages from `m1`,
      the rest trailing, questions for every position, answers for padding discarded.
- [x] T3. `padding` as an option on `check` and `checkMany`, so the feature is reachable without
      dropping down to `Judge`.
- [x] T4. The cache key: padding folded in, categories sorted, JSON payload instead of a `|` join.
- [x] T5. The golden-hash test replaced by one that runs the Python `_key`, over five cases
      including a pipe in the topic and non-ascii text.
- [x] T6. `AGENTS.md` and the package README say what is shared and what is not.

## Acceptance

- No real message reaches `m0` by any path through `judge`, and no padding text reaches a `Verdict`
  or the cache.
- The npm key equals the Python key for the same inputs, proven by running both.
- The same text with different padding is judged twice; with identical padding it is judged once.

## Progress

All six in. 40 offline tests pass, 6 skipped (the two files that call the real API).

**Two things the work itself caught, both worth keeping.**

The cross-language test passed five times before it ran once. `pythonKey` returned `null` on any
failure and the test returned early, so "no Python here" and "the keys match" were the same green
tick. It now probes once and uses `it.skipIf`, so a skip is a skip in the output. The first run
after that fix found the probe had been failing all along on a one-word mistake in the harness.

Then the non-ascii case failed for a reason that was not the code: a Python subprocess on Windows
gets cp1252 on stdin, so `café ❤` was mangled on its way in and the two sides were being compared on
different inputs. `sys.stdin.buffer.read().decode("utf-8")` is the fix, and the case that caught it
stays in the suite.

The README also said the **newest** padding item takes `m0`. It is the oldest, in both packages.
Checked by running it rather than by reading it.

## Next step

Closed for what it covers. The conversation buffer stays unported on purpose; if JEV-18 measures
that the window earns its cost, that is the point at which npm needs either a buffer of its own or
the honest answer that the local engine is going away (JEV-48).
