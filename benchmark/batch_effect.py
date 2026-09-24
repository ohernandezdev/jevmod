"""Does a message's score depend on which other 24 messages share its request? Spam and harassment.

`benchmark/ai_detect/REPORT2.md` measured this for `ai_generated` and found it large enough to move
items across the shipping threshold. It then said, without measuring it, that the effect belongs to
the batching contract rather than to that one category, so it must apply to spam and harassment too.
Those are the two categories whose action can be `delete`. This file turns that expectation into a
number, in whichever direction the number goes. JEV-56.

Six conditions over the same messages, 25 per request, the production batching contract:

    pure        every message in the batch comes from the same side
    pure2       the identical batches, asked a second time
    reordered   the identical batches, the same 25 messages, shuffled within the batch
    reshuffled  the same side, regrouped: different neighbours, same composition
    mixed       the batch is about half the other side, interleaved
    mixed_shuffled  half the other side, and the membership randomised as hard as `reshuffled`
    single          one message per request, no batch at all
    single2         the same, asked again: the noise floor when there is nothing to share a batch with

Each one holds everything constant but one thing, so a flip can be attributed:

- `pure2` changes nothing at all. It is the noise floor.
- `reordered` changes only the position each message occupies. `Judge` keys its state and its
  questions by position (`messages.m3.text`), a shape adopted because lists leaked probabilities
  between neighbours, so position sensitivity is the first rival explanation and needs its own
  control. It also guards against the noise floor being flattered by a byte-identical request: if
  `pure2` is low only because the prompt repeats exactly, `reordered` is the honest noise floor.
- `reshuffled` changes which messages share the batch, holding the composition at all-one-side.
- `mixed_shuffled` changes the composition, at the same neighbour randomisation as `reshuffled`.
- `single` and `single2` remove the batch. They settle the last rival explanation: absolute movement
  correlates with each score's own `p(1-p)` at r = +0.60 to +0.68, *including inside `pure2` where
  nothing changed*, so "mid-range scores are intrinsically unstable" was not separated from
  "batching destabilises them". If a message asked entirely alone, twice, moves as much as it does
  between batches, batching is innocent and the score is simply soft in the middle. If it is as
  steady as `pure2`, batching is the cause. These two arms run on the spam pools only, which is
  where the effect reached significance.

**`mixed` is kept only because it is the arm the first version of this experiment had, and it is a
trap.** It interleaves the two pools in pool order, so every message keeps about half of its original
batch-mates: measured neighbour overlap with `pure` is 11.5 of 24, against 3.8 for `reshuffled`,
where 3.9 is what chance gives. On the axis that matters it is a *weaker* perturbation than
`reshuffled`, not a stronger one, so "mixed is no larger than reshuffled" says nothing whatever about
composition. That non sequitur was written into this file's first version and a red team caught it.
`mixed_shuffled` is the arm that actually isolates composition, because it differs from `reshuffled`
in composition alone.

The only sound composition comparison is `reshuffled` against `mixed_shuffled`. Everything else
measures batch identity.

One residual difference cannot be designed away, and is written here rather than left for a reader
to find: a 50/50 batch is necessarily drawn from a pool twice the size, so its chance-level
neighbour overlap is lower (1.93 of 24 against 3.87). Both arms sit on their own chance level,
measured at 1.90 and 3.79, so both have neighbours fully randomised. The remaining gap is a
consequence of changing the composition, not a confound left in by accident.

`pure2` is not optional. REPORT2 found 17 of 156 items crossing the threshold with nothing changed
at all, so any effect only exists if it is bigger than the noise floor measured beside it.

Both sides are measured. A clean message drifting up into `delete` and a spam message drifting down
out of it are different failures, and neither shows up in the other's number.

    python -m benchmark.batch_effect pools              # free: what the pools are, what was dropped
    python -m benchmark.batch_effect pilot              # paid, one batch per condition, prints cost
    python -m benchmark.batch_effect ask pure           # paid; and the same for pure2, reordered,
    python -m benchmark.batch_effect ask mixed_shuffled # reshuffled, mixed and mixed_shuffled
    python -m benchmark.batch_effect analyse            # free

Every paid command is resumable: results are appended per message and an id already present for that
condition is not asked again.
"""

