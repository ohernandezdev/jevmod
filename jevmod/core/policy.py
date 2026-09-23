"""Policy: turn a Verdict (probabilities) into a Decision (what to do), per tenant. Pure code, offline-testable."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from ..judge import CATEGORIES, Verdict

ACTIONS = ("off", "flag", "delete", "timeout")  # ordered by severity
LINK_MODES = ("off", "invites", "allowlist", "all")
# A pattern is refused at write time rather than discovered at moderation time. The probe is close to the
# spec's 2 KB and deliberately ends on a character the pattern cannot consume, so a classic catastrophic
# backtracker (e.g. `(a+)+$`) is forced through its worst case instead of matching on the first attempt.
_PATTERN_MAX_MATCH_S = 0.05
# The match runs in a throwaway interpreter, not a thread and not a `multiprocessing` child.
#
# Not a thread, because CPython's `re` engine does not release the GIL while backtracking: a thread stuck in
# a catastrophic match freezes the whole interpreter rather than leaking one thread. Only a real process can
# be killed out of that.
#
# Not `multiprocessing`, because its `spawn` start method re-imports the caller's `__main__` in the child.
# Measured: a script that calls this runs its own top level twice. In production the caller's `__main__` is
# the Discord bot, so validating a pattern would have started a second bot. The child here is
# `sys.executable -c`, which imports `re` and nothing of ours.
_PATTERN_PROBE_SRC = """import re, sys, time
pattern = sys.argv[1]
probe = "a" * 2000 + "!"
t0 = time.perf_counter()
re.compile(pattern).search(probe)
print(time.perf_counter() - t0)
"""
# Generous, because it also covers interpreter start-up. The 50ms figure is still the one enforced, against
# the time the child actually spent inside `search()`.
_PATTERN_PROCESS_TIMEOUT_S = 5.0
DEFAULT_THRESHOLDS = {
    "spam": 0.85,
    "scam": 0.75,
    "harassment": 0.75,
    "nsfw": 0.8,
    "offtopic": 0.9,
    # 0.50, and the number comes from a loss ratio Omar chose rather than from F1. Asked how many
    # false positives a missed cry for help is worth, the answer was fifty. Minimising 50*FN + FP on
    # the 51 labelled rows in `benchmark/data/items.jsonl` puts the line at 0.04, which would flag
    # 121 messages of 2,531 and bury the real ones in a queue nobody finishes: a loss function that
    # ignores a moderator's attention says "flag everything" and has to be read with that in mind.
    #
    # 0.50 is the lowest the product can express -- `_clamp` below and the site's own control both
    # floor there -- and it is also where the loss stops falling within that range. Against the 0.80
    # this replaces, measured on the same rows: 24 misses become 14, and 3 false positives become 8.
    # Recall 0.529 to 0.725, on an AUROC of 0.994, so the ordering was always good enough and the
    # line was simply in the wrong place. `benchmark/EVAL.md` section 4, JEV-59.
    "selfharm": 0.5,
    "doxxing": 0.8,
    "minors": 0.7,
    "ai_generated": 0.85,  # experimental, see benchmark/ai_detect/REPORT.md
}
# selfharm is flag-only by design: moderators should reach out, not punish. doxxing/minors flag by default;
# communities that want automatic removal set delete/timeout explicitly.
EXPERIMENTAL = ("ai_generated",)  # never moved by the feedback loop until measured on real traffic
DEFAULT_ACTIONS = {
    "spam": "flag",
    "scam": "flag",
    "harassment": "flag",
    "nsfw": "flag",
    "offtopic": "off",
    "selfharm": "flag",
    "doxxing": "flag",
    "minors": "flag",
    "ai_generated": "off",  # opt-in: precision falls to about 0.2 at a realistic base rate
}
RULE_THRESHOLD = 0.8
POLICY_VERSION = 1


@dataclass
class Policy:
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    actions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTIONS))
    rules: dict[str, str] = field(default_factory=dict)  # name -> natural-language rule
    rule_actions: dict[str, str] = field(default_factory=dict)  # name -> action (default flag)
    rule_thresholds: dict[str, float] = field(default_factory=dict)
    timeout_minutes: int = 10
    # Local rules: zero cost per message, evaluated by jevmod.core.local before any model call.
    link_mode: str = "off"  # off | invites | allowlist | all
    link_action: str = "flag"
    link_allowlist: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    word_action: str = "flag"
    patterns: dict[str, str] = field(default_factory=dict)  # name -> regex
    pattern_actions: dict[str, str] = field(default_factory=dict)  # name -> action
    raid_joins: int = 0  # 0 = off; else 3-100 joins per 60s
    raid_repeats: int = 0  # 0 = off; else 3-50 identical messages per 60s
    raid_action: str = "flag"

    def enabled_categories(self) -> list[str]:
        return [c for c in CATEGORIES if self.actions.get(c, "off") != "off"]

    def active(self) -> bool:
        # True the moment any local rule exists too, even with every category off and no natural-language
        # rule: a server on Free with the model off must still get its link filter and word list run.
        return bool(
            self.enabled_categories()
            or self.rules
            or self.link_mode != "off"
            or self.words
            or self.patterns
            or self.raid_joins
            or self.raid_repeats
        )

    def set_category(self, category: str, action: str, threshold: float | None = None) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}; one of {', '.join(CATEGORIES)}")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}")
        if category in EXPERIMENTAL and action not in ("off", "flag"):
            raise ValueError(
                f"{category} is experimental and can only be off or flag: its precision on a real community's "
                "traffic is too low to remove a message or time a member out"
            )
        self.actions[category] = action
        if threshold is not None:
            self.thresholds[category] = _clamp(threshold)

    def set_rule(self, name: str, text: str | None, action: str = "flag", threshold: float | None = None) -> None:
        name = name.strip().lower().replace(" ", "_")[:30]
        if text is None or not text.strip():
            self.rules.pop(name, None)
            self.rule_actions.pop(name, None)
            self.rule_thresholds.pop(name, None)
            return
        if len(self.rules) >= 5 and name not in self.rules:
            raise ValueError("up to 5 custom rules")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        self.rules[name] = text.strip()[:200]
        self.rule_actions[name] = action
        if threshold is not None:
            self.rule_thresholds[name] = _clamp(threshold)

    def set_link_mode(self, mode: str) -> None:
        if mode not in LINK_MODES:
            raise ValueError(f"unknown link_mode {mode!r}; one of {', '.join(LINK_MODES)}")
        self.link_mode = mode

    def set_link_action(self, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}")
        self.link_action = action

    def set_link_allowlist(self, domains: list[str]) -> None:
        if len(domains) > 25:
            raise ValueError("link_allowlist accepts up to 25 domains")
        for d in domains:
            if len(d) > 253:
                raise ValueError(f"domain {d!r} is longer than 253 characters")
        self.link_allowlist = list(domains)

    def set_words(self, words: list[str]) -> None:
        if len(words) > 100:
            raise ValueError("words accepts up to 100 entries")
        for w in words:
            if not 2 <= len(w) <= 64:
                raise ValueError(f"word {w!r} must be 2 to 64 characters")
        self.words = list(words)

    def set_word_action(self, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}")
        self.word_action = action

    def set_pattern(self, name: str, pattern: str | None, action: str = "flag") -> None:
        name = name.strip().lower().replace(" ", "_")[:30]
        if pattern is None or not pattern.strip():
            self.patterns.pop(name, None)
            self.pattern_actions.pop(name, None)
            return
        if len(self.patterns) >= 5 and name not in self.patterns:
            raise ValueError("up to 5 patterns")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}")
        _validate_pattern(pattern)
        self.patterns[name] = pattern
        self.pattern_actions[name] = action

    def set_raid(self, joins: int | None = None, repeats: int | None = None, action: str | None = None) -> None:
        if joins is not None:
            if joins != 0 and not (3 <= joins <= 100):
                raise ValueError("raid_joins must be 0 (off) or 3 to 100")
            self.raid_joins = joins
        if repeats is not None:
            if repeats != 0 and not (3 <= repeats <= 50):
                raise ValueError("raid_repeats must be 0 (off) or 3 to 50")
            self.raid_repeats = repeats
        if action is not None:
            if action not in ACTIONS:
                raise ValueError(f"unknown action {action!r}; one of {', '.join(ACTIONS)}")
            self.raid_action = action

    def nudge(self, category: str, delta: float = 0.03) -> float:
        """False-positive feedback: raise that category's threshold a notch.

        Experimental categories are not moved: their error rate has not been measured on a real community's
        traffic, so a handful of reactions would move the line on noise."""
        if category in EXPERIMENTAL:
            return self.thresholds.get(category, DEFAULT_THRESHOLDS.get(category, 0.9))
        if category.startswith("rule:"):
            name = category[5:]
            cur = self.rule_thresholds.get(name, RULE_THRESHOLD)
            self.rule_thresholds[name] = _clamp(cur + delta)
            return self.rule_thresholds[name]
        cur = self.thresholds.get(category, 0.9)
        self.thresholds[category] = _clamp(cur + delta)
        return self.thresholds[category]

    def to_dict(self) -> dict[str, Any]:
        return {
            "thresholds": self.thresholds,
            "actions": self.actions,
            "rules": self.rules,
            "rule_actions": self.rule_actions,
            "rule_thresholds": self.rule_thresholds,
            "timeout_minutes": self.timeout_minutes,
            "link_mode": self.link_mode,
            "link_action": self.link_action,
            "link_allowlist": self.link_allowlist,
            "words": self.words,
            "word_action": self.word_action,
            "patterns": self.patterns,
            "pattern_actions": self.pattern_actions,
            "raid_joins": self.raid_joins,
            "raid_repeats": self.raid_repeats,
            "raid_action": self.raid_action,
            "version": POLICY_VERSION,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Policy:
        p = cls()
        for k in ("thresholds", "actions", "rules", "rule_actions", "rule_thresholds", "patterns", "pattern_actions"):
            if isinstance(d.get(k), dict):
                getattr(p, k).update(d[k])
        if "timeout_minutes" in d:
            p.timeout_minutes = int(d["timeout_minutes"])
        if "link_mode" in d:
            p.link_mode = d["link_mode"]
        if "link_action" in d:
            p.link_action = d["link_action"]
        if isinstance(d.get("link_allowlist"), list):
            p.link_allowlist = list(d["link_allowlist"])
        if isinstance(d.get("words"), list):
            p.words = list(d["words"])
        if "word_action" in d:
            p.word_action = d["word_action"]
        if "raid_joins" in d:
            p.raid_joins = int(d["raid_joins"])
        if "raid_repeats" in d:
            p.raid_repeats = int(d["raid_repeats"])
        if "raid_action" in d:
            p.raid_action = d["raid_action"]
        return p


@dataclass
class Decision:
    message_id: str
    action: str  # "none" | "flag" | "delete" | "timeout"
    category: str | None  # winning category or "rule:<name>"
    probability: float
    scores: dict[str, float]  # everything Jev returned, for the audit log
    judged: bool
    reason: str  # "jev" | "cache" | pre-filter reason | "quota" | "error_open"
    policy_version: int = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "action": self.action,
            "category": self.category,
            "probability": round(self.probability, 4),
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "judged": self.judged,
            "reason": self.reason,
            "policy_version": self.policy_version,
        }


def decide(policy: Policy, v: Verdict) -> Decision:
    """The most severe action whose threshold is crossed wins; ties go to the higher probability."""
    scores = {**v.scores, **{f"rule:{n}": p for n, p in v.custom.items()}}
    if not v.judged:
        return Decision(v.message_id, "none", None, 0.0, scores, False, v.reason)
    hits: list[tuple[str, float, str]] = []
    for c, p in v.scores.items():
        action = policy.actions.get(c, "off")
        if action != "off" and p >= policy.thresholds.get(c, 1.0):
            hits.append((c, p, action))
    for n, p in v.custom.items():
        action = policy.rule_actions.get(n, "flag")
        if action != "off" and p >= policy.rule_thresholds.get(n, RULE_THRESHOLD):
            hits.append((f"rule:{n}", p, action))
    if not hits:
        return Decision(v.message_id, "none", None, 0.0, scores, True, v.reason)
    category, p, action = max(hits, key=lambda h: (ACTIONS.index(h[2]), h[1]))
    return Decision(v.message_id, action, category, p, scores, True, v.reason)


def _clamp(x: float) -> float:
    return max(0.5, min(0.99, round(float(x), 2)))



def _validate_pattern(pattern: str) -> None:
    """Refuse a pattern that fails to compile, is longer than 200 characters, or is too slow against a 2 KB
    probe. See the comment on `_PATTERN_PROCESS_TIMEOUT_S` for why this runs in a subprocess rather than a
    thread or a signal-based timeout."""
    if len(pattern) > 200:
        raise ValueError(f"pattern is longer than 200 characters: {pattern[:40]!r}...")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"pattern {pattern!r} does not compile: {exc}") from exc
    try:
        done = subprocess.run(
            [sys.executable, "-c", _PATTERN_PROBE_SRC, pattern],
            capture_output=True,
            text=True,
            timeout=_PATTERN_PROCESS_TIMEOUT_S,
        )
        elapsed = float(done.stdout.strip()) if done.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError):
        # The timeout is the catastrophic case itself; a ValueError means the child printed something that
        # was not a duration, which is the same answer: this pattern is not one we will run.
        elapsed = None
    if elapsed is None or elapsed > _PATTERN_MAX_MATCH_S:
        raise ValueError(
            f"pattern {pattern!r} took more than {int(_PATTERN_MAX_MATCH_S * 1000)}ms against a 2 KB probe "
            "string (catastrophic backtracking?)"
        )
