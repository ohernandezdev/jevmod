"""Tenant policies, usage counters, an optional monthly quota and the audit log, in SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..judge import Message
from .policy import Decision, Policy

# Optional cost guard per tenant per month. 0 (the default) means unlimited; set JEVMOD_MONTHLY_QUOTA=5000 to pause
# judging for a tenant after 5,000 judged messages in a calendar month (nothing is deleted while paused).
FREE_MONTHLY = int(os.environ.get("JEVMOD_MONTHLY_QUOTA", "0") or 0)
# The plan a tenant is on when nothing is paying for it: never subscribed, trial expired, or subscription
# no longer active. It replaced "free" on 2026-09-21, when the free plan was removed as an offer; see
# `odd/tasks/remove-free-plan.md` in the hosted repository.
INACTIVE = "inactive"
# Whether plans mean anything in this deployment.
#
# Off by default, and that default is what keeps a self-hosted copy working. Self-hosting has no billing,
# no Stripe and no plans: every tenant sits on the default plan, and with this off, that fact is ignored
# and everything is judged. The hosted service sets JEVMOD_ENFORCE_PLANS=1, and only there does a plan
# decide whether a message is looked at.
#
# It was previously spelled JEVMOD_MODEL_ON_FREE, inverted, and named after the plan it happened to gate
# rather than after what it does. That name was read as a fallback between 2026-09-21 and 2026-09-22,
# because the hosted service carried it in a file on the VPS that exists in no repository and is the only
# copy of itself: renaming the variable in code alone would have left the new one unset, defaulted it to
# off, and quietly stopped enforcing plans, with the first sign being the bill. The VPS now sets the new
# name, in its own `.env` and in the compose file, both verified on the box, so the fallback is gone.
def _enforce_plans() -> bool:
    """Whether plans mean anything in this deployment. Off unless a deployment says otherwise."""
    return os.environ.get("JEVMOD_ENFORCE_PLANS", "0") != "0"


ENFORCE_PLANS = _enforce_plans()
# Hosted plans: judged messages per tenant per month. 0 = unlimited. Plan names: "inactive", "trial",
# "pro", "unlimited" (comped). "trial" has no entry of its own: `quota_for` gives it Pro's number directly,
# because a trial *is* Pro's judgement and a second place to keep in sync is a second place to get wrong.
#
# Pro's number is a constant rather than an environment variable. It was `JEVMOD_PRO_MONTHLY_QUOTA`, and
# the hosted service set it to 50000, which is the default it already had: a variable whose only
# deployment sets it to its own default is a knob that does nothing but can still be turned by accident.
# There is one paid plan, so there is one number, and it lives with the code that uses it.
#
# `plan` itself is no longer a state machine this package owns: on the hosted service it is written only by
# the Stripe webhook, which mirrors the subscription's own status (`jevmod_hosted/billing.py`). A self-hosted
# copy never sees anything but the default plan, and ENFORCE_PLANS being off is what makes that harmless.
PRO_MONTHLY = 50_000
PLAN_QUOTAS: dict[str, int] = {
    INACTIVE: FREE_MONTHLY,
    "pro": PRO_MONTHLY,
    "unlimited": 0,
}

# What the whole service may spend on the model in a calendar month, across every tenant at once.
#
# The per-tenant quota above caps one server and says nothing about the sum, so fifty paying servers at
# their own ceiling already cost more than a single operator budgeted for. That is the success case, not an
# abuse case, and it is the one nothing was watching.
#
# Two ceilings rather than one, because who gets stopped matters. Tenants on a trial pause at the lower
# figure, so the people who pay are not moderated worse because the people who have not started paying yet
# have been busy. The hard ceiling stops everybody, and reaching it is an operator failure to be alerted on
# rather than a state to live in.
#
# The lower one was JEVMOD_FREE_BUDGET_USD and its reason was `free_budget`, from when a free plan existed.
# It has gated trials since that plan was removed, so it is named for what it does now.
# 0 disables a ceiling, which is what a self-hosted copy paying its own model bill wants.
# The hard ceiling is a floor plus what the paying servers have already funded. A fixed figure pauses a
# customer who paid this month because other customers who also paid were busy, which is the wrong failure:
# every Pro subscription brings its own money, so it should bring its own allowance with it. Set
# JEVMOD_PAID_BUDGET_USD to the model cost of one subscription's full quota, under what that subscription
# nets after card fees. At 0 it is off and the ceiling is the flat figure it has always been.
GLOBAL_BUDGET_USD = float(os.environ.get("JEVMOD_GLOBAL_BUDGET_USD", "0") or 0)
TRIAL_BUDGET_USD = float(os.environ.get("JEVMOD_TRIAL_BUDGET_USD", "0") or 0)
PAID_BUDGET_USD = float(os.environ.get("JEVMOD_PAID_BUDGET_USD", "0") or 0)
USD_PER_M_INPUT = 0.042  # Jev list price per million input tokens
# The fraction of a ceiling past which the spend figure stops being cached. See `over_budget`.
NEAR_CEILING = 0.8


class Store:
    def __init__(
        self,
        path: str | Path = "jevmod.sqlite",
        keep_text_chars: int | None = None,
        retention_days: int = 30,
        monthly_quota: int | None = None,
    ) -> None:
        """keep_text_chars=0 stores no message text at all. Decisions older than retention_days are purged on
        every write and by `purge_expired()`, which the service also calls on every batch."""
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        # The bot, the API and the Stripe webhook are separate processes on one file. `self.lock` only
        # serialises this process; across processes SQLite's own locking is all there is, and its defaults
        # are wrong for that: the rollback journal blocks readers while anyone writes, and a busy timeout of
        # zero raises "database is locked" on the first collision instead of waiting. WAL lets readers run
        # during a write, and five seconds is long enough for any write this schema does.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")  # safe under WAL: a crash loses no committed transaction
        self.lock = threading.Lock()
        self._spend = 0.0        # month-to-date spend, cached; see month_spend_usd
        self._spend_at = 0.0
        self._paying = 0         # paid-plan tenants, cached; see paying_tenants
        self._paying_at = 0.0
        self.keep_text_chars = (
            int(os.environ.get("JEVMOD_KEEP_TEXT_CHARS", "300")) if keep_text_chars is None else keep_text_chars
        )
        self.retention_days = retention_days
        self.monthly_quota = FREE_MONTHLY if monthly_quota is None else monthly_quota  # 0 = unlimited
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY, policy TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'inactive',
                    meta TEXT NOT NULL DEFAULT '{}', quota_notified TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS usage (
                    tenant TEXT, month TEXT, judged INTEGER, requests INTEGER, tokens INTEGER,
                    PRIMARY KEY (tenant, month));
                CREATE TABLE IF NOT EXISTS decisions (
                    ts REAL, tenant TEXT, rid TEXT, message TEXT, author TEXT, channel TEXT,
                    category TEXT, p REAL, action TEXT, scores TEXT, text TEXT);
                CREATE INDEX IF NOT EXISTS decisions_tenant_ts ON decisions (tenant, ts);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    tenant TEXT PRIMARY KEY, customer_id TEXT, subscription_id TEXT, price_id TEXT, status TEXT,
                    current_period_end REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY, tenant TEXT NOT NULL, created REAL, label TEXT);
                CREATE TABLE IF NOT EXISTS labels (
                    ts REAL, tenant TEXT, message TEXT, category TEXT, agreed INTEGER,
                    scores TEXT, source TEXT);
                CREATE INDEX IF NOT EXISTS labels_tenant_ts ON labels (tenant, ts);
                CREATE TABLE IF NOT EXISTS cancellation_queue (
                    subscription_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, requested_at REAL NOT NULL);
                """
            )
            self.db.commit()
            # `trial_ends_at` and `trial_warned` used to live here, added for a trial this package ran itself.
            # That state machine is gone (see the mirror-table comment above `PLAN_QUOTAS`) and nothing reads
            # or writes either column any more. Neither is dropped: a production database already has them,
            # `ALTER TABLE ... DROP COLUMN` is a real risk for no benefit when an unused column costs nothing
            # to leave in place, and a schema statement that runs once and then never again is not "honest
            # migration", it is dead weight. A database that predates them (or postdates this change) never
            # gets them, which is fine, because nothing here looks for them either way.

    # ---- policy
    def get_policy(self, tenant: str) -> Policy:
        with self.lock:
            row = self.db.execute("SELECT policy FROM tenants WHERE id=?", (tenant,)).fetchone()
        return Policy.from_dict(json.loads(row[0])) if row else Policy()

    def save_policy(self, tenant: str, policy: Policy) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO tenants (id, policy) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET policy=excluded.policy",
                (tenant, json.dumps(policy.to_dict())),
            )
            self.db.commit()

    def get_meta(self, tenant: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT meta FROM tenants WHERE id=?", (tenant,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_meta(self, tenant: str, **kw: Any) -> None:
        meta = {**self.get_meta(tenant), **kw}
        with self.lock:
            self.db.execute(
                "INSERT INTO tenants (id, policy, meta) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET meta=excluded.meta",
                (tenant, json.dumps(Policy().to_dict()), json.dumps(meta)),
            )
            self.db.commit()

    def plan(self, tenant: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT plan FROM tenants WHERE id=?", (tenant,)).fetchone()
        return row[0] if row else INACTIVE

    def set_plan(self, tenant: str, plan: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO tenants (id, policy, plan) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET plan=excluded.plan",
                (tenant, json.dumps(Policy().to_dict()), plan),
            )
            self.db.commit()

    # ---- usage and quota
    @staticmethod
    def month() -> str:
        return time.strftime("%Y-%m")

    def add_usage(self, tenant: str, judged: int, requests: int, tokens: int) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO usage (tenant, month, judged, requests, tokens) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant, month) DO UPDATE SET judged=judged+excluded.judged, "
                "requests=requests+excluded.requests, tokens=tokens+excluded.tokens",
                (tenant, self.month(), judged, requests, tokens),
            )
            self.db.commit()

    def usage(self, tenant: str) -> tuple[int, int, int]:
        with self.lock:
            row = self.db.execute(
                "SELECT judged, requests, tokens FROM usage WHERE tenant=? AND month=?", (tenant, self.month())
            ).fetchone()
        return tuple(row) if row else (0, 0, 0)

    def quota_for(self, tenant: str) -> int:
        """Judged messages allowed this month for the tenant's plan.

        0 means unlimited and -1 means none at all. The negative case exists because 0 already meant
        unlimited before plans did, and an inactive tenant falling through to `PLAN_QUOTAS.get(plan, 0)`
        would therefore be granted unlimited judgement rather than none: the exact failure this change was
        written to avoid."""
        plan = self.plan(tenant)
        if ENFORCE_PLANS and plan == INACTIVE:
            return -1
        if plan == INACTIVE:
            return self.monthly_quota
        if plan == "trial":
            # A trial opens the model's judgement at Pro's ceiling, not Free's — Free's quota exists to cap
            # a plan that should not be spending on the model at all, and a trial is the opposite of that.
            # `plan` reaches "trial" only through the hosted webhook mirroring Stripe's own `trialing`
            # status now; this package never puts a tenant on trial itself.
            return PLAN_QUOTAS.get("pro", 0)
        return PLAN_QUOTAS.get(plan, 0)

    def over_quota(self, tenant: str) -> bool:
        q = self.quota_for(tenant)
        if q < 0:
            return True
        return q > 0 and self.usage(tenant)[0] >= q

    def month_spend_usd(self, max_age_s: float = 30.0) -> float:
        """What every tenant together has cost this month, at list price.

        Cached for half a minute. The sum itself is one indexed aggregate and is cheap, but it would run on
        every batch from every server, and a number that is thirty seconds stale cannot overshoot a budget
        by more than thirty seconds of traffic."""
        now = time.time()
        if self._spend_at + max_age_s > now:
            return self._spend
        with self.lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE month=?", (self.month(),)
            ).fetchone()
        self._spend = float(row[0]) * USD_PER_M_INPUT / 1e6
        self._spend_at = now
        return self._spend

    def paying_tenants(self, max_age_s: float = 30.0) -> int:
        """How many servers are on a paid plan right now. Cached on the same window as the spend, because it
        is read on the same path and changes on the timescale of a card payment, not of a batch.

        Counts only `pro`, never `trial`: this number feeds `budget_ceiling`, which grows the hard ceiling by
        one allowance per paying server. A trial brings no money with it, so counting it here would let a
        trial raise the very ceiling it is itself spending against — the opposite of `over_budget` treating
        it as free."""
        now = time.time()
        if self._paying_at + max_age_s > now:
            return self._paying
        with self.lock:
            row = self.db.execute("SELECT COUNT(*) FROM tenants WHERE plan = 'pro'").fetchone()
        self._paying = int(row[0])
        self._paying_at = now
        return self._paying

    def budget_ceiling(self) -> float:
        """The hard ceiling for this month: the operator's own floor, plus the allowance each paying server
        brought with it. 0 means no ceiling at all, and stays 0 however many subscriptions exist, because an
        operator who turned the ceiling off did not ask for one to grow back."""
        if not GLOBAL_BUDGET_USD:
            return 0.0
        return GLOBAL_BUDGET_USD + PAID_BUDGET_USD * self.paying_tenants()

    def over_budget(self, tenant: str) -> str | None:
        """Which ceiling this tenant has run into, if any: `global_budget` or `trial_budget`.

        Checked before spending rather than after, and it is deliberately not exact. The point is a bounded
        bill, not an exact one, and an operator who needs the last cent of precision has set the ceiling too
        close to what they can pay."""
        if not GLOBAL_BUDGET_USD and not TRIAL_BUDGET_USD:
            return None
        spend = self.month_spend_usd()
        ceiling = self.budget_ceiling()
        # Thirty seconds of staleness is cheap when the month is a tenth spent and expensive when it is
        # nearly over: every batch that reads a stale figure is judged in full before the write that would
        # have stopped it. So the cache collapses as the ceiling approaches, and the last stretch is read
        # fresh, which bounds the overshoot to one batch rather than to half a minute of traffic.
        near = max(ceiling or 0.0, TRIAL_BUDGET_USD) * NEAR_CEILING
        if near and spend >= near:
            spend = self.month_spend_usd(max_age_s=0.0)
            ceiling = self.budget_ceiling()
        if ceiling and spend >= ceiling:
            return "global_budget"
        # A trial pauses at the lower ceiling rather than the hard one, so trials give way before a server
        # that is actually paying does. An inactive tenant is listed too, although it cannot reach here:
        # it is gated long before any spending, and leaving it out would make this the one place that
        # treats an unpaid tenant as a paying one if that gate ever moved.
        if TRIAL_BUDGET_USD and spend >= TRIAL_BUDGET_USD and self.plan(tenant) in (INACTIVE, "trial"):
            return "trial_budget"
        return None

    # ---- hosted billing
    def set_subscription(
        self,
        tenant: str,
        *,
        customer_id: str | None,
        subscription_id: str | None,
        price_id: str | None,
        status: str,
        current_period_end: float | None,
    ) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO subscriptions (tenant, customer_id, subscription_id, price_id, status, "
                "current_period_end, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant) DO UPDATE SET customer_id=excluded.customer_id, "
                "subscription_id=excluded.subscription_id, price_id=excluded.price_id, status=excluded.status, "
                "current_period_end=excluded.current_period_end, updated=excluded.updated",
                (tenant, customer_id, subscription_id, price_id, status, current_period_end, time.time()),
            )
            self.db.commit()

    def subscription(self, tenant: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT customer_id, subscription_id, price_id, status, current_period_end, updated FROM subscriptions "
                "WHERE tenant=?",
                (tenant,),
            ).fetchone()
        if not row:
            return None
        keys = ("customer_id", "subscription_id", "price_id", "status", "current_period_end", "updated")
        return dict(zip(keys, row, strict=True))

    def tenant_for_subscription(self, subscription_id: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT tenant FROM subscriptions WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
        return row[0] if row else None

    def tenants_overview(self) -> list[dict[str, Any]]:
        """Every tenant with plan, this month's usage and subscription status, for the admin panel."""
        m = self.month()
        with self.lock:
            rows = self.db.execute(
                "WITH ids AS (SELECT id FROM tenants UNION SELECT tenant FROM usage WHERE month = ?) "
                "SELECT ids.id, COALESCE(t.plan, 'inactive'), COALESCE(u.judged, 0), COALESCE(u.requests, 0), "
                "COALESCE(u.tokens, 0), s.status, s.current_period_end, s.customer_id FROM ids "
                "LEFT JOIN tenants t ON t.id = ids.id "
                "LEFT JOIN usage u ON u.tenant = ids.id AND u.month = ? "
                "LEFT JOIN subscriptions s ON s.tenant = ids.id ORDER BY COALESCE(u.judged, 0) DESC",
                (m, m),
            ).fetchall()
        out = []
        for r in rows:
            plan = r[1]
            quota = self.monthly_quota if plan == INACTIVE else PLAN_QUOTAS.get(plan, 0)
            out.append(
                {
                    "tenant": r[0],
                    "plan": plan,
                    "judged": r[2],
                    "requests": r[3],
                    "tokens": r[4],
                    "quota": quota,
                    "subscription_status": r[5],
                    "current_period_end": r[6],
                    "customer_id": r[7],
                }
            )
        return out

    def totals(self) -> dict[str, Any]:
        m = self.month()
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(judged), 0), COALESCE(SUM(tokens), 0) FROM usage WHERE month=?", (m,)
            ).fetchone()
            plans = self.db.execute("SELECT plan, COUNT(*) FROM tenants GROUP BY plan").fetchall()
        return {"month": m, "active_tenants": row[0], "judged": row[1], "tokens": row[2], "plans": dict(plans)}

    def note_quota_hit(self, tenant: str) -> bool:
        """True the first time this month the tenant hits the quota (so the adapter can notify the owner once)."""
        with self.lock:
            row = self.db.execute("SELECT quota_notified FROM tenants WHERE id=?", (tenant,)).fetchone()
            if row and row[0] == self.month():
                return False
            self.db.execute(
                "INSERT INTO tenants (id, policy, quota_notified) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET quota_notified=excluded.quota_notified",
                (tenant, json.dumps(Policy().to_dict()), self.month()),
            )
            self.db.commit()
            return True

    # ---- audit
    def purge_expired(self) -> int:
        """Decisions only. `labels` is deliberately not swept: see `add_label`."""
        cutoff = time.time() - self.retention_days * 86400
        with self.lock:
            cur = self.db.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount

    def decision_for(self, tenant: str, message_id: str) -> dict[str, Any] | None:
        """The logged decision for one message, so a human verdict can be stored beside the scores
        that produced it rather than beside the category alone."""
        with self.lock:
            row = self.db.execute(
                "SELECT category, p, action, scores FROM decisions WHERE tenant=? AND message=? "
                "ORDER BY ts DESC LIMIT 1",
                (tenant, message_id),
            ).fetchone()
        if not row:
            return None
        return {"category": row[0], "p": row[1], "action": row[2], "scores": json.loads(row[3])}

    def add_label(self, tenant: str, message_id: str, category: str, agreed: bool,
                  scores: dict[str, float], source: str) -> None:
        """A human's verdict on one message, kept as a labelled datapoint. JEV-12.

        **No text and no moderator identity.** Both are deliberate and both cost something.

        Text, because `decisions` rows are purged after `retention_days` and that promise is in the
        privacy notice. A label pointing at a message whose words are gone in thirty days would not
        be a dataset. What is stored instead is the scores the model gave, the category, and whether
        the human agreed, and that is enough to compute precision and recall at *any* threshold for
        ever without keeping a word anybody wrote. What it cannot do is re-judge the message later
        with a different prompt, and that is the price: the alternative is a table of everybody's
        messages kept indefinitely, which is a different product and a different privacy notice.

        Identity, because who clicked is personal data about a second person, the one the privacy
        notice does not even mention. The dataset does not need it. If rate-limiting one person
        clicking fifty times ever matters, that is a counter in memory, not a column.

        Not purged with the decision it refers to. That is the point: a thirty day dataset is not a
        dataset. It holds no personal data to expire, and `delete_tenant` still removes it.
        """
        with self.lock:
            self.db.execute(
                "INSERT INTO labels VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), tenant, message_id, category, 1 if agreed else 0,
                 json.dumps({k: round(v, 3) for k, v in scores.items()}), source),
            )
            self.db.commit()

    def labels(self, tenant: str | None = None, n: int = 1000) -> list[dict[str, Any]]:
        """The dataset, newest first. `tenant=None` reads every tenant, which is what an export for
        `benchmark/evaluate.py` wants."""
        sql = "SELECT ts, tenant, message, category, agreed, scores, source FROM labels"
        args: tuple[Any, ...] = ()
        if tenant is not None:
            sql += " WHERE tenant=?"
            args = (tenant,)
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", (*args, n)).fetchall()
        return [{"ts": r[0], "tenant": r[1], "message_id": r[2], "category": r[3],
                 "agreed": bool(r[4]), "scores": json.loads(r[5]), "source": r[6]} for r in rows]

    def delete_user(self, tenant: str, author: str) -> int:
        """Right to erasure for one member of a community."""
        with self.lock:
            cur = self.db.execute("DELETE FROM decisions WHERE tenant=? AND author=?", (tenant, author))
            self.db.commit()
            return cur.rowcount

    def log_decision(self, tenant: str, m: Message, d: Decision, rid: str) -> None:
        self.purge_expired()
        with self.lock:
            self.db.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    tenant,
                    rid,
                    m.id,
                    m.author,
                    m.channel_topic[:80],
                    d.category,
                    d.probability,
                    d.action,
                    json.dumps({k: round(v, 3) for k, v in d.scores.items()}),
                    m.text[: self.keep_text_chars],
                ),
            )
            self.db.commit()

    def recent_decisions(self, tenant: str, n: int = 10) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                # author and channel are stored and were never read back, which left /mod recent printing a
                # bare message id that a moderator cannot click, search or act on.
                "SELECT ts, message, category, p, action, scores, text, author, channel FROM decisions "
                "WHERE tenant=? ORDER BY ts DESC LIMIT ?",
                (tenant, n),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "message_id": r[1],
                "category": r[2],
                "p": r[3],
                "action": r[4],
                "scores": json.loads(r[5]),
                "text": r[6],
                "author": r[7],
                "channel": r[8],
            }
            for r in rows
        ]

    def export_decisions(self, tenant: str) -> list[dict[str, Any]]:
        return self.recent_decisions(tenant, 100000)

    def delete_tenant(self, tenant: str) -> None:
        """GDPR: forget everything about a community or API tenant.

        The tables are discovered from the schema rather than listed here. A hardcoded list is a promise
        that rots: the hosted service creates its own tables on this same database, and the first one added
        after this method was written was already being missed. Anything with a `tenant` column is this
        tenant's data by construction, and `tenants.id` is the same thing under an older name."""
        with self.lock:
            names = [
                r[0]
                for r in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table in names:
                cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
                col = "tenant" if "tenant" in cols else ("id" if table == "tenants" else None)
                if col:
                    self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (tenant,))
            self.db.commit()

    def leave_tenant(self, tenant: str) -> None:
        """A server jevmod is no longer in: forget its data (`delete_tenant`, above) and, if it was paying,
        make sure the money stops too.

        This package has no idea what Stripe is and must not learn: it is the thing a self-hoster runs with
        no billing at all. What it does know, already, is that a tenant can have a `subscription_id` (see
        `set_subscription`/`subscription`), so queuing "this subscription needs to be cancelled" is a plain
        continuation of that existing vocabulary, not a new dependency. The trial notifier
        (`jevmod_hosted/quota_notify.py`) solved the identical split the same way: one SQLite file, written
        by this process, read by the hosted service's own sweep, which is the only thing that ever calls
        Stripe.

        Order matters. The subscription id has to be read *before* `delete_tenant` wipes the `subscriptions`
        row, and the queue row has to be written *after* `delete_tenant` runs, or `delete_tenant`'s own
        generic "any table with a `tenant` column" sweep would delete the very row this method just wrote,
        since `cancellation_queue` has one too. `subscription_id` is the primary key, so a server that
        leaves and rejoins and leaves again queues the same cancellation once, not twice."""
        sub = self.subscription(tenant)
        self.delete_tenant(tenant)
        sub_id = sub.get("subscription_id") if sub else None
        if sub_id:
            with self.lock:
                self.db.execute(
                    "INSERT OR IGNORE INTO cancellation_queue (subscription_id, tenant, requested_at) "
                    "VALUES (?, ?, ?)",
                    (sub_id, tenant, time.time()),
                )
                self.db.commit()

    def pending_cancellations(self) -> list[dict[str, Any]]:
        """Read by the hosted service's own sweep; never by this package. Oldest first, so a backlog (a
        Stripe outage, say) drains in the order servers actually left."""
        with self.lock:
            rows = self.db.execute(
                "SELECT subscription_id, tenant, requested_at FROM cancellation_queue ORDER BY requested_at"
            ).fetchall()
        keys = ("subscription_id", "tenant", "requested_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def clear_cancellation(self, subscription_id: str) -> None:
        """Written only after the hosted sweep confirms Stripe actually cancelled this subscription, so a
        failed attempt leaves the row queued for the next pass instead of losing it."""
        with self.lock:
            self.db.execute("DELETE FROM cancellation_queue WHERE subscription_id=?", (subscription_id,))
            self.db.commit()

    # ---- API keys (developers)
    def create_api_key(self, tenant: str, key_hash: str, label: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO api_keys (key_hash, tenant, created, label) VALUES (?, ?, ?, ?)",
                (key_hash, tenant, time.time(), label),
            )
            self.db.commit()

    def tenant_for_key(self, key_hash: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT tenant FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
        return row[0] if row else None
