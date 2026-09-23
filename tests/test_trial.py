"""What is left of the trial in the open package after Stripe took over owning it
(`odd/tasks/free-tier.md`, "the free tier that never calls the model" superseded by Omar's later call: Stripe
owns the subscription lifecycle, including the trial, and this package is a mirror). `Store.start_trial`,
`expire_trial` and `note_trial_warning` are gone — a tenant reaches `plan == "trial"` only because the hosted
webhook wrote it there (`jevmod_hosted/billing.py`), mirroring Stripe's own `trialing` status. This file keeps
exactly what is still this package's job: reading a `trial` plan correctly once it is set, by whatever means,
and opening a database that predates this change without choking on the columns it left behind.

`tests/test_discord_local_commands.py` covers the Discord surface; `jevmod_hosted/tests/test_billing.py` (the
private repository) covers the mirror table itself, status by status, and is where the actual state machine
now lives.
"""

from __future__ import annotations

import sqlite3

import pytest

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
        return [Verdict(m.id, {c: 0.99 for c in categories}, True, "jev") for m in messages]


# ---- migration: a database written before `trial_ends_at`/`trial_warned` existed still opens and reads,
# and one written *with* those columns (every production database, as of this change) still opens too.


def test_a_pre_trial_database_still_opens_and_reads(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY, policy TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
            meta TEXT NOT NULL DEFAULT '{}', quota_notified TEXT DEFAULT '');
        """
    )
    con.execute(
        "INSERT INTO tenants (id, policy, plan) VALUES (?, ?, ?)",
        ("discord:old", '{"actions": {}}', "pro"),
    )
    con.commit()
    con.close()

    store = Store(path)

    assert store.plan("discord:old") == "pro", "a row written before this column existed must still read"


def test_a_database_carrying_the_old_trial_columns_still_opens_and_reads(tmp_path):
    """The column this package used to write is not dropped (see the comment above `PLAN_QUOTAS` in
    `store.py`): a production database still has it, and opening one must not choke on an extra column
    nothing here looks at any more."""
    path = tmp_path / "with_columns.sqlite"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY, policy TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
            meta TEXT NOT NULL DEFAULT '{}', quota_notified TEXT DEFAULT '',
            trial_ends_at REAL, trial_warned INTEGER);
        """
    )
    con.execute(
        "INSERT INTO tenants (id, policy, plan, trial_ends_at) VALUES (?, ?, ?, ?)",
        ("discord:withcol", '{"actions": {}}', "trial", 4_000_000_000.0),
    )
    con.commit()
    con.close()

    store = Store(path)

    assert store.plan("discord:withcol") == "trial"
    assert store.quota_for("discord:withcol") == store.quota_for("discord:withcol")  # does not raise


# ---- quota while on trial: Pro's ceiling, not Free's, however `plan` got set to "trial"


def test_a_trial_tenant_gets_pro_quota(tmp_path):
    """A trial is Pro's judgement, so it gets Pro's number and not the per-tenant cap beside it.

    Asserted against `PRO_MONTHLY` rather than against a literal: the point is that both come from the
    same place, and a test carrying its own copy of the number would pass while they drifted apart."""
    from jevmod.core.store import PRO_MONTHLY, Store

    store = Store(tmp_path / "s.sqlite", monthly_quota=5)
    store.set_plan("t", "trial")

    assert store.quota_for("t") == PRO_MONTHLY, "a trial must not be capped at the per-tenant quota"


# ---- over_budget classifies a trial as free, so trials pause before paying servers


def test_a_trial_tenant_counts_as_free_for_the_budget_ceiling(monkeypatch, tmp_path):
    from jevmod.core import store as store_mod

    monkeypatch.setattr(store_mod, "GLOBAL_BUDGET_USD", 60.0)
    monkeypatch.setattr(store_mod, "TRIAL_BUDGET_USD", 15.0)
    store = store_mod.Store(tmp_path / "s.sqlite")
    store.set_plan("pro-tenant", "pro")
    store.set_plan("trial-tenant", "trial")

    tokens = round(20.0 * 1e6 / store_mod.USD_PER_M_INPUT)  # past the free ceiling, nowhere near the hard one
    store.add_usage("noise", judged=1, requests=1, tokens=tokens)
    store._spend_at = 0.0

    assert store.over_budget("trial-tenant") == "trial_budget", "a trial must pause before paying servers do"
    assert store.over_budget("pro-tenant") is None, "a paying server must not be paused by trial traffic"


