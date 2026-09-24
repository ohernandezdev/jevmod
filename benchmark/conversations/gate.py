"""The cheapest possible falsification of the whole plan, run before anything is generated.

`seed.jsonl` holds three scenarios written by hand. Each has one closing line and three lead-ups:

  * **A**, where four people have been turning on a fifth, and the line lands on the isolated one.
  * **B**, where two friends have been ribbing each other, and the *same line* is part of the joke.
  * **C**, where everyone is being equally harsh to everyone, nobody is isolated, same line again.

The line is byte-identical across all three. That is the point of the design: whatever cliché a
generator reaches for goes into the lead-up, which is what varies, and never into the string being
scored. So a difference in score cannot be a difference in the message.

Three things this prints, in the order they can kill the plan:

1. **The gate.** The line scored alone must be one number, not three. It is the same string, so any
   spread here is the model's own run-to-run noise and it bounds everything below. If that spread is
   not small, nothing downstream means anything and this says so.
2. **A against B.** Does the window do anything at all? If these do not separate, the feature does
   not do the thing the product is sold on, and 500 generated pairs will not change that.
3. **A against C.** Does it do the *right* thing? C has A's vocabulary and B's innocence. A window
   that separates A from B but not A from C is detecting rudeness in the neighbourhood rather than
   working out who is doing what to whom. This is the one that can fail while 2 passes.

The reference for "small" is the repeat-run floor measured in JEV-60: asking Jev the same question
twice moves a category's mean score by 0.0001 to 0.0003 and flips 0.9% of flagged-at-all verdicts.

    python -m benchmark.conversations.gate           # paid, about $0.01
    python -m benchmark.conversations.gate --repeats 3

Costs cents because it is twelve judgements. That is the whole argument for running it first.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jevmod.core.policy import DEFAULT_ACTIONS, DEFAULT_THRESHOLDS  # noqa: E402
from jevmod.judge import CATEGORIES, Judge, Message  # noqa: E402

SEED = Path(__file__).parent / "seed.jsonl"
CATS = [c for c in CATEGORIES if DEFAULT_ACTIONS.get(c, "flag") != "off"]
ARMS = ("A", "B", "C")
ARM_NAME = {"A": "targeted", "B": "friendly", "C": "reciprocal"}


def scenarios() -> list[dict]:
    return [json.loads(line) for line in SEED.open(encoding="utf-8") if line.strip()]


def score(judge: Judge, text: str, context: tuple[str, ...], topic: str) -> dict[str, float]:
    """One message, one request, judged the same way in every condition.

    One per request rather than four in one, deliberately. The four conditions share a text, and
    `BATCH_EFFECT.md` measured that batch membership moves scores by more than the effect being
    looked for here; putting the same string in a request four times would also give the model the
    repetition itself as a hint. Same shape every time is worth more than the saved requests.
    """
    m = Message("x", text, channel_topic=topic, context=context)
    return judge.judge([m], CATS)[0].scores


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=1,
                    help="score every condition this many times and report the mean; 2 or more also "
                         "measures this harness's own noise instead of borrowing JEV-60's")
    args = ap.parse_args()

    # The cache is keyed on the context, so the four conditions would not collide anyway. Off
    # regardless: an experiment that silently answers from a cache is not an experiment, and
    # `--repeats` would measure nothing at all with it on.
    judge = Judge(cache_ttl_s=0)
    rows = []
    for sc in scenarios():
        cat, topic = sc["category"], sc["topic"]
        closing = sc["closing"]["text"]
        conds: dict[str, list[float]] = {}
        for name, ctx in [("alone", ())] + [(a, tuple(m["text"] for m in sc["arms"][a])) for a in ARMS]:
            conds[name] = [score(judge, closing, ctx, topic)[cat] for _ in range(args.repeats)]
        rows.append((sc["id"], cat, {k: statistics.mean(v) for k, v in conds.items()}, conds))

    print(f"Scored with `{'`, `'.join(CATS)}` enabled; the column is each scenario's own category.")
    print(f"{args.repeats} repeat(s) per condition. Threshold shown is the shipped one.\n")

    print("## 1. The gate: the same string, scored alone\n")
    print("| scenario | category | alone | shipped threshold |")
    print("|---|---|---|---|")
    for sid, cat, mean, raw in rows:
        spread = f" (spread {max(raw['alone']) - min(raw['alone']):+.3f})" if args.repeats > 1 else ""
        print(f"| {sid} | {cat} | {mean['alone']:.3f}{spread} | {DEFAULT_THRESHOLDS[cat]} |")
    print("\nThese are the baselines the three arms are measured against, one per scenario. They are")
    print("not compared to each other: different scenarios have different closing lines.\n")

    print("## 2 and 3. The same string, in each of its three conversations\n")
    print("| scenario | alone | A targeted | B friendly | C reciprocal | A−B | A−C |")
    print("|---|---|---|---|---|---|---|")
    for sid, _cat, mean, _raw in rows:
        a, b, c = mean["A"], mean["B"], mean["C"]
        print(f"| {sid} | {mean['alone']:.3f} | {a:.3f} | {b:.3f} | {c:.3f} | {a - b:+.3f} | {a - c:+.3f} |")

    print("\nWhat the two right-hand columns have to be for the plan to be worth continuing:")
    print("  A−B  well clear of zero, or the window does nothing.")
    print("  A−C  well clear of zero, or the window is reacting to harsh words nearby rather than")
    print("       to somebody being singled out, which is not the claim the product makes.")
    print("\nThree scenarios is an anecdote, not a measurement. It is here to kill the plan cheaply")
    print("if it deserves killing, not to settle anything.")
    print(f"\n{judge.requests} requests, {judge.input_tokens:,} input tokens, "
          f"${judge.input_tokens * 0.042 / 1e6:.4f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
