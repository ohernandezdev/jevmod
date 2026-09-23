"""Where does the reaction feedback loop settle, and is one reaction bigger than the noise?

`discord_bot.py` lets a moderator react to a log entry: ❌ raises that category's threshold by 0.03,
✅ lowers it by 0.02. Nothing has ever measured what that loop does over time. Two things can be
asked without spending anything, because `benchmark/results/batch_effect.jsonl` already holds 150
real spam messages and 150 real clean ones scored five times each under different batching.

**1. The fixed point is arithmetic, not a measurement.** At equilibrium the ups and downs cancel:

    0.03 * (false positives) = 0.02 * (true positives)

so FP/TP = 2/3, and precision = TP/(TP+FP) = 0.6. The loop aims at the line where two flags in five
are wrong, a number set by the ratio of two constants nobody measured and the same for a support
forum and a meme channel.

**That equilibrium only exists if the category's precision actually passes through 0.6 somewhere
between the clamps at 0.50 and 0.99.** If precision is below 0.6 across the whole range, which is
what a low spam rate does to it, there is nothing for the loop to settle on and it ratchets to the
ceiling. At 0.99 it stops flagging, so it stops receiving reactions, so it stays there. The
category is off and nobody turned it off. The table below is measured, and that is what it shows.

**2. One reaction is smaller than the noise on the message that caused it.** 65% of spam messages
move more than 0.03 when the batch around them changes (`BATCH_EFFECT.md`). A moderator reacting to
one message is reacting to a number that would have been different had the message arrived a second
earlier, and the correction applied is smaller than that difference.

The simulation below does not invent scores. Each message's score is drawn from the five real
observations of that same message, so the variation is the variation that was measured.

    python -m benchmark.nudge_loop                 # free, reads the committed results
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevmod.core.policy import DEFAULT_THRESHOLDS, Policy  # noqa: E402

RESULTS = Path(__file__).parent / "results" / "batch_effect.jsonl"
CAT = "spam"
UP, DOWN = 0.03, -0.02  # discord_bot.py:358
SEED = 56


def observations() -> tuple[list[list[float]], list[list[float]]]:
    """Per message, the five scores it actually received. Positives and clean, kept apart."""
    sys.path.insert(0, str(Path(__file__).parent))
    from batch_effect import pools

    rows: dict[str, list[float]] = {}
    for line in RESULTS.open(encoding="utf-8"):
        r = json.loads(line)
        rows.setdefault(r["id"], []).append(r["scores"][CAT])
    p, _ = pools()
    pos = [rows[it["id"]] for it in p["spam"] if it["id"] in rows]
    clean = [rows[it["id"]] for it in p["clean_vs_spam"] if it["id"] in rows]
    return pos, clean


def simulate(pos: list[list[float]], clean: list[list[float]], base_rate: float, n: int = 4000,
             seed: int = SEED) -> tuple[float, float, float, list[float]]:
    """Stream n messages past the loop. Returns the final threshold, and the precision and recall
    over the last quarter, once it has settled.

    The moderator is idealised on purpose: they react to every flag, and they are always right about
    whether it was spam. That is the most favourable case for the mechanism. A real moderator reacts
    to some flags and is sometimes wrong, which can only make this worse.
    """
    rng = random.Random(seed)
    policy = Policy()
    th = policy.thresholds[CAT]
    track, tp, fp, fn = [], 0, 0, 0
    for i in range(n):
        spam = rng.random() < base_rate
        score = rng.choice(rng.choice(pos if spam else clean))
        flagged = score >= th
        if i >= n * 0.75:
            tp += flagged and spam
            fp += flagged and not spam
            fn += (not flagged) and spam
        if flagged:
            th = policy.nudge(CAT, DOWN if spam else UP)
        track.append(th)
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    return th, precision, recall, track


def main() -> int:
    pos, clean = observations()
    print(f"{len(pos)} spam and {len(clean)} clean messages, five real observations each.")
    print(f"Shipped threshold {DEFAULT_THRESHOLDS[CAT]}, reactions {UP:+} and {DOWN:+}, "
          f"so the loop aims at precision {UP / (UP + abs(DOWN)):.2f}.\n")

    print("| spam rate in the channel | final line | precision | recall |")
    print("|---|---|---|---|")
    finals = []
    for rate in (0.02, 0.05, 0.10, 0.25, 0.50):
        th, pr, rc, _ = simulate(pos, clean, rate)
        finals.append(pr)
        print(f"| {rate:.0%} | {th:.2f} | {pr:.2f} | {rc:.2f} |")
    print(f"\nprecision across those five: {min(finals):.2f} to {max(finals):.2f}, "
          f"against the {UP / (UP + abs(DOWN)):.2f} it aims at. A `nan` means the category stopped "
          "flagging entirely.")

    print("\nWhere is the equilibrium, if there is one? Precision at each threshold, on the same")
    print("messages, against the 0.60 the loop aims at:")
    print()
    print("| spam rate | " + " | ".join(f"P@{t:.2f}" for t in (0.50, 0.70, 0.85, 0.95, 0.99))
          + " | crosses 0.60? |")
    print("|---|" + "---|" * 6)
    for rate in (0.02, 0.05, 0.10, 0.25, 0.50):
        cells, crosses = [], False
        for t in (0.50, 0.70, 0.85, 0.95, 0.99):
            tp = sum(1 for o in pos for v in o if v >= t) * rate / len(pos)
            fp = sum(1 for o in clean for v in o if v >= t) * (1 - rate) / len(clean)
            cells.append("none" if tp + fp == 0 else f"{tp / (tp + fp):.2f}")
            crosses = crosses or (tp + fp > 0 and tp / (tp + fp) >= 0.60)
        print(f"| {rate:.0%} | " + " | ".join(cells) + f" | {'yes' if crosses else '**no**'} |")
    print("\nA row that never crosses 0.60 has no equilibrium to find. The loop ratchets to a clamp,")
    print("and at 0.99 it stops flagging, so it stops being corrected, so it stays there.")

    print("\nDoes the line stay put once it gets there? Ten seeds at a 5% spam rate, "
          "the last thousand messages:")
    spread = []
    for s in range(10):
        _, _, _, track = simulate(pos, clean, 0.05, seed=SEED + s)
        tail = track[-1000:]
        spread.append(max(tail) - min(tail))
    print(f"  the threshold swings {statistics.mean(spread):.2f} on average, "
          f"worst {max(spread):.2f}, while one reaction moves it {UP:.2f}")

    print("\nHow much of one reaction is smaller than the message that caused it:")
    moves = [max(o) - min(o) for o in pos]
    bigger = sum(1 for m in moves if m > UP) / len(moves)
    print(f"  the same spam message spans {statistics.median(moves):.2f} across its five batches "
          f"(median); {bigger:.0%} span more than the {UP:.2f} one reaction applies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
