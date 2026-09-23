"""Position zero is judged alone no matter what else is in the request.

JEV-57 was opened on the finding that spam recall is 17.3% for a message judged by itself and 38.7%
in a request of ten. That framing is wrong, and the committed results in `results/batch_effect.jsonl`
already contained the correction; nobody had looked at it by position.

Reconstructing which index each message occupied in the batched conditions, and comparing each
message against its own score when judged alone:

    batch of 10, at index 0     n= 15    0.395 alone -> 0.409 batched    +0.014
    batch of 10, elsewhere      n=135    0.511 alone -> 0.735 batched    +0.224
    batch of 25, at index 0     n=  6    0.337 alone -> 0.387 batched    +0.050
    batch of 25, elsewhere      n=144    0.506 alone -> 0.733 batched    +0.227

**A message at `messages.m0` gains nothing from its neighbours.** Every other position gains about
0.22. A message judged by itself is always at m0, which is the whole of the "batch size" effect.

Held directly, changing nothing but the index, the same ten messages in the same request:

    index 0: 0.566    index 1: 0.729    index 4: 0.735    index 9: 0.762

And it is asymmetric, which is what makes it a defect rather than a calibration offset: on the same
ten-message request, spam positives at index 0 score 0.577 against 0.726 elsewhere, while clean
messages score 0.095 against 0.106. Index zero costs recall and buys no precision.

This file measures the fix. If a message must not sit at m0, the cheapest possible answer is to put
something harmless there and start the real messages at m1.

    python -m benchmark.position_zero          # paid, 300 requests, about $0.04
    python -m benchmark.position_zero report   # free
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

from batch_effect import CATS, THRESHOLD, pools  # noqa: E402
from typesafe_sdk import Noul  # noqa: E402

from jevmod.judge import CATEGORIES, Judge, normalize  # noqa: E402

OUT = Path(__file__).parent / "results" / "position_zero.jsonl"
SOURCE = Path(__file__).parent / "results" / "batch_effect.jsonl"
CAT = "spam"
PAD_TO = 10

# What sits at m0 so a real message does not have to. Deliberately ordinary: it has to be a sentence
# a moderator would never act on, in the register the channel is in, and short enough that paying
# for it is not the point of the exercise.
FILLER = "hey everyone, how is it going today"


def ask(judge: Judge, sequence: list[str], read_at: int) -> float:
    state = {
        "messages": {f"m{i}": {"text": normalize(t[:4000]), "channel_topic": "general chat"}
                     for i, t in enumerate(sequence)},
        "custom_rules": {},
    }
    questions = {
        f"{c}_{i}": Noul(instructions=CATEGORIES[c]["instructions"].format(m=f"messages.m{i}"),
                         criteria=CATEGORIES[c]["criteria"])
        for i in range(len(sequence)) for c in CATS
    }
    return float(judge.client.system_one(state=state, questions=questions).answers[f"{CAT}_{read_at}"].noul)


def run() -> None:
    """One request per message: the filler at m0, the message at m1, and the rest of the window
    behind it. That is exactly what the fix would send for a server quiet enough to deliver one
    message per batch, which is the case JEV-57 is about."""
    OUT.parent.mkdir(exist_ok=True)
    done = {json.loads(line)["id"] for line in OUT.open(encoding="utf-8")} if OUT.exists() else set()
    p, _ = pools()
    spam, clean = p["spam"], p["clean_vs_spam"]
    judge = Judge(cache_ttl_s=0)
    t0 = time.time()
    with OUT.open("a", encoding="utf-8") as f:
        for side, pool in (("spam", spam), ("clean", clean)):
            peers = pool
            todo = [it for it in pool if it["id"] not in done]
            for n, it in enumerate(todo, 1):
                tail = [o["text"] for o in peers if o["id"] != it["id"]][: PAD_TO - 2]
                score = ask(judge, [FILLER, it["text"], *tail], 1)
                f.write(json.dumps({"id": it["id"], "side": side, "score": round(score, 4)}) + "\n")
                if n % 25 == 0:
                    f.flush()
                    print(f"  {side} {n}/{len(todo)}", end="\r")
            f.flush()
    print(f"\n{judge.input_tokens} tokens, ${judge.input_tokens * 0.042 / 1e6:.4f}, {time.time() - t0:.0f}s")


def report() -> None:
    fixed = {json.loads(line)["id"]: json.loads(line)["score"] for line in OUT.open(encoding="utf-8")}
    by_cond: dict[str, dict[str, float]] = {}
    for line in SOURCE.open(encoding="utf-8"):
        r = json.loads(line)
        by_cond.setdefault(r["condition"], {})[r["id"]] = r["scores"][CAT]
    p, _ = pools()
    th = THRESHOLD[CAT]
    print(f"{CAT} at {th}. 'today' is one message per request, which is one message at m0.\n")
    print("| the request held | mean, spam | mean, clean | recall | FPR |")
    print("|---|---|---|---|---|")
    rows = [
        ("today: the message alone at m0", by_cond["single"], by_cond["single"]),
        ("a filler at m0, the message at m1, ten in all", fixed, fixed),
        ("a real batch of ten", by_cond["batch10"], by_cond["batch10"]),
    ]
    for label, s_pos, s_neg in rows:
        pos = [s_pos[i["id"]] for i in p["spam"] if i["id"] in s_pos]
        neg = [s_neg[i["id"]] for i in p["clean_vs_spam"] if i["id"] in s_neg]
        if not pos or not neg:
            print(f"| {label} | not run | | | |")
            continue
        print(f"| {label} | {statistics.mean(pos):.3f} | {statistics.mean(neg):.3f} | "
              f"{sum(v >= th for v in pos) / len(pos):.1%} | {sum(v >= th for v in neg) / len(neg):.1%} |")


if __name__ == "__main__":
    raise SystemExit(report() if sys.argv[1:2] == ["report"] else run())