from __future__ import annotations

import json
import random
import sys
import time
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevmod.core.policy import DEFAULT_ACTIONS  # noqa: E402
from jevmod.judge import CATEGORIES, Judge, Message, prefilter  # noqa: E402

DATA = Path(__file__).parent / "data"
OUT = Path(__file__).parent / "results" / "batch_effect.jsonl"

# The same question set `run_jevmod.py` asks. Asking only spam and harassment would be a different
# prompt from the one the product sends, and the number would not transfer to production.
# The categories that ship on, which is what a server is charged for. Excluding `offtopic` by
# name used to be the same thing and stopped being it when `ai_generated` arrived off by
# default: see the note in `run_jevmod.py`, where it put 26% on a published cost figure.
CATS = [c for c in CATEGORIES if DEFAULT_ACTIONS.get(c, "flag") != "off"]
BATCH = 25

# From DEFAULT_THRESHOLDS in jevmod/core/policy.py. These are the lines a drift has to cross for the
# drift to mean anything to a server owner.
THRESHOLD = {"spam": 0.85, "harassment": 0.75}

# Seeded so the run repeats. The pools and the batch order are a property of this number.
SEED = 56
RESHUFFLE_SEED = 560  # a different grouping of the same pools, not a different pool
REORDER_SEED = 5600   # a different order inside each batch, not a different batch
MIX_SEED = 5656       # randomises membership of the mixed arm to reshuffled's level
PER_POOL = 150
CONDITIONS = ("pure", "pure2", "reordered", "reshuffled", "mixed", "mixed_shuffled",
              "single", "single2", "batch5", "batch10",
              "single_h")


def _items() -> list[dict]:
    return [json.loads(line) for line in (DATA / "items.jsonl").open(encoding="utf-8")]


