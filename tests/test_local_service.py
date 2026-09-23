"""ModerationService rewired so local rules run before every cost gate (L2).

Today `over_quota` and `over_budget` return early with no decisions at all, so a server that had run out
would also lose its link filter — the opposite of the free-tier plan. These assert the new order: local
rules decide first, cost-gated model judging only ever sees what is left.
"""

from __future__ import annotations

from jevmod.core.policy import Policy
from jevmod.core.service import ModerationService
from jevmod.core.store import Store
from jevmod.judge import Message, Verdict


class _CountingJudge:
    """A fake Judge: no network, records exactly which messages it was asked to judge."""

    def __init__(self):
        self.seen_texts: list[str] = []
        self.requests = 0
        self.input_tokens = 0
        self.judged_messages = 0

    def judge(self, messages, categories, custom_rules=None, padding=()):
        self.seen_texts.extend(m.text for m in messages)
        self.judged_messages += len(messages)
        return [Verdict(m.id, {}, True, "jev") for m in messages]


def test_local_rules_run_even_when_over_quota_and_over_budget(monkeypatch, tmp_path):
    """The one test the spec names by name: a tenant that is both over quota and over the spend ceiling
    must still get its word list applied, because that is the whole point of running local rules first —
    and it is the thing a careless refactor of the gate order would silently break."""
    from jevmod.core import store as store_mod

    monkeypatch.setattr(store_mod, "GLOBAL_BUDGET_USD", 0.01)
    monkeypatch.setattr(store_mod, "TRIAL_BUDGET_USD", 0.01)
    store = store_mod.Store(tmp_path / "s.sqlite", monthly_quota=1)
    store.add_usage("t", 10, 10, 10_000_000)  # comfortably over both the quota and the spend ceiling
    store._spend_at = 0.0
    assert store.over_quota("t")
    assert store.over_budget("t") is not None

    policy = Policy()
    policy.set_words(["nitro"])
    store.save_policy("t", policy)

    service = ModerationService(store=store, judge=_CountingJudge())
    decisions = service.moderate("t", [Message(id="m1", text="free nitro codes")])

    assert decisions[0].reason == "local"
    assert decisions[0].judged is False
    assert decisions[0].category == "local:word"
    assert store.recent_decisions("t"), "a local decision is logged like any other decision"


def test_a_local_hit_does_not_reach_the_model(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    policy = Policy()
    policy.set_category("spam", "flag")
    policy.set_words(["nitro"])
    store.save_policy("t", policy)
    judge = _CountingJudge()
    service = ModerationService(store=store, judge=judge)

    decisions = service.moderate(
        "t",
        [Message("m1", "free nitro codes"), Message("m2", "hello there, a normal message")],
    )

    assert judge.seen_texts == ["hello there, a normal message"], "the local hit must never reach the judge"
    assert decisions[0].reason == "local" and decisions[0].category == "local:word"
    assert decisions[1].reason == "jev"


def test_policy_active_from_local_rules_alone_does_not_short_circuit(tmp_path):
    """policy.active() has to become true from a local rule with every category off, or moderate() takes the
    'policy inactive' exit before local.check ever runs."""
    store = Store(tmp_path / "s.sqlite")
    policy = Policy()
    policy.set_link_mode("all")  # no categories, no natural-language rules
    store.save_policy("t", policy)
    service = ModerationService(store=store, judge=_CountingJudge())

    decisions = service.moderate("t", [Message("m1", "see http://example.com")])

    assert decisions[0].reason == "local"
    assert decisions[0].category == "local:link"


def test_message_order_is_preserved_across_local_and_judged_decisions(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    policy = Policy()
    policy.set_category("spam", "flag")
    policy.set_words(["nitro"])
    store.save_policy("t", policy)
    judge = _CountingJudge()
    service = ModerationService(store=store, judge=judge)

    messages = [
        Message("m1", "ordinary message one"),
        Message("m2", "free nitro codes"),
        Message("m3", "ordinary message two"),
    ]
    decisions = service.moderate("t", messages)

    assert [d.message_id for d in decisions] == ["m1", "m2", "m3"]
    assert decisions[1].reason == "local"
    assert decisions[0].reason == "jev" and decisions[2].reason == "jev"
