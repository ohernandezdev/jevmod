"""Does a deterministic, model-free signal make jevmod's `ai_generated` category good enough to stop being
experimental?

Three commands:

    python -m benchmark.ai_detect.deterministic features   # every feature alone on the existing 520 texts
    python -m benchmark.ai_detect.deterministic trap       # build + score the careful/formal/non-native corpus
    python -m benchmark.ai_detect.deterministic combine    # deterministic + Jev, grouped 5-fold CV

Nothing here touches `jevmod/`. It reads `results/dataset.jsonl` and `results/raw_A.jsonl` produced by `run.py`,
and writes `results/trap.jsonl` and `results/raw_A_trap.jsonl` of its own.

    python -m benchmark.ai_detect.deterministic ask-trap   # the only command that spends money (Jev on the trap set)
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .features import DEGRADED_BY_NORMALIZE, FEATURE_NAMES, describe, extract

HERE = Path(__file__).parent
RESULTS = HERE / "results"
DATASET = RESULTS / "dataset.jsonl"
TRAP = RESULTS / "trap.jsonl"
USD_PER_M = 0.042
MAX_CHARS = 400
BATCH = 25

# ------------------------------------------------------------------------------------------ loading


def _clip(text: str) -> str:
    from jevmod.judge import normalize

    t = normalize(text)
    if len(t) <= MAX_CHARS:
        return t
    cut = t[:MAX_CHARS]
    return cut[: cut.rfind(" ")] if " " in cut[300:] else cut


def load_items(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.open(encoding="utf-8")]


def load_a(path: Path = RESULTS / "raw_A.jsonl") -> dict[str, float]:
    if not path.exists():
        return {}
    return {r["id"]: r["answers"]["a"]["p"] for r in (json.loads(l) for l in path.open(encoding="utf-8"))}


def featurize(items: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    return {it["id"]: extract(it["text"]) for it in items}


# ------------------------------------------------------------------------------------------ metrics


def auroc(pairs: list[tuple[float, int]]) -> float | None:
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return None
    p = np.array(pos)
    n = np.array(neg)
    wins = (p[:, None] > n[None, :]).sum() + 0.5 * (p[:, None] == n[None, :]).sum()
    return float(wins / (len(pos) * len(neg)))


def prf(pairs: list[tuple[float, int]], th: float) -> tuple[float, float, float]:
    tp = sum(1 for s, y in pairs if y and s >= th)
    fp = sum(1 for s, y in pairs if not y and s >= th)
    fn = sum(1 for s, y in pairs if y and s < th)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return (2 * prec * rec / (prec + rec) if prec + rec else 0.0, prec, rec)


def projected_precision(tpr: float, fpr: float, n_neg: int, base: float = 0.02) -> tuple[float, float]:
    """Same projection the existing REPORT.md uses: rule-of-three upper bound on a zero-observed FPR."""
    fpr_ub = max(fpr, 3.0 / max(n_neg, 1))
    denom = base * tpr + (1 - base) * fpr_ub
    return (base * tpr / denom if denom else 0.0, fpr_ub)


def threshold_at_recall(pairs: list[tuple[float, int]], target_recall: float) -> float:
    """Highest threshold that still reaches `target_recall`, so two detectors can be compared at equal recall."""
    pos = sorted((s for s, y in pairs if y), reverse=True)
    if not pos:
        return 1.0
    k = max(1, min(len(pos), int(math.ceil(target_recall * len(pos)))))
    return pos[k - 1]


# ------------------------------------------------------- logistic regression (no sklearn in this venv)


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 400) -> np.ndarray:
    """Newton/IRLS with L2 on an intercept-augmented design matrix. Small data, so this is exact enough."""
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        z = Xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = Xb.T @ (p - y) + reg @ w
        W = np.clip(p * (1 - p), 1e-6, None)
        H = Xb.T @ (Xb * W[:, None]) + reg
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict_logreg(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    z = np.hstack([np.ones((X.shape[0], 1)), X]) @ w
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def logit(p: float, eps: float = 1e-3) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


# ------------------------------------------------------------------------- command: features alone

HUMAN_CAREFUL_STRATA = ("human_hc3_formal", "human_formal_long", "human_nonnative")


def cmd_features() -> None:
    items = {it["id"]: it for it in load_items()}
    feats = featurize(list(items.values()))
    ids = list(items)
    labels = {i: items[i]["label"] for i in ids}
    desc = describe()

    careful = [i for i in ids if items[i]["stratum"] in HUMAN_CAREFUL_STRATA]
    chat = [i for i in ids if items[i]["stratum"] == "human_chat"]
    pairs_ids = [i for i in ids if items[i]["pair"] is not None]

    print("## Every deterministic feature alone, on the existing 520 texts\n")
    print(
        "AUROC > 0.5 means the feature is higher on AI text. `fire` columns use the feature's own natural "
        "threshold (> 0 for counts, else the AI-side median).\n"
    )
    print(
        "| feature | AUROC all | AUROC matched pairs | fires on AI | fires on careful human | "
        "fires on chat human | verdict |"
    )
    print("|---|---|---|---|---|---|---|")
    rows = []
    for f in FEATURE_NAMES:
        pairs = [(feats[i][f], labels[i]) for i in ids]
        a_all = auroc(pairs)
        a_mp = auroc([(feats[i][f], labels[i]) for i in pairs_ids])
        vals_ai = [feats[i][f] for i in ids if labels[i]]
        # counts: "fires" means >= 1; rates/fractions: >= the AI median, floored away from zero
        binary = max(feats[i][f] for i in ids) <= 1.0
        th = 0.5 if binary else max(1.0, float(np.median(vals_ai)))
        # f and th are bound as defaults: the closure is called inside this iteration, and binding them
        # says so instead of relying on it.
        def fire(sub: list[int], f: str = f, th: float = th) -> float:
            return (sum(1 for i in sub if feats[i][f] >= th) / len(sub)) if sub else float("nan")

        rows.append((f, a_all, a_mp, fire([i for i in ids if labels[i]]), fire(careful), fire(chat), th))

    rows.sort(key=lambda r: -(r[1] or 0))
    for f, a_all, a_mp, fa, fc, fh, th in rows:
        lift = (a_all or 0.5) - 0.5
        if f in DEGRADED_BY_NORMALIZE:
            verdict = "unmeasurable here (newlines stripped)"
        elif abs(lift) < 0.05:
            verdict = "no signal"
        elif abs(lift) < 0.12:
            verdict = "weak"
        else:
            verdict = "real signal"
        print(
            f"| `{f}` (th {th:g}) | {a_all:.3f} | {a_mp:.3f} | {fa:.3f} | {fc:.3f} | {fh:.3f} | {verdict} |"
        )

    print("\n### One line each\n")
    for f, *_ in rows:
        print(f"- `{f}`: {desc[f]}")


# ------------------------------------------------------------------------------ command: trap corpus


def cmd_trap_build(seed: int = 20260919) -> None:
    """Careful, formal and non-native HUMAN text, large enough to measure a false-positive rate on.

    Every source predates ChatGPT (JFLEG 2017, Civil Comments 2017-19, HC3 human answers scraped 2022), so a
    positive here is a false positive by construction.
    """
    from datasets import load_dataset

    rng = random.Random(seed)
    existing = {it["text"] for it in load_items()}
    out: list[dict[str, Any]] = []

    # 1. Non-native English, uncorrected learner writing. The population the trap is about.
    jf = load_dataset("jhu-clsp/jfleg", split="validation")
    jf2 = load_dataset("jhu-clsp/jfleg", split="test")
    learner = [s.strip() for s in list(jf["sentence"]) + list(jf2["sentence"]) if 80 <= len(s.strip()) <= 400]
    rng.shuffle(learner)
    for i, s in enumerate(x for x in learner if _clip(x) not in existing):
        if i >= 300:
            break
        out.append(
            {
                "id": f"tnn{i}",
                "text": _clip(s),
                "label": 0,
                "stratum": "nonnative_learner",
                "provenance": "JFLEG validation+test (English-learner, uncorrected)",
            }
        )

    # 2. Careful formal human prose: long, clean Civil Comments (2017-2019 news comments).
    disk = [json.loads(l) for l in (HERE.parent / "data" / "items.jsonl").open(encoding="utf-8")]
    cc = [r for r in disk if r["source"] == "civil_comments" and not r["labels"] and len(r["text"]) >= 200]
    rng.shuffle(cc)
    n = 0
    for r in cc:
        if _clip(r["text"]) in existing:
            continue
        out.append(
            {
                "id": f"tcl{r['id']}",
                "text": _clip(r["text"]),
                "label": 0,
                "stratum": "formal_long_comment",
                "provenance": "Civil Comments (toxicity<=0.1, >=200 chars)",
            }
        )
        n += 1
        if n >= 150:
            break

    # 3. The measured worst case: human answers in an encyclopedic / expert register, from HC3.
    hc3 = load_dataset("json", data_files="hf://datasets/Hello-SimpleAI/HC3/all.jsonl", split="train")
    pool: dict[str, list[tuple[str, str]]] = {}
    for r in hc3:
        h = (r["human_answers"] or [None])[0]
        if not h or not (60 <= len(h) <= 1200):
            continue
        pool.setdefault(r["source"], []).append((r["source"], h))
    for src in sorted(pool):
        rows = pool[src]
        rng.shuffle(rows)
        n = 0
        for _s, h in rows:
            c = _clip(h)
            if c in existing:
                continue
            out.append(
                {
                    "id": f"th{src}{n}",
                    "text": c,
                    "label": 0,
                    "stratum": f"hc3_human_{src}",
                    "provenance": f"HC3/{src}/human (held out from the 520)",
                }
            )
            n += 1
            if n >= 60:
                break

    rng.shuffle(out)
    with TRAP.open("w", encoding="utf-8") as f:
        for it in out:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    from collections import Counter

    print(f"{len(out)} human items written to {TRAP}")
    for s, c in sorted(Counter(i["stratum"] for i in out).items()):
        print(f"  {s:26s} {c:4d}")


def cmd_trap_report() -> None:
    """Deterministic-only false positives on careful / formal / non-native human writing. No Jev, no cost."""
    items = load_items(TRAP)
    feats = featurize(items)
    strata = sorted({i["stratum"] for i in items})
    print(f"## Deterministic false positives on {len(items)} careful/formal/non-native human texts\n")
    cols = {
        "`d_register_total`>=1": lambda f: f["d_register_total"] >= 1,
        "`d_register_total`>=2": lambda f: f["d_register_total"] >= 2,
        "`d_emdash_any`": lambda f: f["d_emdash_any"] >= 1,
        "`d_mechanically_clean`": lambda f: f["d_mechanically_clean"] >= 1,
        "`d_ai_words`>0": lambda f: f["d_ai_words"] > 0,
        "any §1-§5 staging tell": lambda f: (
            f["d_not_x_but_y"] + f["d_deep_sayings"] + f["d_staged_opener"] + f["d_strawman"] + f["d_tidy_closer"]
        )
        >= 1,
    }
    print("| stratum | n | " + " | ".join(cols) + " |")
    print("|---|---|" + "---|" * len(cols))
    for s in list(strata) + ["ALL"]:
        sub = [i for i in items if s == "ALL" or i["stratum"] == s]
        cells = [f"{sum(1 for i in sub if fn(feats[i['id']])) / len(sub):.3f}" for fn in cols.values()]
        print(f"| {s} | {len(sub)} | " + " | ".join(cells) + " |")
    _ = FEATURE_NAMES, DEGRADED_BY_NORMALIZE


# ---------------------------------------------------------------------------- command: ask Jev (costs)


def cmd_ask_trap(limit: int = 700) -> None:
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    from jevmod.keys import get_api_key

    from .run import questions_a

    items = load_items(TRAP)[:limit]
    out = RESULTS / "raw_A_trap.jsonl"
    done = {json.loads(l)["id"] for l in out.open(encoding="utf-8")} if out.exists() else set()
    todo = [it for it in items if it["id"] not in done]
    print(f"[A/trap] {len(todo)} to ask, {len(done)} done")
    client = TypeSafeClient(
        api_key=get_api_key(),
        retry=RetryPolicy(
            max_retries=3, backoff_initial=0.5, backoff_max=8.0, http_statuses={429, 500, 502, 503, 504, 529}
        ),
        timeout=90.0,
    )
    total = 0
    with out.open("a", encoding="utf-8") as fh:
        for start in range(0, len(todo), BATCH):
            chunk = todo[start : start + BATCH]
            state = {"messages": {f"m{i}": {"text": it["text"]} for i, it in enumerate(chunk)}}
            questions = {}
            for i in range(len(chunk)):
                for name, q in questions_a(f"messages.m{i}").items():
                    questions[f"{name}_{i}"] = q
            resp = client.system_one(state=state, questions=questions)
            toks = getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
            total += toks
            for i, it in enumerate(chunk):
                fh.write(
                    json.dumps(
                        {"id": it["id"], "answers": {"a": {"p": float(resp.answers[f"a_{i}"].noul)}}},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            fh.flush()
            print(f"[A/trap] {start + len(chunk)}/{len(todo)}  {total:,} tok  ${total * USD_PER_M / 1e6:.4f}")


# ------------------------------------------------------------------------------- command: combination

# Chosen before looking at any combined result: everything except the features the corpus cannot measure.
COMBINE_FEATURES = [f for f in FEATURE_NAMES if f not in DEGRADED_BY_NORMALIZE]


def _design(ids, feats, names) -> np.ndarray:
    return np.array([[feats[i][f] for f in names] for i in ids], dtype=float)


def _folds(items: dict[str, dict], ids: list[str], k: int = 5, seed: int = 20260919):
    """Grouped folds: a topic-matched HC3 pair (same question, human + ChatGPT answer) never straddles a split,
    so the model cannot memorise the question and answer it from the other side."""
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for i in ids:
        g = f"pair{items[i]['pair']}" if items[i].get("pair") is not None else f"solo{i}"
        groups.setdefault(g, []).append(i)
    keys = sorted(groups)
    rng.shuffle(keys)
    folds: list[list[str]] = [[] for _ in range(k)]
    for n, g in enumerate(keys):
        folds[n % k] += groups[g]
    return folds


def cmd_combine() -> None:
    items = {it["id"]: it for it in load_items()}
    A = load_a()
    ids = [i for i in items if i in A]
    feats = featurize([items[i] for i in ids])
    y = np.array([items[i]["label"] for i in ids], dtype=float)
    names = COMBINE_FEATURES
    print(f"n={len(ids)}  AI={int(y.sum())}  human={int(len(y) - y.sum())}  features={len(names)}\n")

    Xd = _design(ids, feats, names)
    Xa = np.array([[logit(A[i])] for i in ids])
    designs = {
        "Jev A alone (baseline)": Xa,
        "deterministic alone": Xd,
        "Jev A + deterministic": np.hstack([Xa, Xd]),
    }

    folds = _folds(items, ids)
    idx = {i: n for n, i in enumerate(ids)}
    oof: dict[str, np.ndarray] = {}
    for label, X in designs.items():
        pred = np.zeros(len(ids))
        for f in range(len(folds)):
            te = [idx[i] for i in folds[f]]
            tr = [idx[i] for i in ids if idx[i] not in set(te)]
            mu, sd = X[tr].mean(0), X[tr].std(0)
            sd[sd == 0] = 1.0
            w = fit_logreg((X[tr] - mu) / sd, y[tr], l2=2.0)
            pred[te] = predict_logreg(w, (X[te] - mu) / sd)
        oof[label] = pred
    # raw Jev probability, no fitting at all: the thing actually shipping today
    oof["Jev A raw (unfitted)"] = np.array([A[i] for i in ids])

    careful = [n for n, i in enumerate(ids) if items[i]["stratum"] in HUMAN_CAREFUL_STRATA]
    chat = [n for n, i in enumerate(ids) if items[i]["stratum"] == "human_chat"]
    wiki = [n for n, i in enumerate(ids) if items[i]["provenance"] == "HC3/wiki_csai/human"]
    nonnat = [n for n, i in enumerate(ids) if items[i]["stratum"] == "human_nonnative"]
    neg = [n for n in range(len(ids)) if not y[n]]

    print("### Out-of-fold, grouped 5-fold CV (topic-matched pairs kept together)\n")
    print(
        "At a fixed recall of 0.954 - the recall Jev A reaches at its shipping threshold 0.85 - so the three "
        "detectors are compared on false positives only.\n"
    )
    print(
        "| detector | AUROC (oof) | FPR all human | FPR careful human | FPR wiki-style | FPR non-native | "
        "FPR chat | P@2% (all human) | P@2% (chat only) |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for label, p in oof.items():
        pairs = list(zip(p.tolist(), y.astype(int).tolist(), strict=True))
        th = threshold_at_recall(pairs, 0.954)
        tpr = float(((p >= th) & (y == 1)).sum() / (y == 1).sum())
        def fr(sub: list[int], p: Any = p, th: float = th) -> float:
            return float(sum(1 for n in sub if p[n] >= th) / len(sub)) if sub else float("nan")

        f_all, f_car, f_wiki, f_nn, f_chat = fr(neg), fr(careful), fr(wiki), fr(nonnat), fr(chat)
        p2_all = projected_precision(tpr, f_all, len(neg))[0]
        p2_chat = projected_precision(tpr, f_chat, len(chat))[0]
        print(
            f"| {label} | {auroc(pairs):.3f} | {f_all:.3f} | {f_car:.3f} | {f_wiki:.3f} | {f_nn:.3f} | "
            f"{f_chat:.3f} | {p2_all:.3f} | {p2_chat:.3f} |"
        )

    # ---- the rules table: TPR from the 260 AI texts, FPR from the 697-item careful-human trap corpus ----
    trap_a = load_a(RESULTS / "raw_A_trap.jsonl")
    titems = {it["id"]: it for it in load_items(TRAP) if it["id"] in trap_a} if TRAP.exists() else {}
    tf = featurize(list(titems.values()))
    tids = list(titems)

    def _gates(fx: dict[str, float], a: float) -> dict[str, bool]:
        staging = (
            fx["d_not_x_but_y"] + fx["d_deep_sayings"] + fx["d_staged_opener"] + fx["d_strawman"] + fx["d_tidy_closer"]
        )
        human_tell = fx["d_slang"] + fx["d_missing_apostrophe"] + fx["d_emoji"] + fx["d_repeated_punct"]
        return {
            "A >= 0.85 (today's shipping rule)": a >= 0.85,
            "A >= 0.85 AND register_total >= 1": a >= 0.85 and fx["d_register_total"] >= 1,
            "A >= 0.85 AND register_total >= 2": a >= 0.85 and fx["d_register_total"] >= 2,
            "A >= 0.85 AND (register >= 1 OR chatbot residue)": a >= 0.85
            and (fx["d_register_total"] >= 1 or fx["d_chatbot_residue"] >= 1),
            "A >= 0.85 AND no human tell (slang/typo/emoji/!!!)": a >= 0.85 and human_tell == 0,
            "A >= 0.85 AND >= 1 staging tell (SKILL.md §1-§5)": a >= 0.85 and staging >= 1,
            "A >= 0.85 AND mechanically clean": a >= 0.85 and fx["d_mechanically_clean"] >= 1,
            "register_total >= 1 alone (no Jev)": fx["d_register_total"] >= 1,
            "A >= 0.90 alone": a >= 0.90,
        }

    rule_names = list(_gates(feats[ids[0]], 0.0))
    ai_ids = [i for i in ids if items[i]["label"]]
    chat_ids = [i for i in ids if items[i]["stratum"] == "human_chat"]
    print("\n### Simple rules: Jev A AND a deterministic gate\n")
    print(
        "TPR from the 260 AI texts. FPR from the 697-item careful/formal/non-native human corpus built for this "
        "run (`results/trap.jsonl`), and from the 90 real chat messages. Precision is projected at a 2% base "
        "rate the same way the existing REPORT.md does it, with the rule-of-three upper bound on a zero FPR.\n"
    )
    print(
        "| rule | TPR | FPR careful (n=697) | FPR wiki-style (n=60) | FPR non-native (n=300) | FPR chat (n=90) "
        "| P@2% careful | P@2% chat |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for rn in rule_names:
        tpr = sum(1 for i in ai_ids if _gates(feats[i], A[i])[rn]) / len(ai_ids)
        trap_fire = [i for i in tids if _gates(tf[i], trap_a[i])[rn]]
        f_trap = len(trap_fire) / max(len(tids), 1)
        wik = [i for i in tids if titems[i]["stratum"] == "hc3_human_wiki_csai"]
        nn = [i for i in tids if titems[i]["stratum"] == "nonnative_learner"]
        f_wiki = sum(1 for i in wik if _gates(tf[i], trap_a[i])[rn]) / max(len(wik), 1)
        f_nn = sum(1 for i in nn if _gates(tf[i], trap_a[i])[rn]) / max(len(nn), 1)
        f_chat = sum(1 for i in chat_ids if _gates(feats[i], A[i])[rn]) / max(len(chat_ids), 1)
        p_trap = projected_precision(tpr, f_trap, len(tids))[0]
        p_chat = projected_precision(tpr, f_chat, len(chat_ids))[0]
        print(
            f"| {rn} | {tpr:.3f} | {f_trap:.3f} | {f_wiki:.3f} | {f_nn:.3f} | {f_chat:.3f} | "
            f"{p_trap:.3f} | {p_chat:.3f} |"
        )
    Araw = np.array([A[i] for i in ids])
    _ = Araw

    # Fitted-model coefficients, refit on everything, only to see which features the fit actually used.
    X = designs["Jev A + deterministic"]
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    w = fit_logreg((X - mu) / sd, y, l2=2.0)
    coefs = sorted(zip(["logit(A)"] + names, w[1:], strict=True), key=lambda kv: -abs(kv[1]))[:15]
    print("\n### What the fitted model leans on (standardised coefficients, refit on all 520)\n")
    print("| feature | coefficient |")
    print("|---|---|")
    for k, v in coefs:
        print(f"| `{k}` | {v:+.3f} |")

    # If Jev was asked about the trap corpus, the same rules on that population.
    trap_a = load_a(RESULTS / "raw_A_trap.jsonl")
    if trap_a and TRAP.exists():
        titems = {it["id"]: it for it in load_items(TRAP) if it["id"] in trap_a}
        tf = featurize(list(titems.values()))
        print(f"\n### The trap population, real Jev scores, n={len(titems)} (every item human by construction)\n")
        print("| stratum | n | FPR A>=0.85 | FPR A>=0.85 AND staging | FPR A>=0.85 AND clean | FPR A>=0.90 |")
        print("|---|---|---|---|---|---|")
        for s in sorted({i["stratum"] for i in titems.values()}):
            sub = [i for i in titems if titems[i]["stratum"] == s]
            def st(i: str) -> float:
                """SKILL.md sections 1 to 5: the staging tells."""
                return sum(
                    tf[i][k]
                    for k in ("d_not_x_but_y", "d_deep_sayings", "d_staged_opener", "d_strawman", "d_tidy_closer")
                )
            r1 = sum(1 for i in sub if trap_a[i] >= 0.85) / len(sub)
            r2 = sum(1 for i in sub if trap_a[i] >= 0.85 and st(i) >= 1) / len(sub)
            r3 = sum(1 for i in sub if trap_a[i] >= 0.85 and tf[i]["d_mechanically_clean"] >= 1) / len(sub)
            r4 = sum(1 for i in sub if trap_a[i] >= 0.90) / len(sub)
            print(f"| {s} | {len(sub)} | {r1:.3f} | {r2:.3f} | {r3:.3f} | {r4:.3f} |")


# ------------------------------------------------------------------- batch-composition controls (cost money)


def _client():
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    from jevmod.keys import get_api_key

    return TypeSafeClient(
        api_key=get_api_key(),
        retry=RetryPolicy(
            max_retries=3, backoff_initial=0.5, backoff_max=8.0, http_statuses={429, 500, 502, 503, 504, 529}
        ),
        timeout=90.0,
    )


def _ask_batches(batches: list[list[tuple[str, str]]], out: Path) -> None:
    """Ask formulation A about pre-composed batches of (id, text). The caller owns the composition."""
    from .run import questions_a

    client = _client()
    total = 0
    with out.open("w", encoding="utf-8") as fh:
        for n, chunk in enumerate(batches, 1):
            state = {"messages": {f"m{i}": {"text": t} for i, (_id, t) in enumerate(chunk)}}
            qs = {}
            for i in range(len(chunk)):
                for name, q in questions_a(f"messages.m{i}").items():
                    qs[f"{name}_{i}"] = q
            resp = client.system_one(state=state, questions=qs)
            total += getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
            for i, (_id, _t) in enumerate(chunk):
                fh.write(
                    json.dumps({"id": _id, "answers": {"a": {"p": float(resp.answers[f"a_{i}"].noul)}}}) + "\n"
                )
            fh.flush()
            print(f"  batch {n}/{len(batches)}  {total:,} tok  ${total * USD_PER_M / 1e6:.4f}")


def _control_sample() -> list[str]:
    """The 156 trap items the composition controls re-ask: every wiki_csai item, 48 non-native, 48 other."""
    ti = {i["id"]: i for i in load_items(TRAP)}
    wiki = [i for i in ti if ti[i]["stratum"] == "hc3_human_wiki_csai"]
    nn = [i for i in ti if ti[i]["stratum"] == "nonnative_learner"][:48]
    other = [i for i in ti if ti[i]["stratum"] not in ("hc3_human_wiki_csai", "nonnative_learner")][:48]
    return wiki + nn + other


def cmd_ask_control() -> None:
    """Re-ask the 156 sample in 50/50 AI/human batches: does a neighbour's label move this message's score?"""
    rng = random.Random(20260919)
    items = {i["id"]: i for i in load_items()}
    ti = {i["id"]: i for i in load_items(TRAP)}
    ai_pool = [i for i in items if items[i]["label"]]
    rng.shuffle(ai_pool)
    sample = _control_sample()
    batches = []
    for b in range(0, len(sample), 12):
        h = sample[b : b + 12]
        a = ai_pool[b : b + len(h)]
        chunk = [(x, ti[x]["text"]) for x in h] + [(x, items[x]["text"]) for x in a]
        rng.shuffle(chunk)
        batches.append(chunk)
    _ask_batches(batches, RESULTS / "raw_A_control.jsonl")


def cmd_ask_retest() -> None:
    """Re-ask the same 156 in all-human batches again, REGROUPED: the `rng.shuffle(sample)` below puts
    them in different batches of 25. That makes this a membership control, not a repetition control.

    REPORT2 called it "plain run-to-run noise" and read its 17/156 as instability "with nothing changed
    at all". It is not: what changed is which messages share a batch. A byte-identical repeat was never
    run here; `benchmark/batch_effect.py` runs one, and it flips 2.7% against this arm's 11%. Corrected
    2026-09-22, see the note at the top of REPORT2.md section 1."""
    rng = random.Random(4242)
    ti = {i["id"]: i for i in load_items(TRAP)}
    sample = _control_sample()
    rng.shuffle(sample)
    batches = [[(x, ti[x]["text"]) for x in sample[b : b + 25]] for b in range(0, len(sample), 25)]
    _ask_batches(batches, RESULTS / "raw_A_retest.jsonl")


def cmd_ask_dilute() -> None:
    """Judge 60 AI texts in 4-AI/21-human batches: the composition a channel with a low AI rate actually has."""
    rng = random.Random(909)
    items = {i["id"]: i for i in load_items()}
    ti = {i["id"]: i for i in load_items(TRAP)}
    ai = [i for i in items if items[i]["label"]]
    rng.shuffle(ai)
    ai = ai[:60]
    hp = list(ti)
    rng.shuffle(hp)
    batches = []
    for b in range(15):
        chunk = [(x, items[x]["text"]) for x in ai[b * 4 : (b + 1) * 4]]
        chunk += [(x, ti[x]["text"]) for x in hp[b * 21 : (b + 1) * 21]]
        rng.shuffle(chunk)
        batches.append(chunk)
    _ask_batches(batches, RESULTS / "raw_A_dilute.jsonl")


def cmd_composition() -> None:
    """Is a message's probability a property of the message, or of the batch it arrived in?"""
    import statistics

    ti = {i["id"]: i for i in load_items(TRAP)}
    pure, ctrl, ret = (
        load_a(RESULTS / "raw_A_trap.jsonl"),
        load_a(RESULTS / "raw_A_control.jsonl"),
        load_a(RESULTS / "raw_A_retest.jsonl"),
    )
    ids = [i for i in ret if i in pure and i in ctrl]
    print(f"## Batch composition moves the score, n={len(ids)} human texts asked three times\n")
    print("| stratum | n | p, all-human batch | p, regrouped all-human | p, 50/50 batch "
          "| regrouping drift | composition drift |")
    print("|---|---|---|---|---|---|---|")
    for s in sorted({ti[i]["stratum"] for i in ids}) + ["ALL"]:
        sub = [i for i in ids if s == "ALL" or ti[i]["stratum"] == s]
        mp = statistics.mean(pure[i] for i in sub)
        mr = statistics.mean(ret[i] for i in sub)
        mc = statistics.mean(ctrl[i] for i in sub)
        print(f"| {s} | {len(sub)} | {mp:.3f} | {mr:.3f} | {mc:.3f} | {mr - mp:+.3f} | {mc - mr:+.3f} |")
    print("\n| condition | FPR@0.85 all | FPR@0.85 wiki-style | FPR@0.90 all | items flipping across 0.85 |")
    print("|---|---|---|---|---|")
    w = [i for i in ids if ti[i]["stratum"] == "hc3_human_wiki_csai"]
    for name, sc in (("all-human batch", pure), ("regrouped, all-human", ret), ("50/50 batch", ctrl)):
        flip = sum(1 for i in ids if (pure[i] >= 0.85) != (sc[i] >= 0.85))
        print(
            f"| {name} | {sum(1 for i in ids if sc[i] >= 0.85) / len(ids):.3f} | "
            f"{sum(1 for i in w if sc[i] >= 0.85) / len(w):.3f} | "
            f"{sum(1 for i in ids if sc[i] >= 0.90) / len(ids):.3f} | {flip}/{len(ids)} |"
        )


