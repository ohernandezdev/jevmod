"""How many pairs the comparison needs, worked out before anything is generated or judged.

`gate.py` scored three hand-written pairs. That is an anecdote and it is also the only estimate of
effect size that exists, so this takes it seriously and refuses to take it precisely.

**Why a range and not a number.** The between-scenario spread is estimated from three scenarios. A
standard deviation from n=3 is a famously bad number: its own 95% interval runs from about half the
estimate to about six times it. Quoting one N off it would be the same mistake as quoting a spam F1
off a set that was never labelled for spam. So every table below sweeps the spread instead of
assuming it, and the honest answer is "this many if the effect is as clean as the seed suggests,
this many if it is a third as clean".

**Why the sign test.** Distribution-free, exact, and the only thing it assumes is that a difference
has a direction. It is less powerful than a t-test, so every N here is an upper bound rather than an
optimistic one — the direction to be wrong in when deciding how much work to do. It also needs no
scipy, which `batch_effect.py` already refuses to add as a dependency for the same reason.

**Two different questions, two different answers, and mixing them is the usual way this goes wrong:**

1. *Does the window separate the arms at all?* A paired comparison of means. Cheap in scenarios,
   because the seed's effect is large relative to its spread.
2. *How often does it change what the product actually does?* A proportion — the fraction of
   scenarios where the closing line crosses its shipped threshold in A and not in B. Estimating a
   proportion to a useful width takes an order of magnitude more scenarios than detecting a mean
   difference, and it is the number a server owner would care about.

    python -m benchmark.conversations.power      # free, no API calls
"""

from __future__ import annotations

import random
import statistics
from math import comb

# From `gate.py`, three scenarios, three repeats per cell, 2026-09-24.
SEED_AB = [0.467, 0.267, 0.160]
SEED_AC = [0.253, 0.207, 0.147]

# Widest within-cell spread the gate saw across three repeats of one condition. The spread of three
# draws is about 1.7 standard deviations, so this is a generous reading of the measurement noise.
WITHIN_SPREAD = 0.050
WITHIN_SD = WITHIN_SPREAD / 1.7

# One request per judgement, measured: the gate did 36 requests for 80,559 input tokens at
# $0.042/M. Scenarios with context cost more than the alone condition, so this is the blend.
USD_PER_JUDGEMENT = 80_559 * 0.042 / 1e6 / 36

ALPHA = 0.05
TRIALS = 20_000


