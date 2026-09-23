"""The judgment core: a batch of messages in, one Jev request, a probability per category per message out.

Cost controls live here, not in the bot: local pre-filters decide what is worth judging, a cache reuses verdicts
for repeated text, and only the categories a server enabled are asked.

Findings from the adversarial red team that shaped this file (tests/data/redteam.csv):
- Messages go to Jev as a dict keyed by position (`messages.m3.text`), not a list. With a list, probabilities
  leaked between neighbouring positions in multilingual batches (a clean German message inherited harassment 0.95).
- Text is NFKC-normalised and stripped of combining marks before judging and caching: fullwidth, enclosed
  alphanumerics and zalgo were slipping under the thresholds or the pre-filter.
- The pre-filter counts characters, not space-separated words: Japanese and Chinese never have spaces.
- Every question carries `criteria`, otherwise "offtopic" measured "undesirable" and "spam" caught one-off sales.

Against the TypeSafe docs (checked 2026-09-18): one Noul per hazard with true/false criteria is the pattern of the
`llm_guardrails` cookbook. Judging several messages in one request is NOT a documented pattern (the cookbooks send
one item per request, or many questions about one document); it is our own cost trade-off, kept only because
`tests/test_redteam.py::test_batch_matches_single_verdicts_in_multilingual_batch` shows batch and single verdicts
agree once the state is a dict. 429/529 are retried with exponential backoff and Retry-After, as the API asks.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typesafe_sdk import Noul, NoulAnswer, RetryPolicy, TypeSafeClient

from .keys import get_api_key

# The questions live in categories.json so every implementation (Python, npm, MCP) asks Jev exactly the same thing.
CATEGORIES: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).with_name("categories.json")).read_text(encoding="utf-8")
)["categories"]

LINK_RE = re.compile(
    r"(https?://|hxxps?://|www\.|\S+\[\.\]\S+|\b[\w-]+\.(?:gg|com|net|org|ru|io|xyz|fr|de|jp|br)\b/?)", re.I
)


# Enclosed Alphanumeric Supplement (🄰 🅐 🅰 …) has no NFKC decomposition; map the three A-Z rows by hand.
_ENCLOSED = {cp + k: chr(ord("A") + k) for cp in (0x1F130, 0x1F150, 0x1F170) for k in range(26)}


def normalize(text: str) -> str:
    """HTML entities → text (scraped comments carry `&#39;`), NFKC (fullwidth, enclosed letters, ligatures → plain),
    drop combining marks (zalgo), collapse whitespace."""
    t = unicodedata.normalize("NFKC", html.unescape(text).translate(_ENCLOSED))
    # drop combining marks (zalgo) and format characters (zero-width spaces/joiners used to split words, BOM)
    t = "".join(ch for ch in t if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf")
    return " ".join(t.split())


@dataclass
class Message:
    id: str
    text: str
    author: str = ""
    channel_topic: str = ""
    author_trusted: bool = False
    # Which channel this was said in, when the platform has more than one under a single tenant.
    # Only Discord does: a guild is the tenant and its channels are separate conversations, so
    # without this the context window would paste #general under a message in #support. Telegram,
    # Reddit, Twitch and YouTube each make the tenant and the conversation the same thing and leave
    # it empty. Local only, like `author`: it never reaches Jev.
    channel: str = ""
    # What was said in this channel just before, oldest first. Filled by `ModerationService` from
    # `core.context.ConversationBuffer`, so every adapter gets it without changing. Message text
    # only: an author name here would break the promise in AGENTS.md and the privacy notice, and
    # the buffer that fills it cannot hold one.
    context: tuple[str, ...] = ()


@dataclass
class Verdict:
    message_id: str
    scores: dict[str, float]  # category -> probability
    judged: bool  # False when a pre-filter skipped Jev
    reason: str = ""  # why it was skipped, or "cache" / "jev"
    custom: dict[str, float] = field(default_factory=dict)  # server-defined rules -> probability

    def top(self) -> tuple[str, float] | None:
        allscores = {**self.scores, **self.custom}
        if not allscores:
            return None
        k = max(allscores, key=lambda c: allscores[c])
        return k, allscores[k]


def prefilter(m: Message, min_chars: int = 8) -> str | None:
    """Return a reason to skip judging, or None to judge. Counts letters/digits in any script, not words."""
    if m.author_trusted:
        return "trusted author"
    text = normalize(m.text)
    if not text:
        return "empty"
    if LINK_RE.search(text):
        return None  # a link is never too short
    letters = sum(1 for ch in text if ch.isalnum())
    if letters < min_chars:
        return "too short"
    return None


class Judge:
    def __init__(self, client: TypeSafeClient | None = None, cache_ttl_s: int = 86400, timeout_s: float = 20.0) -> None:
        self.client = client or TypeSafeClient(
            api_key=get_api_key(),
            retry=RetryPolicy(
                max_retries=3, backoff_initial=0.5, backoff_max=8.0, http_statuses={429, 500, 502, 503, 504, 529}
            ),
            timeout=timeout_s,
        )
        self.cache: dict[str, tuple[float, dict[str, float], dict[str, float]]] = {}
        self.cache_ttl = cache_ttl_s
        self.requests = 0
        self.input_tokens = 0
        self.judged_messages = 0

    def judge(
        self,
        messages: list[Message],
        categories: list[str],
        custom_rules: dict[str, str] | None = None,
    ) -> list[Verdict]:
        """One Jev request for every message that passes the pre-filter and is not cached."""
        custom_rules = custom_rules or {}
        cats = [c for c in categories if c in CATEGORIES]
        out: dict[str, Verdict] = {}
        to_judge: list[tuple[Message, str]] = []
        now = time.time()
        for m in messages:
            why = prefilter(m)
            if why:
                out[m.id] = Verdict(m.id, {}, False, why)
                continue
            text = normalize(m.text)
            key = _key(text, m.channel_topic, cats, custom_rules, m.context)
            hit = self.cache.get(key)
            if hit and now - hit[0] < self.cache_ttl:
                out[m.id] = Verdict(m.id, dict(hit[1]), True, "cache", dict(hit[2]))
                continue
            to_judge.append((m, text))

        if to_judge and (cats or custom_rules):
            # Only message text and the channel topic reach Jev: no author names, no ids beyond the
            # position. Context is other people's message text, which is the same kind of data and
            # not a new one, so the promise in AGENTS.md and the privacy notice still holds exactly
            # as written. Sending anything about the author is a different decision, gated on
            # JEV-20 to JEV-22, and is not this.
            state: dict[str, Any] = {
                "messages": {
                    f"m{i}": {
                        "text": text,
                        "channel_topic": m.channel_topic or "general chat",
                        # Keyed by position like the messages themselves, and for consistency rather
                        # than for the original reason: the list-versus-dict finding above is about
                        # positions that carry questions, and no question points at a context entry.
                        # A dict costs a handful of tokens and keeps one rule in this file instead
                        # of two. Omitted entirely when empty, so a message with no history reaches
                        # Jev in exactly the shape it did before this existed.
                        **({"context": {f"c{j}": c for j, c in enumerate(m.context)}} if m.context else {}),
                    }
                    for i, (m, text) in enumerate(to_judge)
                },
                "custom_rules": custom_rules,
            }
            questions: dict[str, Noul] = {}
            for i, _ in enumerate(to_judge):
                path = f"messages.m{i}"
                for c in cats:
                    questions[f"{c}_{i}"] = Noul(
                        instructions=CATEGORIES[c]["instructions"].format(m=path), criteria=CATEGORIES[c]["criteria"]
                    )
                for name, rule in custom_rules.items():
                    questions[f"custom__{name}_{i}"] = Noul(
                        instructions=f"Does `{path}.text` break this community rule: `custom_rules.{name}` ({rule!r})?",
                        criteria={
                            "true": "the message does what the rule forbids, as a moderator who wrote it would read it",
                            "false": "the message is ordinary conversation, or the rule does not clearly cover it; "
                            "when the rule lists exceptions, those are allowed",
                        },
                    )
            resp = self.client.system_one(state=state, questions=questions)
            self.requests += 1
            self.input_tokens += getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
            self.judged_messages += len(to_judge)
            for i, (m, text) in enumerate(to_judge):
                scores = {c: _p(resp.answers[f"{c}_{i}"]) for c in cats}
                custom = {name: _p(resp.answers[f"custom__{name}_{i}"]) for name in custom_rules}
                self.cache[_key(text, m.channel_topic, cats, custom_rules, m.context)] = (now, scores, custom)
                out[m.id] = Verdict(m.id, scores, True, "jev", custom)
        elif to_judge:
            for m, _ in to_judge:
                out[m.id] = Verdict(m.id, {}, False, "no categories enabled")
        return [out[m.id] for m in messages]


def _p(answer: Any) -> float:
    if not isinstance(answer, NoulAnswer):
        raise TypeError(f"expected a Noul answer, got {type(answer).__name__}")
    return float(answer.noul)


def _key(text: str, topic: str, cats: list[str], rules: dict[str, str], context: tuple[str, ...] = ()) -> str:
    """The cache key. The context is part of it, and that costs hit rate on purpose.

    Without it, a verdict computed while one conversation was happening is handed back during
    another. That was always a little untrue and is now measured: JEV-56 found that regrouping the
    same messages into different batches moves 12% of spam positives across their threshold, against
    2.7% for a request repeated unchanged (`benchmark/BATCH_EFFECT.md`). The old key asserted that
    two scorings of the same text are interchangeable, and they are not.

    The hit rate is bought back by what a cache is actually for here. Its job is the spam wave: the
    same text posted forty times in a minute. Those forty arrive in near-identical windows, so they
    still share a key. What no longer shares a key is the same text a day later in a different
    conversation, which is exactly the hit that was wrong.
    """
    norm = text.lower()
    ctx = "␟".join(context)  # a symbol no message text contains, so two windows cannot collide
    h = hashlib.sha256(f"{norm}|{topic}|{','.join(cats)}|{sorted(rules.items())}|{ctx}".encode()).hexdigest()
    return h[:32]
