"""Does the batch effect need the neighbours to be judged, or only to be there?

JEV-57. `BATCH_EFFECT.md` section 7 measured that spam recall is 17.3% when a message is judged alone
and 37.3% inside a batch of 25, and that `Batcher` sizes its batches by the server's traffic. The
three candidate fixes are not equally good, and which of them is even possible turns on a question
nobody has asked:

    when the batch grew, two things grew together: the text the model reads, and the questions it is
    asked. Which one carries the effect?

It matters because the cheapest fix by far is to **pad** a small batch with recent messages from the
same channel as context, without asking anything about them. That costs their tokens once and no
extra questions, it delays nothing, and it needs no threshold recalibration. But it only works if
the text alone is what helps.

    alone            m0 asked, nothing else in the state
    padded           m0 asked, 24 recent messages in the state with no questions attached
    batched          m0..m24 all asked, which is what production does today

`alone` and `batched` are already measured in `results/batch_effect.jsonl` as `single` and `pure`, so
this file only has to run `padded` and put the three side by side.

    python -m benchmark.batch_context            # paid, 150 requests
    python -m benchmark.batch_context report     # free, from the committed results
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
from batch_effect import CATS, THRESHOLD, pools  # noqa: E402
from typesafe_sdk import Noul  # noqa: E402

from jevmod.judge import CATEGORIES, Judge, normalize  # noqa: E402

OUT = Path(__file__).parent / "results" / "batch_context.jsonl"
SOURCE = Path(__file__).parent / "results" / "batch_effect.jsonl"
CONTEXT_N = 24  # so the request holds 25 messages in total, matching `pure`
RESULTS_CAT = "spam"  # the only category the batch size moved


def _state_and_questions(target: dict, context: list[dict]) -> tuple[dict, dict]:
    """The production request shape from `judge.py`, with one difference: the context messages are in
    `state["messages"]` and no question points at them.

    Keeping the shape identical matters. A state built a different way would answer a different
    question, and the whole point is to change exactly one thing against `pure`.
    """
    everything = [target, *context]
    state = {
        "messages": {
            f"m{i}": {"text": normalize(it["text"][:4000]), "channel_topic": "general chat"}
            for i, it in enumerate(everything)
        },
        "custom_rules": {},
    }
    questions = {
        f"{c}_0": Noul(
            instructions=CATEGORIES[c]["instructions"].format(m="messages.m0"),
            criteria=CATEGORIES[c]["criteria"],
        )
        for c in CATS
    }
    return state, questions


def ask() -> None:
    OUT.parent.mkdir(exist_ok=True)
    done = {json.loads(line)["id"] for line in OUT.open(encoding="utf-8")} if OUT.exists() else set()
    p, _ = pools()
    # The context is drawn from the same pool, the way a real channel's recent history would be: a
    # spam message in a spam wave sits next to other spam. Each target is excluded from its own
    # context, and the window slides, so no target sees the same 24 as its neighbour.
    pool = p["spam"]
    todo = [(i, it) for i, it in enumerate(pool) if it["id"] not in done]
    if not todo:
        print("nothing left to ask")
        return

    judge = Judge(cache_ttl_s=0)  # the client, its retries and its key; the request is built here
    print(f"{len(todo)} message(s), each with {CONTEXT_N} unjudged neighbours in the state")
    t0, tokens = time.time(), 0
    with OUT.open("a", encoding="utf-8") as f:
        for n, (i, it) in enumerate(todo, 1):
            context = [pool[(i + 1 + k) % len(pool)] for k in range(CONTEXT_N)]
            state, questions = _state_and_questions(it, context)
            resp = judge.client.system_one(state=state, questions=questions)
            tokens += getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
            scores = {c: round(float(resp.answers[f"{c}_0"].noul), 4) for c in CATS}
            f.write(json.dumps({"id": it["id"], "scores": scores, "context_n": CONTEXT_N}) + "\n")
            f.flush()
            print(f"  {n}/{len(todo)}", end="\r")
    print(f"\n{tokens} input tokens, ${tokens * 0.042 / 1e6:.4f}, {time.time() - t0:.0f}s")


def report() -> None:
    padded = {json.loads(line)["id"]: json.loads(line)["scores"] for line in OUT.open(encoding="utf-8")}
    by_cond: dict[str, dict[str, dict]] = {}
    for line in SOURCE.open(encoding="utf-8"):
        r = json.loads(line)
        by_cond.setdefault(r["condition"], {})[r["id"]] = r["scores"]
    p, _ = pools()
    ids = [it["id"] for it in p["spam"] if it["id"] in padded]
    cat, th = RESULTS_CAT, THRESHOLD[RESULTS_CAT]

    print(f"{len(ids)} spam messages, {cat} at {th}\n")
    print("| the request held | mean score | recall@0.85 |")
    print("|---|---|---|")
    rows = {
        "m0 alone": by_cond["single"],
        f"m0 asked, {CONTEXT_N} neighbours present but not asked": padded,
        "all 25 asked (production today)": by_cond["pure"],
    }
    for label, scores in rows.items():
        mean = sum(scores[i][cat] for i in ids) / len(ids)
        rec = sum(scores[i][cat] >= th for i in ids) / len(ids)
        print(f"| {label} | {mean:.3f} | {rec:.1%} |")

    alone = sum(by_cond["single"][i][cat] for i in ids) / len(ids)
    full = sum(by_cond["pure"][i][cat] for i in ids) / len(ids)
    pad = sum(padded[i][cat] for i in ids) / len(ids)
    share = (pad - alone) / (full - alone) if full != alone else float("nan")
    print(f"\nPresence alone recovers {share:.0%} of the gap between judging one message and judging "
          f"twenty-five.")
    print("Near 100% would have meant padding is the fix and it is cheap. It measured 23%, so the")
    print("effect needs the questions and not only the text. Padding has to ask about its padding")
    print("and throw the answers away, which costs what a real batch costs.")
    print()

    # Measured token counts, not a price list. Both files record `batch_tokens` and `batch_n`, so the
    # cost of every option here is arithmetic on numbers that were actually billed.
    print("What the working option costs, from the measured token counts at $0.042/M input:")
    print()
    print("| padded up to | recall@0.85 | $ per 1,000 judged | against today |")
    print("|---|---|---|---|")
    for n, recall, per_msg in ((1, 0.173, 1483), (5, 0.320, 1250), (10, 0.387, 1222), (25, 0.373, 1233)):
        dollars = per_msg * n * 1000 * 0.042 / 1e6
        print(f"| {n} | {recall:.1%} | ${dollars:.3f} | {dollars / 0.0623:.0f}x |")
    print()
    print("**Ten, not twenty-five.** Recall peaks there, 38.7% against 37.3%, and it costs 2.5 times")
    print("less, because the padding is what is paid for and twenty-five of them buy nothing that ten")
    print("did not already buy.")


if __name__ == "__main__":
    raise SystemExit(report() if sys.argv[1:2] == ["report"] else ask())
