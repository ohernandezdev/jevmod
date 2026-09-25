"""Formulation D asked through Judge, unpadded, no channel context, all nine categories, one request per 25 texts.

REPORT3 measured D as the only question in its request. A server that turns `ai_generated` on asks it beside
every other category it has enabled, in one request. REPORT2 showed that what shares a request moves the
score, so REPORT3's numbers are a premise until they are measured through `Judge`.

This script does that and nothing else. It loads the same 1047 texts REPORT3 used (`slop.load_items`),
shuffles them with REPORT3's seed so every batch of 25 holds the same texts `slop ask-mixed` grouped
together (about 25% AI), and hands each batch to `Judge.judge` with all nine categories. What reaches Jev:
no padding, `m0` the lead filler, topic "general chat", no conversation context. Production pads a batch
with the channel's recent messages, attaches the conversation before each message and sends the channel's
own topic; none of the three is measured here. The pre-filter
runs as it does in production, so a text under eight letters is not judged and cannot fire.

Two variants, so the change can be compared with what it replaces:

  d           `ai_generated` swapped in memory for formulation D, `slop.questions_d("{m}")`, which is what
              the run of 2026-09-25 had in `jevmod/categories.json`. D was not shipped, so the file on disk
              asks the authorship question and this swap is what keeps `d` reproducible
  d_retest    `d` asked a second time, unchanged, to see how much of a margin is noise
  authorship  `ai_generated` swapped for the authorship question, held below as `AUTHORSHIP`

    python -m benchmark.ai_detect.through_judge ask d            # paid, resumable per batch
    python -m benchmark.ai_detect.through_judge ask d_retest     # paid, the same request again
    python -m benchmark.ai_detect.through_judge ask authorship   # paid, resumable per batch
    python -m benchmark.ai_detect.through_judge report           # free, prints every table in REPORT4.md

Raw results go to `results/raw_judge_<variant>.jsonl`, one row per text with all nine scores, the batch it
was in and that batch's input tokens. The Judge cache is cleared between batches so every text is scored
in its own batch; production would serve a repeated text from cache, and the corpus has one repeat.

This imports `jevmod.judge` on purpose, which its siblings do not. For every variant it swaps one entry of
`jevmod.judge.CATEGORIES` in memory for the run and restores it; nothing on disk is written.
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .slop import load_items, questions_d

HERE = Path(__file__).parent
RESULTS = HERE / "results"
USD_PER_M = 0.042
BATCH = 25
SEED = 20260919  # REPORT3's `ask-mixed` seed: the same texts share a request here as there
THRESHOLD = 0.85  # DEFAULT_THRESHOLDS["ai_generated"]

# The question `ai_generated` asked before formulation D, verbatim from categories.json at commit f462cbb.
AUTHORSHIP = {
    "instructions": "Was `{m}.text` written by a language model (an AI assistant) rather than typed by a person in a "
    "chat, comment thread or forum?",
    "criteria": {
        "true": "it reads as generated assistant output: evenly hedged structure, complete and correct punctuation "
        "with no typos, generic politeness, list-like or 'there are several reasons' phrasing, a tidy summary "
        "sentence at the end, no slang, no in-jokes, an encyclopedic explaining register aimed at nobody in "
        "particular",
        "false": "it reads as a person typing: typos, missing apostrophes, lowercase starts, slang, abbreviations "
        "(lol, idk, imo), emotion or swearing, an unfinished thought, a reply to someone else, an in-joke or a "
        "reference to this channel/video/thread, an opinion stated bluntly with no hedging. Careful writing, "
        "correct grammar, a formal register or imperfect English from a non-native speaker are NOT by themselves "
        "signs of a language model; many people write carefully",
    },
}

# Written before the run, in odd/tasks/ai-slop-rule.md. (label, side filter, max fires or min rate, kind)
STRATA = {
    "real chat": lambda it: it["side"] == "chat",
    "non-native learners": lambda it: it["stratum"] == "nonnative_learner",
    "encyclopedia style": lambda it: it["stratum"] == "hc3_human_wiki_csai",
    "careful humans": lambda it: it["side"] == "careful",
    "assistant answers": lambda it: it["side"] == "ai",
}
CRITERIA = {
    "real chat": ("max_count", 0),
    "non-native learners": ("max_count", 3),
    "encyclopedia style": ("max_count", 3),
    "careful humans": ("max_rate", 0.03),
    "assistant answers": ("min_rate", 0.20),
}


def raw_path(variant: str) -> Path:
    return RESULTS / f"raw_judge_{variant}.jsonl"


@contextmanager
def variant_categories(variant: str) -> Iterator[None]:
    from jevmod import judge

    if variant in ("d", "d_retest"):
        d = questions_d("{m}")["d"]
        swap = {"instructions": d.instructions, "criteria": dict(d.criteria)}
    elif variant == "authorship":
        swap = AUTHORSHIP
    else:
        raise SystemExit(f"unknown variant {variant}")
    saved = judge.CATEGORIES["ai_generated"]
    judge.CATEGORIES["ai_generated"] = {**saved, **swap}
    try:
        yield
    finally:
        judge.CATEGORIES["ai_generated"] = saved


def batches() -> list[list[dict[str, Any]]]:
    items = load_items()
    random.Random(SEED).shuffle(items)
    return [items[i : i + BATCH] for i in range(0, len(items), BATCH)]


def ask(variant: str) -> None:
    from jevmod.judge import CATEGORIES, Judge, Message

    out = raw_path(variant)
    done = {json.loads(line)["batch"] for line in out.open(encoding="utf-8")} if out.exists() else set()
    groups = batches()
    print(f"[{variant}] {len(groups) - len(done)} of {len(groups)} batches to ask")
    judge = Judge(timeout_s=180.0)
    total = 0
    with variant_categories(variant), out.open("a", encoding="utf-8") as fh:
        cats = list(CATEGORIES)
        for b, chunk in enumerate(groups):
            if b in done:
                continue
            judge.cache.clear()
            before = judge.input_tokens
            t0 = time.time()
            verdicts = judge.judge([Message(id=it["id"], text=it["text"]) for it in chunk], cats)
            ms = (time.time() - t0) * 1000
            toks = judge.input_tokens - before
            total += toks
            rows = [
                {"id": v.message_id, "batch": b, "batch_tokens": toks, "judged": v.judged, "reason": v.reason,
                 "scores": v.scores}
                for v in verdicts
            ]
            fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            fh.flush()
            print(f"[{variant}] batch {b + 1}/{len(groups)}  {toks:,} tok  {ms:.0f} ms  "
                  f"running {total:,} tok  ${total * USD_PER_M / 1e6:.4f}")


def _load(variant: str) -> dict[str, dict[str, Any]]:
    path = raw_path(variant)
    return {r["id"]: r for r in map(json.loads, path.open(encoding="utf-8"))} if path.exists() else {}


def _cost(rows: dict[str, dict[str, Any]]) -> tuple[int, int]:
    per_batch = {r["batch"]: r["batch_tokens"] for r in rows.values()}
    return len(per_batch), sum(per_batch.values())


def _ai(row: dict[str, Any]) -> float:
    return row["scores"].get("ai_generated", 0.0)


def report() -> None:
    from jevmod.core.policy import DEFAULT_THRESHOLDS

    items = load_items()
    runs = {v: _load(v) for v in ("d", "d_retest", "authorship")}
    alone = {}
    if (RESULTS / "raw_D_mixed.jsonl").exists():
        alone = {r["id"]: r["p"] for r in map(json.loads, (RESULTS / "raw_D_mixed.jsonl").open(encoding="utf-8"))}

    for v, rows in runs.items():
        if not rows:
            continue
        n_b, toks = _cost(rows)
        skipped = [r for r in rows.values() if not r["judged"]]
        print(f"\n## `{v}` through Judge, all nine categories: {len(rows)} texts, {n_b} requests, "
              f"{toks:,} input tokens, ${toks * USD_PER_M / 1e6:.4f}")
        print(f"\n{len(skipped)} not judged by the pre-filter: "
              + ", ".join(f"{r['id']} ({r['reason']})" for r in skipped))

    d = runs["d"]
    if d:
        print(f"\n### Acceptance, `ai_generated` >= {THRESHOLD} through Judge\n")
        print("| set | n | fires | rate | must be | result | same request again | D alone (REPORT3) "
              "| authorship through Judge |")
        print("|---|---|---|---|---|---|---|---|---|")
        dash = "-"
        verdict = True
        for name, pick in STRATA.items():
            sub = [it for it in items if pick(it) and it["id"] in d]
            fires = sum(1 for it in sub if _ai(d[it["id"]]) >= THRESHOLD)
            rate = fires / len(sub)
            kind, bound = CRITERIA[name]
            ok = {"max_count": fires <= bound, "max_rate": rate <= bound, "min_rate": rate >= bound}[kind]
            verdict &= ok
            must = {"max_count": f"<= {bound}", "max_rate": f"<= {bound:.0%}", "min_rate": f">= {bound:.0%}"}[kind]
            a = sum(1 for it in sub if alone.get(it["id"], 0) >= THRESHOLD) if alone else None
            au = runs["authorship"]
            b = sum(1 for it in sub if it["id"] in au and _ai(au[it["id"]]) >= THRESHOLD) if au else None
            rt = runs["d_retest"]
            r = sum(1 for it in sub if it["id"] in rt and _ai(rt[it["id"]]) >= THRESHOLD) if rt else None
            print(f"| {name} | {len(sub)} | {fires} | {rate:.3f} | {must} | {'pass' if ok else 'FAIL'} "
                  f"| {dash if r is None else r} | {dash if a is None else a} | {dash if b is None else b} |")
        print(f"\n**{'Every row passes.' if verdict else 'At least one row fails.'}**")

        rt = runs["d_retest"]
        if rt:
            ids = [i for i in d if i in rt and d[i]["judged"]]
            flips = sorted(i for i in ids if (_ai(d[i]) >= THRESHOLD) != (_ai(rt[i]) >= THRESHOLD))
            drift = sum(abs(_ai(d[i]) - _ai(rt[i])) for i in ids) / len(ids)
            print(f"\nThe same request sent twice: mean abs difference {drift:.3f}, {len(flips)} of {len(ids)} "
                  f"texts on different sides of {THRESHOLD}: " + ", ".join(flips))

        if alone:
            print("\n### D through Judge against D alone, same texts, same batch membership\n")
            print("| side | n | mean p alone | mean p through Judge | drift | fires alone | fires through Judge |")
            print("|---|---|---|---|---|---|---|")
            for name in ("assistant answers", "careful humans", "encyclopedia style", "real chat"):
                sub = [it for it in items if STRATA[name](it) and it["id"] in d and it["id"] in alone]
                a = sum(alone[it["id"]] for it in sub) / len(sub)
                b = sum(_ai(d[it["id"]]) for it in sub) / len(sub)
                fa = sum(1 for it in sub if alone[it["id"]] >= THRESHOLD)
                fb = sum(1 for it in sub if _ai(d[it["id"]]) >= THRESHOLD)
                print(f"| {name} | {len(sub)} | {a:.3f} | {b:.3f} | {b - a:+.3f} | {fa} | {fb} |")
            both = [it for it in items if it["id"] in d and it["id"] in alone and d[it["id"]]["judged"]]
            flip = sum(1 for it in both if (alone[it["id"]] >= THRESHOLD) != (_ai(d[it["id"]]) >= THRESHOLD))
            print(f"\n{flip} of {len(both)} judged texts cross {THRESHOLD} between the two.")

        print(f"\n### Every human text that fires at {THRESHOLD}, to be read\n")
        humans = [it for it in items if it["side"] != "ai" and it["id"] in d and _ai(d[it["id"]]) >= THRESHOLD]
        for it in sorted(humans, key=lambda i: -_ai(d[i["id"]])):
            print(f"- {it['id']} ({it['stratum']}) {_ai(d[it['id']]):.2f}: {it['text'][:160]!r}")

    au = runs["authorship"]
    if d and au:
        print("\n### The other eight categories, D in the request against the authorship question in it\n")
        print("Same texts, same batches, same everything except the `ai_generated` question.\n")
        print("| category | threshold | mean abs diff | fires, authorship | fires, D | crossed |")
        print("|---|---|---|---|---|---|")
        ids = [i for i in d if i in au and d[i]["judged"] and au[i]["judged"]]
        for c in DEFAULT_THRESHOLDS:
            if c == "ai_generated":
                continue
            th = DEFAULT_THRESHOLDS[c]
            diff = sum(abs(d[i]["scores"][c] - au[i]["scores"][c]) for i in ids) / len(ids)
            fa = sum(1 for i in ids if au[i]["scores"][c] >= th)
            fd = sum(1 for i in ids if d[i]["scores"][c] >= th)
            cross = sum(1 for i in ids if (au[i]["scores"][c] >= th) != (d[i]["scores"][c] >= th))
            print(f"| {c} | {th} | {diff:.3f} | {fa} | {fd} | {cross} |")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "ask":
        ask(sys.argv[2] if len(sys.argv) > 2 else "d")
    elif cmd == "report":
        report()
    else:
        raise SystemExit(f"unknown command {cmd}")