def sign_test_threshold(n: int) -> int:
    """The smallest k such that k of n differences pointing the same way is significant at ALPHA,
    two-sided. Exact binomial at p=0.5, written out for the reason `batch_effect.py` writes it out."""
    for k in range(n, n // 2, -1):
        p = 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2**n
        if p > ALPHA:
            return k + 1
    return n + 1


def power(n: int, mu: float, sd: float, rng: random.Random) -> float:
    """Simulated power of the paired sign test: draw n scenario-level differences, count how often
    enough of them point the same way."""
    need = sign_test_threshold(n)
    if need > n:
        return 0.0  # no outcome at this n can reach significance
    hits = 0
    for _ in range(TRIALS):
        pos = sum(1 for _ in range(n) if rng.gauss(mu, sd) > 0)
        if max(pos, n - pos) >= need:
            hits += 1
    return hits / TRIALS


def smallest_n(mu: float, sd: float, target: float, rng: random.Random, cap: int = 400) -> int | None:
    for n in range(4, cap + 1):
        if power(n, mu, sd, rng) >= target:
            return n
    return None


def main() -> None:
    rng = random.Random(20260924)
    mu_ab, sd_ab = statistics.mean(SEED_AB), statistics.stdev(SEED_AB)
    mu_ac, sd_ac = statistics.mean(SEED_AC), statistics.stdev(SEED_AC)

    print("## What the three seed pairs actually said\n")
    print("| comparison | mean difference | spread (n=3) | mean / spread |")
    print("|---|---|---|---|")
    print(f"| A against B | {mu_ab:+.3f} | {sd_ab:.3f} | {mu_ab / sd_ab:.1f} |")
    print(f"| A against C | {mu_ac:+.3f} | {sd_ac:.3f} | {mu_ac / sd_ac:.1f} |")
    print(f"\nMeasurement noise inside one cell is about {WITHIN_SD:.3f}, from the widest spread the gate saw")
    print(f"across three repeats. Between scenarios the spread is {sd_ab:.3f}, which is {sd_ab / WITHIN_SD:.0f} times larger.")
    print("\n**So repeats buy almost nothing and scenarios buy everything.** A scenario's mean over r")
    print("repeats has variance sd_between² + sd_within²/r; the second term is already "
          f"{WITHIN_SD**2 / sd_ab**2:.1%} of the first at")
    print("r=1. Judge each cell once and spend the budget on more scenarios. The gate keeps its three")
    print("repeats because its job is to show the same string scores the same, which is the one place")
    print("the within-cell number is the point.\n")

    print("## 1. Scenarios needed to show the arms separate at all\n")
    print("Paired sign test, two-sided, alpha 0.05. Each row assumes the effect stays as measured and")
    print("the spread is worse than measured, because a spread from three points is not a spread.\n")
    print("| if the between-scenario spread is | A against B, 80% | A against B, 90% | A against C, 80% | A against C, 90% |")
    print("|---|---|---|---|---|")
    for label, factor in (("as the seed measured", 1.0), ("half again as wide", 1.5),
                          ("twice as wide", 2.0), ("three times as wide", 3.0)):
        cells = []
        for mu, sd in ((mu_ab, sd_ab), (mu_ac, sd_ac)):
            for target in (0.80, 0.90):
                n = smallest_n(mu, sd * factor, target, rng)
                cells.append(str(n) if n else ">400")
        print(f"| {label} (×{factor:g}) | " + " | ".join(cells) + " |")

    print("\nThe sign test only counts directions, so these are upper bounds. A test that used the size")
    print("of each difference would need fewer.\n")

    print("## 2. Scenarios needed to say how often it changes the verdict\n")
    print("This is the product question rather than the statistical one: in what fraction of scenarios")
    print("does the closing line cross its shipped threshold in A and not in B? One of the three seed")
    print("scenarios did (harassment, 0.763 against a 0.75 line). One of three is not an estimate.\n")
    print("| to pin that fraction to | scenarios, if it is near 1 in 3 | if it is near 1 in 10 |")
    print("|---|---|---|")
    for width in (0.15, 0.10, 0.05, 0.03):
        row = []
        for p in (1 / 3, 0.10):
            n = (1.96**2) * p * (1 - p) / width**2
            row.append(f"{int(-(-n // 10) * 10)}")
        print(f"| ±{width:.0%} | " + " | ".join(row) + " |")

    print("\n## 3. What it costs, which turns out not to be the constraint\n")
    print("| scenarios | judgements | cost |")
    print("|---|---|---|")
    for n in (30, 60, 120, 240, 400):
        j = n * 4  # alone, A, B, C, judged once each
        print(f"| {n} | {j} | ${j * USD_PER_JUDGEMENT:.2f} |")

    print(f"\nAt ${USD_PER_JUDGEMENT:.5f} a judgement, even 400 scenarios is under a euro. **Money does not")
    print("decide this number.** What decides it is how many scenarios can be written whose closing line")
    print("is genuinely ambiguous alone and whose hard negative borrows the register of the positive")
    print("without borrowing its imagery — both rules the gate produced by breaking two of three.\n")

    n_ab = smallest_n(mu_ab, sd_ab * 2.0, 0.90, rng)
    n_ac = smallest_n(mu_ac, sd_ac * 2.0, 0.90, rng)
    print("## The recommendation\n")
    print(f"**Generate 120 scenarios.** Question 1 needs {n_ab} for A against B and {n_ac} for A against C at")
    print("90% power even if the spread is twice what three pairs suggested, so 120 has room for the")
    print("estimate to be wrong and for scenarios to be thrown away. Question 2 gets the changed-verdict")
    print("fraction to about ±8 points, which is enough to say 'roughly one in three' or 'roughly one in")
    print("ten' and not enough to say '31%'. Saying which of those it is would already settle whether a")
    print("41% cost increase is worth paying.")
    print("\nStop and re-read the numbers at 40. If A against C is not separating by then, the extra 80")
    print("scenarios are being spent to put a confidence interval around a feature that does not work.")


if __name__ == "__main__":
    main()
