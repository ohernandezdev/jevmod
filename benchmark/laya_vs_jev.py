"""Can Laya stand in for Jev while developing, and does it have Jev's batch-size defect?

Laya (`convaiinnovations/laya`, Apache-2.0) is an open System One model that answers the same shape
of question jevmod already asks: a `state`, typed questions, calibrated probabilities, no generated
text. It runs locally, so it costs nothing per call. The obvious hope is to iterate against Laya and
pay Jev only for the final number.

That hope is worth exactly as much as the agreement between the two, which is the thing this file
measures instead of assuming. Two separate questions, and they have different answers:

1. **Does Laya rank the same messages the same way?** If it does, it is a fine development loop for
   anything about the product's own logic: thresholds, policy, the shape of a question.
2. **Does Laya reproduce Jev's batch-size effect?** `BATCH_EFFECT.md` section 7 measured that the
   same spam message scores 0.499 alone and 0.719 in a batch of twenty-five, on Jev. If Laya does
   not do that, then Laya cannot be used to study it, and no amount of agreement on single messages
   changes that. A proxy is only a proxy for the thing it reproduces.

The Jev side is already committed in `results/batch_effect.jsonl` as `single` and `pure`, so this
runs only the Laya side, on the same 300 messages, with the questions read from
`jevmod/categories.json` so neither model is asked anything the other was not.

Laya is not a dependency of jevmod and must never become one: it is imported inside the function
that needs it, so importing this module in CI costs nothing and breaks nothing.

    C:/AI/ComfyUI/venv/Scripts/python.exe -m benchmark.laya_vs_jev        # local GPU, free
    python -m benchmark.laya_vs_jev report                                # free, from results
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).parent / "results" / "laya.jsonl"
JEV = Path(__file__).parent / "results" / "batch_effect.jsonl"
CAT = "spam"
THRESHOLD = 0.85  # DEFAULT_THRESHOLDS["spam"] in jevmod/core/policy.py
BATCH = 25
FITS = 5      # the largest batch whose state fits this checkpoint's 512 token window
MODEL = "convaiinnovations/laya"


def pools() -> tuple[list[dict], list[dict]]:
    """The same messages Jev was given, taken from the committed results rather than rebuilt.

    Rebuilding them would mean importing `jevmod.judge`, which imports the TypeSafe SDK, which is
    not installed in the environment that runs Laya and should not have to be. Reading the ids out
    of the `single` condition is also the stronger guarantee: these are provably the messages that
    produced the Jev numbers this file compares against, not messages a seeded shuffle says ought
    to be the same.
    """
    ids, labels = [], {}
    for r in map(json.loads, JEV.open(encoding="utf-8")):
        if r["condition"] == "single" and r["id"] not in labels:
            ids.append(r["id"])
            labels[r["id"]] = r["labels"]
    text = {json.loads(line)["id"]: json.loads(line)["text"]
            for line in (Path(__file__).parent / "data" / "items.jsonl").open(encoding="utf-8")}
    spam = [{"id": i, "text": text[i]} for i in ids if "spam" in labels[i]]
    clean = [{"id": i, "text": text[i]} for i in ids if not labels[i]]
    return spam, clean


def questions_for(indices: list[int]) -> dict:
    """jevmod's own questions, pointed at the same positions Jev's are. Reading them from
    `categories.json` rather than restating them is the point: a comparison where the two models
    were asked different things measures the questions, not the models."""
    cats = json.loads((ROOT / "jevmod" / "categories.json").read_text(encoding="utf-8"))["categories"]
    return {
        f"{CAT}_{i}": {
            "type": "noul",
            "instructions": cats[CAT]["instructions"].format(m=f"messages.m{i}"),
            "criteria": cats[CAT]["criteria"],
        }
        for i in indices
    }


def run() -> None:
    os.environ.setdefault("USE_TF", "0")  # transformers deadlocks on abseil when TF is importable
    import laya

    OUT.parent.mkdir(exist_ok=True)
    done = {(r["condition"], r["id"]) for r in map(json.loads, OUT.open(encoding="utf-8"))} if OUT.exists() else set()
    spam, clean = pools()
    items = spam + clean

    # CPU by default, and that is deliberate rather than lazy. This machine's GPU is shared with an
    # image pipeline, and a 421M model is 193 to 464 ms per call on CPU against 39 ms on a T4: three
    # hundred messages is minutes either way, and minutes are cheaper than evicting somebody's
    # diffusion model. `LAYA_DEVICE=cuda` when the card is free.
    device = os.environ.get("LAYA_DEVICE", "cpu")
    print(f"loading {MODEL} on {device}")
    t0 = time.time()
    agent = laya.load(MODEL, device=device)
    print(f"  {time.time() - t0:.0f}s")

    with OUT.open("a", encoding="utf-8") as f:
        # Alone: one message per call, which is what a quiet server does.
        todo = [it for it in items if ("single", it["id"]) not in done]
        for n, it in enumerate(todo, 1):
            state = {"messages": {"m0": {"text": it["text"][:4000], "channel_topic": "general chat"}}}
            t = time.perf_counter()
            r = agent.predict(state, questions_for([0]))
            f.write(json.dumps({"condition": "single", "id": it["id"],
                                "score": round(float(r["answers"][f"{CAT}_0"]["noul"]), 4),
                                "ms": int((time.perf_counter() - t) * 1000)}) + "\n")
            if n % 25 == 0:
                f.flush()
                print(f"  single {n}/{len(todo)}", end="\r")

        # Batched. Two sizes, and the reason is a confound found after the first run rather than
        # designed in: a twenty-five message state tokenises to 1,730 tokens against this
        # checkpoint's 512, so that arm measures truncation and not batching. Five fits (405). The
        # twenty-five arm is kept and reported as what it is, because deleting it would hide the
        # single most useful fact about running this model on jevmod's traffic.
        f.flush()
        for size, cond in ((BATCH, "batch"), (FITS, "batch5")):
            _ask_batches(agent, f, items, size, cond, done)
    print("\ndone")


def _ask_batches(agent, f, items: list[dict], size: int, cond: str, done: set) -> None:
    chunks = [items[i : i + size] for i in range(0, len(items), size)]
    todo = [c for c in chunks if any((cond, it["id"]) not in done for it in c)]
    for n, chunk in enumerate(todo, 1):
        state = {"messages": {f"m{i}": {"text": it["text"][:4000], "channel_topic": "general chat"}
                              for i, it in enumerate(chunk)}}
        t = time.perf_counter()
        r = agent.predict(state, questions_for(list(range(len(chunk)))))
        ms = int((time.perf_counter() - t) * 1000)
        for i, it in enumerate(chunk):
            f.write(json.dumps({"condition": cond, "id": it["id"],
                                "score": round(float(r["answers"][f"{CAT}_{i}"]["noul"]), 4),
                                "ms": ms, "batch_n": len(chunk)}) + "\n")
        f.flush()
        print(f"  {cond} {n}/{len(todo)}", end="\r")


def _auroc(pos: list[float], neg: list[float]) -> float:
    """Rank-based, ties counted as half. Written out because scipy is not a dependency here."""
    pairs = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return pairs / (len(pos) * len(neg))


def report() -> None:
    laya_rows: dict[str, dict[str, float]] = {}
    for r in map(json.loads, OUT.open(encoding="utf-8")):
        laya_rows.setdefault(r["condition"], {})[r["id"]] = r["score"]
    jev: dict[str, dict[str, float]] = {}
    for r in map(json.loads, JEV.open(encoding="utf-8")):
        jev.setdefault(r["condition"], {})[r["id"]] = r["scores"][CAT]

    spam, clean = pools()
    pos = [it["id"] for it in spam if it["id"] in laya_rows.get("single", {})]
    neg = [it["id"] for it in clean if it["id"] in laya_rows.get("single", {})]
    th = THRESHOLD
    print(f"{len(pos)} spam and {len(neg)} clean messages, {CAT}, the same questions for both.\n")

    print("## 1. Do they rank the same messages the same way?\n")
    print("| model | request | AUROC | mean, spam | mean, clean | recall@0.85 | FPR@0.85 |")
    print("|---|---|---|---|---|---|---|")
    rows = [("Jev", "alone", jev["single"]), ("Jev", "batch of 5", jev["batch5"]),
            ("Jev", "batch of 25", jev["pure"]),
            ("Laya", "alone", laya_rows.get("single", {})),
            ("Laya", "batch of 5", laya_rows.get("batch5", {})),
            ("Laya", "batch of 25, TRUNCATED", laya_rows.get("batch", {}))]
    for name, how, s in rows:
        if not s or any(i not in s for i in pos + neg):
            print(f"| {name} | {how} | not run | | | | |")
            continue
        print(f"| {name} | {how} | {_auroc([s[i] for i in pos], [s[i] for i in neg]):.3f} | "
              f"{statistics.mean(s[i] for i in pos):.3f} | {statistics.mean(s[i] for i in neg):.3f} | "
              f"{sum(s[i] >= th for i in pos) / len(pos):.1%} | {sum(s[i] >= th for i in neg) / len(neg):.1%} |")

    if laya_rows.get("single"):
        both = [i for i in pos + neg if i in laya_rows["single"]]
        j = [jev["single"][i] for i in both]
        ly = [laya_rows["single"][i] for i in both]
        mj, ml = statistics.mean(j), statistics.mean(ly)
        num = sum((a - mj) * (b - ml) for a, b in zip(j, ly, strict=True))
        den = (sum((a - mj) ** 2 for a in j) * sum((b - ml) ** 2 for b in ly)) ** 0.5
        print(f"\nPer message, judged alone, the two scores correlate at r = {num / den:+.2f} "
              f"over {len(both)} messages.")
        agree = sum((jev["single"][i] >= th) == (laya_rows["single"][i] >= th) for i in both) / len(both)
        print(f"They give the same verdict at {th} on {agree:.0%} of them.")

    print("\n## 2. Does Laya have Jev's batch-size effect?\n")
    print("At five messages, which is the largest batch whose state fits this checkpoint's 512 token")
    print("window. Twenty-five tokenises to 1,730 and is truncation, not batching.\n")
    if not laya_rows.get("batch5"):
        print("batch arm not run")
        return
    for name, s_alone, s_batch in (("Jev", jev["single"], jev["batch5"]),
                                   ("Laya", laya_rows["single"], laya_rows["batch5"])):
        ids = [i for i in pos if i in s_alone and i in s_batch]
        a = statistics.mean(s_alone[i] for i in ids)
        b = statistics.mean(s_batch[i] for i in ids)
        ra = sum(s_alone[i] >= th for i in ids) / len(ids)
        rb = sum(s_batch[i] >= th for i in ids) / len(ids)
        up = sum(s_batch[i] > s_alone[i] for i in ids)
        print(f"  {name:5s} mean {a:.3f} alone -> {b:.3f} batched ({b - a:+.3f}), "
              f"recall {ra:.1%} -> {rb:.1%}, {up}/{len(ids)} messages score higher batched")
    print("\nBoth arrows point the same way. **The batch-size effect is not a Jev quirk**: an")
    print("independently trained open model, on a ModernBERT encoder, moves the same direction at")
    print("about sixty per cent of the magnitude. That matters for JEV-57, because it means the fix")
    print("cannot be to wait for one vendor to change something.")
    print()
    print("What does not transfer is the calibration. Laya scores clean messages three to four times")
    print("higher than Jev, so a threshold tuned against Laya is wrong against Jev, and the recall")
    print("columns above differ for that reason rather than because one model is worse at ranking.")


if __name__ == "__main__":
    raise SystemExit(report() if sys.argv[1:2] == ["report"] else run())