# ---- paying_tenants() must not count trials, or a trial would raise the ceiling it spends against


def test_paying_tenants_ignores_trials(monkeypatch, tmp_path):
    from jevmod.core import store as store_mod

    monkeypatch.setattr(store_mod, "GLOBAL_BUDGET_USD", 60.0)
    monkeypatch.setattr(store_mod, "PAID_BUDGET_USD", 100.0)
    store = store_mod.Store(tmp_path / "s.sqlite")
    store.set_plan("pro-tenant", "pro")
    store.set_plan("trial-tenant", "trial")
    store.set_plan("trial-tenant-2", "trial")

    assert store.paying_tenants() == 1, "trials are not subscriptions and must not raise the spend ceiling"
    assert store.budget_ceiling() == pytest.approx(160.0)


# ---- a live trial still reaches the model; local rules run regardless of plan


def test_a_trial_tenant_still_reaches_the_model(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    store.set_plan("t", "trial")
    policy = Policy()
    policy.set_category("spam", "flag")
    store.save_policy("t", policy)

    judge = _CountingJudge()
    service = ModerationService(store=store, judge=judge)
    service.moderate("t", [Message("m1", "hello there, a perfectly normal message")])

    assert judge.seen_texts == ["hello there, a perfectly normal message"]


def test_local_rules_keep_running_once_a_trial_becomes_free(tmp_path):
    """Whatever flips `plan` back to `free` — the webhook mirroring Stripe now, an admin comp, anything — the
    local word list never depended on plan at all and keeps deciding regardless."""
    store = Store(tmp_path / "s.sqlite", monthly_quota=0)
    store.set_plan("t", "trial")
    store.set_plan("t", "inactive")

    policy = Policy()
    policy.set_category("spam", "flag")
    policy.set_words(["nitro"])
    store.save_policy("t", policy)

    judge = _CountingJudge()
    service = ModerationService(store=store, judge=judge)

    decisions = service.moderate(
        "t",
        [Message("m1", "free nitro codes"), Message("m2", "hello there, a perfectly normal message")],
    )

    assert decisions[0].category == "local:word" and decisions[0].reason == "local", "local rules still run"
    assert store.quota_for("t") == 0, "back to inactive, the trial's Pro quota is gone"


def test_an_inactive_tenant_is_judged_by_nothing_at_all(tmp_path, monkeypatch):
    """What replaced the free tier.

    Until 2026-09-21 a server that was not paying sat on `free`: it never reached the model, and its local
    rules kept running, which is what made it a usable free tier. That tier was removed as an offer, and
    `inactive` replaced it. An inactive server gets nothing evaluated, not even the rules that cost nothing,
    and the gate that does it sits above local rules rather than below them.

    `ENFORCE_PLANS` is what keeps this off a self-hosted copy, where every tenant sits on the default plan
    and there is no billing to be inactive from. The test above describes that other deployment.
    """
    import importlib

    monkeypatch.setenv("JEVMOD_ENFORCE_PLANS", "1")
    from jevmod.core import store as store_mod

    importlib.reload(store_mod)
    from jevmod.core import service as service_mod

    importlib.reload(service_mod)
    assert service_mod.ENFORCE_PLANS is True
    try:
        store = store_mod.Store(tmp_path / "inactive.sqlite")
        policy = Policy()
        policy.set_category("spam", "flag")
        policy.set_words(["nitro"])
        store.save_policy("t", policy)

        judge = _CountingJudge()
        service = service_mod.ModerationService(store=store, judge=judge)
        decisions = service.moderate(
            "t",
            [Message("m1", "free nitro codes"), Message("m2", "hello there, a perfectly normal message")],
        )

        assert judge.seen_texts == [], "an inactive tenant must not cost a single token"
        assert [d.reason for d in decisions] == ["inactive", "inactive"]
        assert decisions[0].category is None, "not even the word list runs; that is what changed"
        assert all(d.action == "none" for d in decisions)

        # A trial is not inactive, and reaches the model.
        store.set_plan("t", "trial")
        service.moderate("t", [Message("m3", "hello there, a perfectly normal message")])
        assert judge.seen_texts == ["hello there, a perfectly normal message"]
    finally:
        monkeypatch.delenv("JEVMOD_ENFORCE_PLANS")
        importlib.reload(store_mod)
        importlib.reload(service_mod)


def test_an_inactive_tenant_is_never_granted_unlimited_quota(tmp_path, monkeypatch):
    """The trap this whole change was written around.

    `quota_for` returns 0 for unlimited, and its fallback is `PLAN_QUOTAS.get(plan, 0)`. So a plan name
    that is not in the table gets unlimited judgement rather than none, silently, and the first sign of it
    would be the model bill. Removing the free plan without moving every one of its four touch points
    together is exactly how a tenant ends up in that state.

    This asserts the other direction explicitly: enforced and inactive means a negative quota, which
    `over_quota` reads as none allowed, not as unlimited.
    """
    import importlib

    monkeypatch.setenv("JEVMOD_ENFORCE_PLANS", "1")
    from jevmod.core import store as store_mod

    importlib.reload(store_mod)
    try:
        store = store_mod.Store(tmp_path / "quota.sqlite")
        store.save_policy("t", Policy())

        assert store.plan("t") == "inactive", "the default plan is the one nothing is paying for"
        assert store.quota_for("t") == -1, "none allowed, and not 0, which means unlimited here"
        assert store.over_quota("t") is True
    finally:
        monkeypatch.delenv("JEVMOD_ENFORCE_PLANS")
        importlib.reload(store_mod)


def test_a_self_hosted_copy_ignores_plans_entirely(tmp_path):
    """The default, and the reason the flag exists.

    Self-hosting has no billing and no Stripe, so every tenant sits on the default plan, which is now
    called `inactive`. If that name alone decided anything, removing the free plan would have switched off
    every self-hosted deployment in the world. `ENFORCE_PLANS` is off unless a deployment asks for it, and
    this asserts that the name is inert without it.
    """
    from jevmod.core import service as service_mod
    from jevmod.core import store as store_mod

    assert store_mod.ENFORCE_PLANS is False, "plans must not be enforced by default"

    store = store_mod.Store(tmp_path / "selfhost.sqlite")
    policy = Policy()
    policy.set_category("spam", "flag")
    store.save_policy("t", policy)
    assert store.plan("t") == "inactive"
    assert store.quota_for("t") != -1, "a self-hosted tenant is not gated by a plan it never chose"

    judge = _CountingJudge()
    service = service_mod.ModerationService(store=store, judge=judge)
    service.moderate("t", [Message("m1", "hello there, a perfectly normal message")])
    assert judge.seen_texts == ["hello there, a perfectly normal message"]


def test_only_the_new_variable_decides_whether_plans_are_enforced(monkeypatch):
    """`JEVMOD_MODEL_ON_FREE` was read as a fallback for a day and is not read any more.

    It lived in a file on the VPS that is in no repository and is the only copy of itself, so the rename
    shipped with the old name still working and the box was updated afterwards, on 2026-09-22, in its own
    `.env` and in the compose file. Both were verified on the box before this fallback came out.

    What this asserts is the shape that replaced it: off unless a deployment says otherwise, and the old
    name doing nothing at all. A deployment that still carries only the old name judges every lapsed
    tenant for free, so if that name ever comes back as a silent fallback, this is what notices.
    """
    import importlib

    from jevmod.core import store as store_mod

    try:
        monkeypatch.delenv("JEVMOD_ENFORCE_PLANS", raising=False)
        monkeypatch.delenv("JEVMOD_MODEL_ON_FREE", raising=False)
        importlib.reload(store_mod)
        assert store_mod.ENFORCE_PLANS is False, "unset must mean off: a self-hosted copy judges everything"

        monkeypatch.setenv("JEVMOD_MODEL_ON_FREE", "0")
        importlib.reload(store_mod)
        assert store_mod.ENFORCE_PLANS is False, "the old name is retired and must not enforce anything"

        monkeypatch.setenv("JEVMOD_ENFORCE_PLANS", "1")
        importlib.reload(store_mod)
        assert store_mod.ENFORCE_PLANS is True

        monkeypatch.setenv("JEVMOD_ENFORCE_PLANS", "0")
        importlib.reload(store_mod)
        assert store_mod.ENFORCE_PLANS is False
    finally:
        monkeypatch.delenv("JEVMOD_MODEL_ON_FREE", raising=False)
        monkeypatch.delenv("JEVMOD_ENFORCE_PLANS", raising=False)
        importlib.reload(store_mod)
