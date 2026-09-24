"""A against B: the same 2,531 messages with the context engine off and on.

**This is not the experiment JEV-5 asks for, and the difference matters.** JEV-5 wants A (message
alone) against B (message plus conversation). `data/items.jsonl` has no conversation in it: four
fields, `id`, `source`, `labels`, `text`, and 2,531 rows that are single prompts, isolated comments
and unthreaded YouTube spam. JEV-11 already wrote that down. Feeding a model the neighbouring rows
and calling the result "with conversation" would put a name on a number that has not earned it.

What this does measure is real and was missing: **the context engine shipped in JEV-17 is on by
default and nobody has seen what it does to the labelled set.** B fills `Message.context` the way
`ModerationService` does, from the preceding rows of the same source, which is a plausible set of
neighbours rather than a conversation. The question it answers is "how much does turning this on
move the verdicts", which is the risk of what already shipped.

The question it does not answer is whether conversation improves judgement. That needs labelled
conversations, which is JEV-11 and JEV-7, and is the same dependency JEV-56's harassment half hit.

    python -m benchmark.run_context_ab off      # paid
    python -m benchmark.run_context_ab on       # paid
    python -m benchmark.evaluate context_off context_on      # free, the delta
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevmod.core.context import WINDOW, assemble  # noqa: E402
from jevmod.core.policy import DEFAULT_ACTIONS  # noqa: E402
from jevmod.judge import CATEGORIES, Judge, Message  # noqa: E402

DATA = Path(__file__).parent / "data" / "items.jsonl"
# The categories that ship on, which is what a server is charged for. Excluding `offtopic` by
# name used to be the same thing and stopped being it when `ai_generated` arrived off by
# default: see the note in `run_jevmod.py`, where it put 26% on a published cost figure.
CATS = [c for c in CATEGORIES if DEFAULT_ACTIONS.get(c, "flag") != "off"]
BATCH = 25


def items() -> list[dict]:
    return [json.loads(line) for line in DATA.open(encoding="utf-8")]


def with_context(rows: list[dict]) -> dict[str, tuple[str, ...]]:
    """The preceding rows of the same source, newest last, trimmed by the same assembler production
    uses. Source is the only grouping the file has; it is not a channel and is not a thread."""
    window: dict[str, list[str]] = {}
    out: dict[str, tuple[str, ...]] = {}
    for r in rows:
        prev = window.setdefault(r["source"], [])
        out[r["id"]] = assemble(tuple(prev[-WINDOW:]))
        prev.append(r["text"])
    return out


def main(argv: list[str]) -> int:
    arm = (argv[0] if argv else "").lower()
    if arm not in ("off", "on"):
        print(__doc__)
        return 2
    out = Path(__file__).parent / "results" / f"context_{arm}.jsonl"
    out.parent.mkdir(exist_ok=True)
    done = {json.loads(line)["id"] for line in out.open(encoding="utf-8")} if out.exists() else set()

    rows = items()
    ctx = with_context(rows) if arm == "on" else {}
    todo = [r for r in rows if r["id"] not in done]
    print(f"{arm}: {len(todo)} to judge ({len(done)} done)")
    # A fresh cache each run, and the key includes the context anyway, so the two arms cannot answer
    # for each other.
    j = Judge(cache_ttl_s=0)
    t0 = time.time()
    with out.open("a", encoding="utf-8") as f:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i : i + BATCH]
            msgs = [Message(r["id"], r["text"][:4000], context=ctx.get(r["id"], ())) for r in chunk]
            verdicts = j.judge(msgs, CATS)
            for r, v in zip(chunk, verdicts, strict=True):
                f.write(json.dumps({"id": r["id"], "judged": v.judged, "reason": v.reason,
                                    "scores": v.scores, "context_n": len(ctx.get(r["id"], ()))}) + "\n")
            f.flush()
            print(f"  {i + len(chunk)}/{len(todo)}", end="\r")
    print(f"\n{j.judged_messages} judged, {j.input_tokens} tokens, "
          f"${j.input_tokens * 0.042 / 1e6:.4f}, {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