def cmd_realistic() -> None:
    """The headline table: every rule scored on batches composed like a real channel, not like a benchmark."""
    items = {i["id"]: i for i in load_items()}
    ti = {i["id"]: i for i in load_items(TRAP)}
    dil = load_a(RESULTS / "raw_A_dilute.jsonl")
    A = load_a()
    ai = [i for i in dil if i in items and items[i]["label"]]
    hu = [i for i in dil if i in ti]
    fx = {i: extract(items[i]["text"]) for i in ai}
    fx.update({i: extract(ti[i]["text"]) for i in hu})
    wiki = [i for i in hu if ti[i]["stratum"] == "hc3_human_wiki_csai"]
    nn = [i for i in hu if ti[i]["stratum"] == "nonnative_learner"]

    print(f"## Realistic batch composition: {len(ai)} AI and {len(hu)} human texts judged in 16%-AI batches\n")
    print(
        f"| rule | recall | recall in 50/50 batches | FPR (n={len(hu)}) | FPR wiki-style (n={len(wiki)}) "
        f"| FPR non-native (n={len(nn)}) | FPR* | P@2% |"
    )
    print("|---|---|---|---|---|---|---|---|")
    rules = {
        "A >= 0.85 (today's rule)": lambda i, a: a >= 0.85,
        "A >= 0.88": lambda i, a: a >= 0.88,
        "A >= 0.90": lambda i, a: a >= 0.90,
        "A >= 0.85 AND register_total >= 1": lambda i, a: a >= 0.85 and fx[i]["d_register_total"] >= 1,
        "A >= 0.90 AND register_total >= 1": lambda i, a: a >= 0.90 and fx[i]["d_register_total"] >= 1,
        "A >= 0.85 AND register_total >= 2": lambda i, a: a >= 0.85 and fx[i]["d_register_total"] >= 2,
        "register_total >= 1 alone (no Jev)": lambda i, a: fx[i]["d_register_total"] >= 1,
        "register_total >= 2 alone (no Jev)": lambda i, a: fx[i]["d_register_total"] >= 2,
    }
    for label, rule in rules.items():
        tpr = sum(1 for i in ai if rule(i, dil[i])) / len(ai)
        tpr50 = sum(1 for i in ai if rule(i, A[i])) / len(ai)
        fpr = sum(1 for i in hu if rule(i, dil[i])) / len(hu)
        fw = sum(1 for i in wiki if rule(i, dil[i])) / len(wiki)
        fn = sum(1 for i in nn if rule(i, dil[i])) / len(nn)
        p2, fub = projected_precision(tpr, fpr, len(hu))
        print(
            f"| {label} | {tpr:.3f} | {tpr50:.3f} | {fpr:.3f} | {fw:.3f} | {fn:.3f} | {fub:.4f} | {p2:.3f} |"
        )
    print(
        f"\nFPR* is the rule-of-three upper bound (3/{len(hu)} = {3.0 / len(hu):.4f}) where no false "
        f"positive was observed, and P@2% is projected with it, exactly as the existing REPORT.md does."
    )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "features"
    if cmd == "features":
        cmd_features()
    elif cmd == "trap-build":
        cmd_trap_build()
    elif cmd == "trap":
        cmd_trap_report()
    elif cmd == "ask-trap":
        cmd_ask_trap(int(sys.argv[2]) if len(sys.argv) > 2 else 700)
    elif cmd == "combine":
        cmd_combine()
    elif cmd == "ask-control":
        cmd_ask_control()
    elif cmd == "ask-retest":
        cmd_ask_retest()
    elif cmd == "ask-dilute":
        cmd_ask_dilute()
    elif cmd == "composition":
        cmd_composition()
    elif cmd == "realistic":
        cmd_realistic()
    else:
        raise SystemExit(f"unknown command {cmd}")
