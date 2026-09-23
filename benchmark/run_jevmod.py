"""jevmod over items.jsonl: every category, batches of 25, one Jev request per batch. Writes a results file
with the raw probabilities and the token count, so cost is measured, not estimated. Resumable.

    python -m benchmark.run_jevmod                   # -> results/jevmod.jsonl
    python -m benchmark.run_jevmod --out jevmod_v2   # -> results/jevmod_v2.jsonl

**The name is an argument because the engine changed under the published figures.** `results/jevmod.jsonl`
was measured on 2026-09-18, before `judge()` started putting a filler at `messages.m0`. That run therefore
had one message in every twenty-five silently judged in isolation, and it used a batch of 25, which
`BATCH_EFFECT.md` section 7 later showed is the favourable end of a curve: spam recall is 17.3% at one
message per request and 38.7% at ten. Re-running under a different name and diffing the two with
`python -m benchmark.evaluate jevmod jevmod_v2` is how that gets settled rather than argued about.
Overwriting the old file instead would destroy the only copy of the measurement the published claims
actually came from.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevmod.core.policy import DEFAULT_ACTIONS  # noqa: E402
from jevmod.judge import CATEGORIES, Judge, Message  # noqa: E402

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"

# The categories that ship on, which is what a server is actually charged for. This was
# `[c for c in CATEGORIES if c != "offtopic"]`, one name excluded by hand, and it broke the moment a
# second off-by-default category arrived: `ai_generated` was added after the published run and got
# scored anyway, which put 26% on the measured cost per thousand messages that no user pays and swept
# a category nobody enabled into the report's "flagged at all" column. Reading the actions means the
# next category added is handled by whatever its own default says.
CATS = [c for c in CATEGORIES if DEFAULT_ACTIONS.get(c, "flag") != "off"]
BATCH = 25


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="jevmod", help="results file stem under results/ (default: jevmod)")
    args = ap.parse_args()
    OUT = RESULTS / f"{args.out}.jsonl"

    OUT.parent.mkdir(exist_ok=True)
    done = {json.loads(l)["id"] for l in OUT.open(encoding="utf-8")} if OUT.exists() else set()
    items = [json.loads(l) for l in (DATA / "items.jsonl").open(encoding="utf-8")]
    todo = [it for it in items if it["id"] not in done]
    print(f"{len(todo)} to judge ({len(done)} done)")
    j = Judge()
    t0 = time.time()
    with OUT.open("a", encoding="utf-8") as f:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i : i + BATCH]
            msgs = [Message(it["id"], it["text"][:4000]) for it in chunk]
            tok0, t1 = j.input_tokens, time.perf_counter()
            verdicts = j.judge(msgs, CATS)
            ms = int((time.perf_counter() - t1) * 1000)
            toks = j.input_tokens - tok0
            for it, v in zip(chunk, verdicts, strict=True):
                f.write(
                    json.dumps(
                        {
                            "id": it["id"],
                            "judged": v.judged,
                            "reason": v.reason,
                            "scores": v.scores,
                            "batch_tokens": toks,
                            "batch_ms": ms,
                            "batch_n": len(chunk),
                        }
                    )
                    + "\n"
                )
            f.flush()
            print(f"{i + len(chunk)}/{len(todo)}  {toks} tok  {ms} ms", end="\r")
    print(
        f"\n{j.judged_messages} judged, {j.input_tokens} tokens, ${j.input_tokens * 0.042 / 1e6:.4f}, {time.time() - t0:.0f}s"
    )


if __name__ == "__main__":
    main()