def pools() -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Four pools of 150, and what had to be thrown away to build them.

    Pre-filtered items are dropped here rather than discovered as holes in the results: under eight
    alphanumerics without a link never reaches Jev at all, and comes back `judged=False` with no
    scores. A message carrying both labels is dropped too, because it belongs to neither arm.

    The clean pool is split in two, so no clean message is reused between the spam arm and the
    harassment arm and the two results stay independent.
    """
    rng = random.Random(SEED)
    dropped = {"prefiltered": 0, "both_labels": 0}
    spam, harass, clean = [], [], []
    for it in _items():
        labels = set(it.get("labels") or [])
        if prefilter(Message(it["id"], it["text"])):
            dropped["prefiltered"] += 1
            continue
        if "spam" in labels and "harassment" in labels:
            dropped["both_labels"] += 1
            continue
        if "spam" in labels:
            spam.append(it)
        elif "harassment" in labels:
            harass.append(it)
        elif not labels:
            clean.append(it)
    for pool in (spam, harass, clean):
        rng.shuffle(pool)
    return {
        "spam": spam[:PER_POOL],
        "harassment": harass[:PER_POOL],
        "clean_vs_spam": clean[:PER_POOL],
        "clean_vs_harassment": clean[PER_POOL : PER_POOL * 2],
    }, dropped


def batches(condition: str) -> list[list[dict]]:
    """The batches for one condition, in the order they are asked.

    `pure` and `pure2` are the same batches; the point of the second is that nothing differs but the
    asking. `mixed` interleaves a positive pool with its own clean pool in pool order, which is the
    composition REPORT2 used and the trap described at the top of this file. `mixed_shuffled` is the
    same 50/50 composition with the membership randomised, and is the arm to read.
    """
    p, _ = pools()
    out: list[list[dict]] = []
    if condition in ("pure", "pure2", "reordered", "reshuffled"):
        rng = random.Random(RESHUFFLE_SEED)
        order = random.Random(REORDER_SEED)
        for name in ("spam", "harassment", "clean_vs_spam", "clean_vs_harassment"):
            pool = list(p[name])
            if condition == "reshuffled":
                rng.shuffle(pool)
            chunks = [pool[i : i + BATCH] for i in range(0, len(pool), BATCH)]
            if condition == "reordered":
                for chunk in chunks:
                    order.shuffle(chunk)
            out += chunks
        return out
    if condition.startswith("batch") and condition[5:].isdigit():
        # The same pools cut into a different batch size. `single` found the same messages score 0.22
        # higher on spam inside a batch of 25 than alone, which is a bias rather than a drift and is
        # larger than every flip effect in this file. `Batcher` uses a 2 second window, so a real
        # server's batch size is whatever its traffic delivers. These arms are the curve between.
        n = int(condition[5:])
        items = p["spam"] + p["clean_vs_spam"]
        return [items[i : i + n] for i in range(0, len(items), n)]
    if condition == "single_h":
        # The harassment arm of `single`, added because the site publishes a harassment recall figure
        # and the spam arms cannot speak for it: they scored harassment on spam and clean messages,
        # where it sits near zero and has nothing to move.
        return [[it] for it in p["harassment"] + p["clean_vs_harassment"]]
    if condition in ("single", "single2"):
        # One message per request. The spam arm only: 300 requests per run, against 12 for a batched
        # condition, so running all four pools would quadruple the wall clock to answer a question
        # that is only open for spam.
        return [[it] for it in p["spam"] + p["clean_vs_spam"]]
    if condition in ("mixed", "mixed_shuffled"):
        mix = random.Random(MIX_SEED)
        for pos, neg in (("spam", "clean_vs_spam"), ("harassment", "clean_vs_harassment")):
            merged = [x for pair in zip(p[pos], p[neg], strict=True) for x in pair]
            if condition == "mixed_shuffled":
                mix.shuffle(merged)
            out += [merged[i : i + BATCH] for i in range(0, len(merged), BATCH)]
        return out
    raise SystemExit(f"unknown condition {condition!r}; use one of {', '.join(CONDITIONS)}")


def _done() -> set[tuple[str, str]]:
    if not OUT.exists():
        return set()
    return {(json.loads(line)["condition"], json.loads(line)["id"]) for line in OUT.open(encoding="utf-8")}


def ask(condition: str, limit: int | None = None) -> None:
    """Score one condition. `limit` caps the number of batches, which is what `pilot` uses."""
    OUT.parent.mkdir(exist_ok=True)
    done = _done()
    # Drop ids already scored for this condition rather than re-asking a whole batch because one is
    # missing. Re-asking wrote a second row for the ids that were already there, and `_rows()` keeps
    # whichever came last without saying so, which would have silently mixed two batch compositions
    # into one column. It never fired (3,000 rows, 3,000 unique pairs) and it is fixed before it can.
    todo = [[it for it in b if (condition, it["id"]) not in done] for b in batches(condition)]
    todo = [b for b in todo if b]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        print(f"{condition}: nothing left to ask")
        return

    # cache_ttl_s=0 is the whole experiment. `Judge.cache` is keyed by normalised text plus topic plus
    # category set, so with the default 24 hours the second and third conditions would come back
    # `reason="cache"` with a drift of exactly zero. That looks like a clean result and is not one.
    judge = Judge(cache_ttl_s=0)
    print(f"{condition}: {len(todo)} batch(es) of up to {BATCH}")
    t0 = time.time()
    with OUT.open("a", encoding="utf-8") as f:
        for n, chunk in enumerate(todo, 1):
            tok0 = judge.input_tokens
            t1 = time.perf_counter()
            verdicts = judge.judge([Message(it["id"], it["text"][:4000]) for it in chunk], CATS)
            ms = int((time.perf_counter() - t1) * 1000)
            toks = judge.input_tokens - tok0
            for it, v in zip(chunk, verdicts, strict=True):
                if v.reason == "cache":
                    raise SystemExit("a cached verdict reached the results; the experiment is void")
                if not v.judged:
                    raise SystemExit(f"{it['id']} came back unjudged ({v.reason}); it should have been dropped")
                f.write(json.dumps({
                    "condition": condition, "id": it["id"], "labels": it.get("labels") or [],
                    "scores": v.scores, "batch_n": len(chunk), "batch_tokens": toks, "batch_ms": ms,
                }) + "\n")
            f.flush()
            print(f"  {n}/{len(todo)}  {toks} tok  {ms} ms", end="\r")
    spent = judge.input_tokens * 0.042 / 1e6
    print(f"\n{condition}: {judge.judged_messages} judged, {judge.input_tokens} tokens, ${spent:.4f}, "
          f"{time.time() - t0:.0f}s")


def _rows() -> dict[str, dict[str, dict[str, float]]]:
    """{condition: {id: scores}}"""
    out: dict[str, dict[str, dict[str, float]]] = {c: {} for c in CONDITIONS}
    if not OUT.exists():
        return out
    for line in OUT.open(encoding="utf-8"):
        r = json.loads(line)
        out.setdefault(r["condition"], {})[r["id"]] = r["scores"]
    return out


def _flip(rows: dict, cond: str, cat: str, th: float, ids: list[str]) -> list[bool]:
    return [(rows["pure"][i][cat] >= th) != (rows[cond][i][cat] >= th) for i in ids]


def _binom(k: int, n: int) -> float:
    """Two-sided exact binomial at p=0.5. Written out rather than imported: scipy is not a dependency
    of this package and a benchmark must not add one."""
    if n == 0:
        return 1.0
    probs = [comb(n, i) * 0.5 ** n for i in range(n + 1)]
    return min(1.0, sum(x for x in probs if x <= probs[k] + 1e-12))


def analyse() -> None:
    """The tables. Per side and per category, never pooled: REPORT2's ALL row drifted +0.001 because
    opposite signs cancelled while the false-positive rate for one stratum more than doubled."""
    rows = _rows()
    p, _ = pools()
    sides = {
        ("spam", "positive"): p["spam"],
        ("spam", "clean"): p["clean_vs_spam"],
        ("harassment", "positive"): p["harassment"],
        ("harassment", "clean"): p["clean_vs_harassment"],
    }
    have = [c for c in CONDITIONS if rows.get(c)]
    if "pure" not in have:
        print("nothing to analyse yet: run `ask pure` first")
        return

    # The means are printed first and mean the least. They are here because REPORT2's ALL row is the
    # standing warning about reading them: it drifted +0.001 while one stratum's false-positive rate
    # went from 0.300 to 0.667 underneath it.
    print("Mean probability per condition. Read the flip tables below instead; these cancel.")
    print()
    print("| category | side | n | " + " | ".join(f"mean {c}" for c in have) + " |")
    print("|---" * (3 + len(have)) + "|")
    for (cat, side), pool in sides.items():
        ids = [i for i in (it["id"] for it in pool) if all(i in rows.get(c, {}) for c in have)]
        if not ids:
            continue
        m = {c: sum(rows[c][i][cat] for i in ids) / len(ids) for c in have}
        print(f"| {cat} | {side} | {len(ids)} | " + " | ".join(f"{m[c]:.3f}" for c in have) + " |")

    print()
    print("Messages crossing their shipping threshold, counted against `pure`. Counts alone decide")
    print("nothing at n=150; the significance table below is the one to read.")
    print()
    print("| category | side | n | threshold | " + " | ".join(c for c in have if c != "pure") + " |")
    print("|---" * (4 + len(have) - 1) + "|")
    for (cat, side), pool in sides.items():
        th = THRESHOLD[cat]
        ids = [it["id"] for it in pool if all(it["id"] in rows.get(c, {}) for c in have)]
        if not ids:
            continue
        cells = [f"{sum(_flip(rows, c, cat, th, ids)):d}" for c in have if c != "pure"]
        print(f"| {cat} | {side} | {len(ids)} | {th} | " + " | ".join(cells) + " |")

    print()
    print("McNemar, exact binomial on discordant pairs, each manipulation against the `pure2` floor,")
    print(f"Holm-corrected over the {len([1 for _ in sides for c in have if c not in ('pure', 'pure2')])} "
          "tests. A count that does not survive Holm is not a finding.")
    print()
    tests = []
    for (cat, side), pool in sides.items():
        th = THRESHOLD[cat]
        ids = [it["id"] for it in pool if all(it["id"] in rows.get(c, {}) for c in have)]
        base = _flip(rows, "pure2", cat, th, ids) if "pure2" in have else None
        if base is None:
            continue
        for c in have:
            if c in ("pure", "pure2"):
                continue
            man = _flip(rows, c, cat, th, ids)
            b = sum(1 for x, y in zip(man, base, strict=True) if x and not y)
            d = sum(1 for x, y in zip(man, base, strict=True) if y and not x)
            tests.append((f"{cat}/{side}/{c}", sum(man), sum(base), _binom(b, b + d)))
    m = len(tests)
    prev = 0.0
    print("| comparison | flips vs floor | raw p | Holm p | |")
    print("|---|---|---|---|---|")
    for k, (name, flips, floor, raw) in enumerate(sorted(tests, key=lambda t: t[3])):
        prev = min(1.0, max(prev, (m - k) * raw))
        print(f"| {name} | {flips} vs {floor} | {raw:.4f} | {prev:.3f} | "
              f"{'**survives**' if prev < 0.05 else 'does not'} |")

    # The composition test, which is the only comparison in this file that is about composition. It
    # is `reshuffled` against `mixed_shuffled` and nothing else: those two arms differ in composition
    # and have their neighbours randomised to chance in both directions. A `mixed` column next to a
    # `reshuffled` column is not a composition test, because `mixed` keeps half its neighbours.
    if "reshuffled" in have and "mixed_shuffled" in have:
        print()
        print("Composition, isolated: `reshuffled` against `mixed_shuffled`, both with neighbours at")
        print("chance. This is the only comparison here that is about what the neighbours are.")
        print()
        print("| category | side | reshuffled | mixed_shuffled | b/c | p |")
        print("|---|---|---|---|---|---|")
        for (cat, side), pool in sides.items():
            th = THRESHOLD[cat]
            ids = [it["id"] for it in pool if all(it["id"] in rows.get(c, {}) for c in have)]
            a = _flip(rows, "reshuffled", cat, th, ids)
            b = _flip(rows, "mixed_shuffled", cat, th, ids)
            x = sum(1 for u, v in zip(b, a, strict=True) if u and not v)
            y = sum(1 for u, v in zip(b, a, strict=True) if v and not u)
            print(f"| {cat} | {side} | {sum(a)} | {sum(b)} | {x}/{y} | {_binom(x, x + y):.3f} |")

    # Deliberately not printed: a lost-against-gained table. The first version of this file had one
    # and it read as a finding. It is not: no asymmetry in any cell reaches significance, and the
    # apparent direction follows from there being more mass just above each threshold than just
    # below it. See BATCH_EFFECT.md section 5, where the claim is withdrawn.


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "pools"
    if cmd == "pools":
        p, dropped = pools()
        for name, pool in p.items():
            print(f"{name:22s} {len(pool)}")
        print(f"dropped: {dropped}")
        for c in CONDITIONS:
            print(f"{c:6s} {len(batches(c))} batches")
    elif cmd == "pilot":
        for c in CONDITIONS:
            ask(c, limit=1)
    elif cmd == "ask":
        if len(argv) < 2:
            return int(bool(print(f"ask which? one of {', '.join(CONDITIONS)}")))
        ask(argv[1])
    elif cmd == "analyse":
        analyse()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
