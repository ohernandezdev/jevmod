"""Score any labelled set against any results file, per category, and name the mistakes.

`report.py` computes metrics but is welded to the comparison against Llama Guard, ShieldGemma and
toxic-bert: its columns, its thresholds and its sources are that table's. Every evaluation issue on
the board needs the same arithmetic over a different pair of files, so this is that arithmetic with
nothing else attached.

**It lists the false positives and the false negatives, it does not only count them.** A count says
a category is at 0.78 recall; the list says the misses are all sarcasm, or all one language, or all
the same person's phrasing. Only one of those can be acted on, and the count has been the only thing
available for long enough that nobody has read the misses.

    python -m benchmark.evaluate jevmod                        # a results file in results/
    python -m benchmark.evaluate context_off context_on        # two, as a delta
    python -m benchmark.evaluate jevmod --show spam --limit 5  # and read the mistakes

A results file is JSONL with `id` and `scores`, which is what every runner in this directory already
writes. A labelled set is JSONL with `id`, `text` and `labels`. Nothing here knows what produced
either one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevmod.core.policy import DEFAULT_THRESHOLDS  # noqa: E402

RESULTS = Path(__file__).parent / "results"
DATA = Path(__file__).parent / "data" / "items.jsonl"


def load_labels(path: Path = DATA) -> dict[str, dict]:
    return {r["id"]: r for r in map(json.loads, path.open(encoding="utf-8"))}


def load_scores(name: str) -> dict[str, dict[str, float]]:
    """`name` is a file in `results/`, with or without the extension. Later rows win, which is what a
    resumable runner writes when it is re-run."""
    path = RESULTS / (name if name.endswith(".jsonl") else f"{name}.jsonl")
    out: dict[str, dict[str, float]] = {}
    for r in map(json.loads, path.open(encoding="utf-8")):
        if "scores" in r:
            out[r["id"]] = r["scores"]
    return out


def confusion(labels: dict[str, dict], scores: dict[str, dict[str, float]], category: str,
              threshold: float) -> tuple[list[str], list[str], list[str]]:
    """(true positives, false positives, false negatives), as ids rather than counts."""
    tp, fp, fn = [], [], []
    for mid, sc in scores.items():
        if mid not in labels or category not in sc:
            continue
        actual = category in (labels[mid].get("labels") or [])
        flagged = sc[category] >= threshold
        if flagged and actual:
            tp.append(mid)
        elif flagged:
            fp.append(mid)
        elif actual:
            fn.append(mid)
    return tp, fp, fn


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def auroc(labels: dict[str, dict], scores: dict[str, dict[str, float]], category: str) -> float | None:
    """Rank based, ties at half. Threshold-free, so it says whether the ordering is any good
    independently of where the line happens to sit."""
    pos, neg = [], []
    for mid, sc in scores.items():
        if mid not in labels or category not in sc:
            continue
        (pos if category in (labels[mid].get("labels") or []) else neg).append(sc[category])
    if not pos or not neg:
        return None
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def best_threshold(labels: dict[str, dict], scores: dict[str, dict[str, float]],
                   category: str) -> tuple[float, float]:
    """The threshold with the highest F1, and that F1, searched on the 0.01 grid the scores live on.

    It is here because a shipped threshold and a well-placed one are different questions and the
    table above only answers the first. A category with a high AUROC and a poor F1 has a model that
    ranks well and a line in the wrong place, which is a one-number fix; a category with a poor
    AUROC has neither, and moving the line will not save it.

    Not a recommendation to ship. Best-F1 on a labelled set is fitted to that set, F1 weights a
    false positive and a false negative equally when this product does not, and `benchmark/
    BATCH_EFFECT.md` measured that the second decimal of a threshold is not reproducible anyway.
    Read it as the size of the gap, not as the number to paste into `policy.py`.
    """
    best = (0.0, 0.0)
    for k in range(50, 100):
        th = k / 100
        tp, fp, fn = confusion(labels, scores, category, th)
        _, _, f = prf(len(tp), len(fp), len(fn))
        if f > best[1]:
            best = (th, f)
    return best


def categories_in(labels: dict[str, dict]) -> list[str]:
    return sorted({c for r in labels.values() for c in (r.get("labels") or [])})


def table(name: str, labels: dict[str, dict], scores: dict[str, dict[str, float]]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for cat in categories_in(labels):
        th = DEFAULT_THRESHOLDS.get(cat, 0.8)
        tp, fp, fn = confusion(labels, scores, cat, th)
        if not tp and not fn:
            continue  # the set carries no examples of this category; a row of zeros says nothing
        p, r, f = prf(len(tp), len(fp), len(fn))
        bt, bf = best_threshold(labels, scores, cat)
        rows[cat] = {"n": len(tp) + len(fn), "th": th, "precision": p, "recall": r, "f1": f,
                     "auroc": auroc(labels, scores, cat), "fp": fp, "fn": fn,
                     "best_th": bt, "best_f1": bf}
    print(f"\n## {name}   ({len(scores)} scored)\n")
    print("| category | labelled | shipped th | precision | recall | F1 | AUROC | FP | FN "
          "| best F1 (at th) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for cat, v in rows.items():
        a = f"{v['auroc']:.3f}" if v["auroc"] is not None else "-"
        print(f"| {cat} | {v['n']} | {v['th']} | {v['precision']:.3f} | {v['recall']:.3f} | "
              f"{v['f1']:.3f} | {a} | {len(v['fp'])} | {len(v['fn'])} | "
              f"{v['best_f1']:.3f} ({v['best_th']:.2f}) |")
    return rows


def delta(a_name: str, b_name: str, a: dict[str, dict], b: dict[str, dict]) -> None:
    print(f"\n## {b_name} against {a_name}\n")
    print("| category | Δ precision | Δ recall | Δ F1 | Δ AUROC | verdicts that changed |")
    print("|---|---|---|---|---|---|")
    for cat in a:
        if cat not in b:
            continue
        moved = len(set(a[cat]["fp"]) ^ set(b[cat]["fp"])) + len(set(a[cat]["fn"]) ^ set(b[cat]["fn"]))
        da = (b[cat]["auroc"] - a[cat]["auroc"]) if a[cat]["auroc"] and b[cat]["auroc"] else None
        print(f"| {cat} | {b[cat]['precision'] - a[cat]['precision']:+.3f} | "
              f"{b[cat]['recall'] - a[cat]['recall']:+.3f} | {b[cat]['f1'] - a[cat]['f1']:+.3f} | "
              f"{f'{da:+.3f}' if da is not None else '-'} | {moved} |")
    print("\nA changed verdict is a message that one run flags and the other does not. It is the")
    print("number a server owner would notice; the metric columns can cancel while it does not.")


def show(labels: dict[str, dict], rows: dict[str, dict], category: str, limit: int) -> None:
    v = rows.get(category)
    if not v:
        print(f"\nno rows for {category!r}; the set has {', '.join(categories_in(labels))}")
        return
    for kind in ("fp", "fn"):
        title = "flagged and should not have been" if kind == "fp" else "not flagged and should have been"
        print(f"\n### {category}: {title} ({len(v[kind])})\n")
        for mid in v[kind][:limit]:
            print(f"- `{mid}` {labels[mid]['text'][:160]!r}")


def main(argv: list[str]) -> int:
    # argparse rather than splitting on "--" by hand: the hand-rolled version put the argument of
    # `--show` into the list of result files and went looking for `results/spam.jsonl`.
    import argparse

    ap = argparse.ArgumentParser(prog="benchmark.evaluate", description=__doc__)
    ap.add_argument("results", nargs="+", help="one or two files in results/, with or without .jsonl")
    ap.add_argument("--show", metavar="CATEGORY", help="print the mistakes for one category")
    ap.add_argument("--limit", type=int, default=10, help="how many of each to print")
    ap.add_argument("--labels", type=Path, default=DATA, help="a labelled set other than items.jsonl")
    args = ap.parse_args(argv)

    labels = load_labels(args.labels)
    tables = [table(n, labels, load_scores(n)) for n in args.results[:2]]
    if len(tables) == 2:
        delta(args.results[0], args.results[1], tables[0], tables[1])
    if args.show:
        show(labels, tables[-1], args.show, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
