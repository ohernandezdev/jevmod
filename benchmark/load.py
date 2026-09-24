"""How many streams at once before the service pushes back, and what a message costs at scale.

JEV-6. Latency for a single batch was measured on 2026-09-21: 801 ms p50 and 1110 ms p95 for fifty
messages. Nothing has measured what happens when several servers are busy at the same time, which is
the only shape a hosted service ever has, and nothing has priced the conversation window that
shipped in JEV-17.

**Retries are turned off for the concurrency arm on purpose.** `Judge` ships with three retries and
respects `Retry-After`, which is correct in production and useless here: it converts the answer to
"where does this service start pushing back" into a slightly longer latency, and hides the number
this file exists to find. With `max_retries=0` a 429 arrives as an exception and gets counted.

**It ramps and stops.** Concurrency doubles until the first level where rate limiting appears, and
then it stops rather than climbing to find out how much worse it can get. This is somebody's paid
API and the question is where the ceiling is, not how hard it can be hit.

    python -m benchmark.load concurrency          # paid, ramps 1 2 4 8 16
    python -m benchmark.load concurrency 32 64    # paid, continue a ramp that found no ceiling
    python -m benchmark.load cost                 # free, from committed token counts
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError, TypeSafeRateLimitError  # noqa: E402

from jevmod.core.policy import DEFAULT_ACTIONS  # noqa: E402
from jevmod.judge import CATEGORIES, Judge, Message  # noqa: E402
from jevmod.keys import get_api_key  # noqa: E402

DATA = Path(__file__).parent / "data" / "items.jsonl"
RESULTS = Path(__file__).parent / "results"
OUT = RESULTS / "load.jsonl"
# The categories that ship on, which is what a server is charged for. Excluding `offtopic` by
# name used to be the same thing and stopped being it when `ai_generated` arrived off by
# default: see the note in `run_jevmod.py`, where it put 26% on a published cost figure.
CATS = [c for c in CATEGORIES if DEFAULT_ACTIONS.get(c, "flag") != "off"]
BATCH = 25
LEVELS = (1, 2, 4, 8, 16)
PER_WORKER = 4  # requests each worker sends at a level; enough for a p95, small enough to stop fast
USD_PER_M = 0.042


def _judge_no_retry() -> Judge:
    """A judge that surfaces a 429 instead of absorbing it. Everything else matches production."""
    client = TypeSafeClient(api_key=get_api_key(), retry=RetryPolicy(max_retries=0), timeout=30.0)
    return Judge(client=client, cache_ttl_s=0)


def concurrency(levels: tuple[int, ...] = LEVELS) -> None:
    rows = [json.loads(line) for line in DATA.open(encoding="utf-8")]
    RESULTS.mkdir(exist_ok=True)
    print(f"batches of {BATCH}, {PER_WORKER} requests per worker, retries off\n")
    print("| streams at once | requests | p50 ms | p95 ms | max ms | rate limited | other errors |")
    print("|---|---|---|---|---|---|---|")

    with OUT.open("a", encoding="utf-8") as f:
        for n in levels:
            def one(worker: int, n: int = n) -> list[tuple[float, str]]:
                j = _judge_no_retry()
                out = []
                for k in range(PER_WORKER):
                    start = (worker * PER_WORKER + k) * BATCH % (len(rows) - BATCH)
                    msgs = [Message(r["id"], r["text"][:4000]) for r in rows[start : start + BATCH]]
                    t = time.perf_counter()
                    try:
                        j.judge(msgs, CATS)
                        out.append(((time.perf_counter() - t) * 1000, "ok"))
                    except TypeSafeRateLimitError:
                        out.append(((time.perf_counter() - t) * 1000, "rate_limited"))
                    except TypeSafeError as exc:
                        out.append(((time.perf_counter() - t) * 1000, type(exc).__name__))
                return out

            with ThreadPoolExecutor(max_workers=n) as pool:
                got = [r for rs in pool.map(one, range(n)) for r in rs]
            ok = sorted(ms for ms, status in got if status == "ok")
            limited = sum(1 for _, s in got if s == "rate_limited")
            other = sum(1 for _, s in got if s not in ("ok", "rate_limited"))
            p50 = statistics.median(ok) if ok else float("nan")
            p95 = ok[int(len(ok) * 0.95)] if len(ok) > 1 else (ok[0] if ok else float("nan"))
            print(f"| {n} | {len(got)} | {p50:.0f} | {p95:.0f} | {max(ok) if ok else float('nan'):.0f} "
                  f"| {limited} | {other} |")
            f.write(json.dumps({"streams": n, "requests": len(got), "p50_ms": p50, "p95_ms": p95,
                                "rate_limited": limited, "other_errors": other}) + "\n")
            f.flush()
            if limited or other:
                print(f"\nStopped at {n} streams: the service pushed back, which is the number this was "
                      "looking for. Climbing further would only measure how much worse it gets.")
                return
    print("\nNo push-back at any level tried. The ceiling is above 16 concurrent streams of "
          f"{BATCH} messages, and this says nothing about where it is.")


def cost() -> None:
    """Priced from token counts that were actually billed, in `results/context_{off,on}.jsonl`'s own
    runs, rather than from a rate card and an assumption about message length."""
    off, on = 3_268_515, 4_612_822  # input tokens for 2,504 judged messages, each arm, 2026-09-23
    judged = 2_504
    per = {"without the conversation window": off / judged, "with it": on / judged}
    print("Measured token cost per judged message, batches of 25, eight categories asked.\n")
    print("| request | tokens per message | $ per 1K | $ per 100K | $ per 1M |")
    print("|---|---|---|---|---|")
    for label, t in per.items():
        print(f"| {label} | {t:,.0f} | ${t * 1e3 * USD_PER_M / 1e6:.2f} | "
              f"${t * 1e5 * USD_PER_M / 1e6:.2f} | ${t * 1e6 * USD_PER_M / 1e6:,.2f} |")
    extra = per["with it"] / per["without the conversation window"] - 1
    print(f"\nThe window costs **{extra:.0%} more per message**. That is the number JEV-6 was for: it "
          "is what the context engine costs at scale, measured rather than estimated.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "concurrency":
        # Levels can be given on the command line so a run that found no ceiling can be continued
        # from where it stopped instead of paying again for the levels it already answered.
        chosen = tuple(int(a) for a in sys.argv[2:]) or LEVELS
        concurrency(chosen)
    elif cmd == "cost":
        cost()
    else:
        print(__doc__)
        raise SystemExit(2)
