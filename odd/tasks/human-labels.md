# Capturing human decisions, and what a label has to survive

JEV-12, Urgent. <https://linear.app/jevmod/issue/JEV-12>

## Objective

One storage that both surfaces write: the Discord buttons under a flag, and approve/dismiss/act in
the dashboard. A human decision on a message, kept as a label.

## Why this is the load-bearing ticket on the board

Three separate measurements this week stopped at the same wall.

- **JEV-56** could not settle the batch effect for `harassment`: 60% power using every one of the
  319 harassment rows in `items.jsonl`, and it needs about 500.
- **JEV-5** could not run its own A/B: the labelled set has no conversations in it, four fields and
  no threads.
- **JEV-59** found `selfharm` shipping at 0.80 when its best-F1 line is 0.51, and the set that says
  so is 51 rows.

All three are waiting on data nobody is collecting, while the product watches moderators make the
exact judgements those measurements need and throws every one of them away.

## The design decision that makes this work, and it is about retention

`decisions` rows are purged after 30 days. That promise is in the privacy notice and in the store.
A label that points at a message whose text is gone in 30 days is not a dataset.

**So a label does not store text.** It stores the scores the model gave, the category, the human's
verdict, and when. That is enough to compute precision and recall at *any* threshold, for ever,
without keeping a word anybody wrote. It is exactly what JEV-59 needs and it needs no text at all.

What it cannot do is re-judge the message later with a new prompt. That is a real loss and it is
worth it: the alternative is a table of everybody's messages kept indefinitely, which is a different
product and a different privacy notice.

**And it stores no moderator identity.** Who clicked is personal data about a second person, the one
the privacy notice does not even mention. The dataset does not need it. If rate-limiting one person
clicking fifty times ever matters, that is a counter in memory, not a column.

## What a button press should do to the threshold, which is now a measured question

`discord_bot.py` currently moves the line by +0.03 or −0.02 per press and stores nothing.
`benchmark/nudge_loop.py` measured what that loop does over time: its equilibrium sits where
precision is 0.60, a number set by the ratio of two constants, and **at a 2% or 5% spam rate that
equilibrium does not exist at all**, so the line ratchets to 0.99, nothing scores that high, the
category stops flagging, it stops being corrected, and it stays off. Recall 0.00, and nobody is told.

So this ticket cannot just add a write next to the nudge. It has to decide what the press does, and
the measurement says the current answer is wrong.

Starting position: **the press records a label and stops moving the line by itself.** A threshold
then moves when somebody, or a calibration, has enough labels to justify it — which is the thing the
labels are for. That turns a silent ratchet into an explicit change, and it is a product decision
Omar signs off rather than one a measurement makes.

## Scope

In:

- `Store.add_label` and `Store.labels`, defined once so both surfaces cannot drift.
- The Discord buttons write one.
- Whatever the buttons should now do to the threshold, decided above and confirmed before shipping.
- Offline tests, including retention and that no text or moderator id is stored.
- `benchmark/evaluate.py` able to read a label export, so the dataset is usable the day it exists.

Out:

- The dashboard surface. `/app/[guild]/log` lives in `jevmod-private`; this ships the storage and the
  API it calls, and that repo's change is its own commit.

## Tasks

- [x] T1. The `labels` table and `Store.add_label` / `Store.labels`, with retention decided and
      written next to it.
- [x] T2. The Discord buttons write a label.
- [x] T3. Decide and implement what a press now does to the threshold, with `nudge_loop.py`'s numbers
      in the comment.
- [x] T4. Offline tests: both surfaces write the same shape, no text, no moderator id, retention
      holds, a label survives the purge of the decision it refers to.
- [ ] T5. An export `benchmark/evaluate.py` can score. Not built: there are zero labels to export,
      and a format decided with no rows in it is a format decided twice. `Store.labels()` returns the
      shape it will read.
- [ ] T6. `AGENTS.md`, and the private repo's issue for the dashboard half.

## Acceptance

- A label outlives the 30-day purge of the message it describes, and contains no text.
- The two surfaces call the same method; a test asserts there is only one writer.
- `evaluate.py` scores a label export without being edited for it.

## Progress

**Omar decided the open question**: the press records the label and the sensitivity is changed by
hand. That is what shipped.

`labels(ts, tenant, message, category, agreed, scores, source)`, written by `Store.add_label` and
nothing else, asserted by a test that counts the `INSERT INTO labels` statements across the store,
the service, the Discord adapter and the API and requires exactly one. The dashboard in the private
repository will call the same method, which is the issue's own argument for doing both at once.

No text and no identity, both deliberate and both with a price written next to them. The label
survives the thirty day purge of the decision it describes, because a thirty day dataset is not a
dataset, and `delete_tenant` still removes it, because that is a different promise.

The buttons now read **This was wrong** and **This was right** instead of "Be less/more strict",
because they no longer change strictness. The flag's footer carries the message id so a press can
find the decision that produced it; the id was already reachable through the jump link, so nothing
new is disclosed.

291 tests, `ruff` and `mypy` clean.

Still open: the export (T5, nothing to export yet) and the dashboard half, which belongs to the
private repository.
