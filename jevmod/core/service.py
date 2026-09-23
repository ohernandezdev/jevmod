"""ModerationService: one entry point for every adapter and for the HTTP API.

    svc = ModerationService(store)
    decisions = svc.moderate(tenant_id, messages)   # applies policy, quota, audit log, fail-open

Tenant = a Discord guild, a Telegram chat, a subreddit or an API key. The service never knows which.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from typesafe_sdk import TypeSafeError

from ..judge import PAD_TO, Judge, Message
from . import local
from .context import ConversationBuffer, assemble
from .local import RepeatWindow
from .policy import Decision, Policy, decide
from .store import ENFORCE_PLANS, INACTIVE, Store

log = logging.getLogger("jevmod")

JEV_USD_PER_M_INPUT = 0.042
# The most one check-then-spend window may commit. A quota is checked before a batch and written
# after it, so a batch bigger than this is a ceiling overshot by exactly that much.
MAX_BATCH = int(os.environ.get("JEVMOD_MAX_BATCH", "100") or 100)

# Pad a small batch with recent messages from the same channel so a quiet server is not moderated
# worse than a busy one. On by default, and the reason is that the alternative is not moderation:
# spam recall at the shipped threshold is 17.3% when a message is judged alone and 38.7% in a
# request of ten (`benchmark/BATCH_EFFECT.md` section 7), and `Batcher` sizes a batch by how busy
# the channel is, so which of those a server gets is decided by its traffic.
#
# It costs about eight times the model spend for the messages that get padded, and only those: a
# busy channel already fills its own request and pays nothing extra. At $0.042/M input that is
# $0.51 per thousand judged messages against $0.06, so a quiet server at a thousand messages a
# month goes from six cents to fifty against a $3.99 plan.
#
# `JEVMOD_PAD_BATCH=0` buys the cheaper version back. What it trades away is twenty-one points of
# spam recall, which is why the number is written here next to the switch rather than in a
# changelog nobody reads.
# Explicit about every spelling it accepts, and loud about one it does not. The first version
# treated anything outside {0, false, no} as on, so `JEVMOD_PAD_BATCH=off` left padding running and
# said nothing: an operator who believed they had turned off eight times the model spend had not.
_PAD_RAW = os.environ.get("JEVMOD_PAD_BATCH")
_PAD_FALSE = {"0", "false", "no", "off", "n", ""}
_PAD_TRUE = {"1", "true", "yes", "on", "y"}
if _PAD_RAW is not None and _PAD_RAW.strip().lower() not in _PAD_FALSE | _PAD_TRUE:
    log.warning({"event": "pad_batch_unrecognised", "value": _PAD_RAW[:20],
                 "using": "on", "accepts": sorted(_PAD_FALSE | _PAD_TRUE)})
PAD_BATCH = _PAD_RAW is None or _PAD_RAW.strip().lower() not in _PAD_FALSE
# Whether plans mean anything here, and the plan a tenant sits on when nothing is paying for it. Both are
# defined in `store` so that the gate and the quota that enforce them cannot drift apart.


class ModerationService:
    def __init__(self, store: Store, judge: Judge | None = None, fail_open: bool = True) -> None:
        self.store = store
        self._judge = judge
        self.fail_open = fail_open
        # Anti-raid's repeat counter: in memory, per service instance, on purpose. It is a sixty second
        # phenomenon, not something a disk write per message should pay for, and losing it on restart is the
        # correct failure — see jevmod/core/local.py.
        # Public, because the adapter counts joins in the same window: an adapter reaching into a private
        # attribute is how two copies of a sixty second window end up disagreeing with each other.
        self.seen = RepeatWindow()
        # What was said just before, per channel, bounded three ways (see core/context.py). In
        # memory for the same reason as `seen`: it is a fifteen minute phenomenon, and persisting
        # everybody's recent messages is a retention question the privacy notice does not answer
        # today. JEV-20 to JEV-22 own that decision; losing the window on restart costs one slightly
        # worse verdict and nothing else.
        self.context = ConversationBuffer()

    def _channel_key(self, tenant: str, m: Message) -> tuple[str, str]:
        """One window per conversation. Only Discord splits a tenant into channels; everywhere else
        `m.channel` is empty and the tenant is the conversation.

        A tuple rather than a joined string. The join used a separator a user can type, and a tenant
        named `acme<sep>evil` was deleted by `forget_context("acme")`. Only the admin key-minting
        API can produce such a tenant, since all five adapters build theirs from a platform id, but
        a key that cannot be spoofed costs nothing.
        """
        return (tenant, m.channel)

    def forget_context(self, tenant: str) -> None:
        """Drop every window belonging to a tenant. `/mod forget`, leaving a server and
        `DELETE /v1/tenant` all promise that what was stored about a server is gone, and a deque of
        its recent messages is stored about that server."""
        for key in self.context.channels():
            if key[0] == tenant:
                self.context.forget(key)

    @property
    def judge(self) -> Judge:
        if self._judge is None:
            self._judge = Judge()
        return self._judge

    def policy(self, tenant: str) -> Policy:
        return self.store.get_policy(tenant)

    def save_policy(self, tenant: str, policy: Policy) -> None:
        self.store.save_policy(tenant, policy)

    def moderate(self, tenant: str, messages: list[Message], request_id: str | None = None) -> list[Decision]:
        rid = request_id or uuid.uuid4().hex[:12]
        policy = self.policy(tenant)
        if not policy.active():
            return [Decision(m.id, "none", None, 0.0, {}, False, "policy inactive") for m in messages]
        self.store.purge_expired()

        # An inactive tenant is stopped here, above local rules rather than below them.
        #
        # The plan this replaced, `free`, sat below: it never reached the model but its link filters, word
        # lists and patterns kept running, which is what made it a usable free tier. That tier was removed
        # as an offer on 2026-09-21, and with it the reason for the gate to sit further down. A server that
        # is not paying gets nothing evaluated; what it configured stays stored and stops being applied.
        #
        # Only on the hosted service. `ENFORCE_PLANS` is off by default, so a self-hosted copy, where every
        # tenant sits on the default plan and there is no billing at all, never takes this branch.
        if ENFORCE_PLANS and self.store.plan(tenant) == INACTIVE:
            return [Decision(m.id, "none", None, 0.0, {}, False, "inactive") for m in messages]

        # The conversation window, below the inactive gate on purpose: a tenant whose messages are
        # not evaluated should not have its messages held in memory either. Above it, an inactive
        # server would keep filling a buffer nobody reads, which is both waste and the wrong answer
        # to "what do you keep about a server that stopped paying".
        #
        # The window is what was said before this batch, not inside it. Messages that share a batch
        # already see each other: they sit at neighbouring positions in the same request, and adding
        # them as context too would show the model the same sentence twice and tell it somebody
        # repeated themselves. So every message reads first, and the batch is written after.
        for m in messages:
            m.context = assemble(self.context.window_for(self._channel_key(tenant, m), exclude=m.text))
        for m in messages:
            # Every message that arrived, judged or not. A pre-filtered "lol" is still part of what
            # the conversation looked like, and a window holding only the messages worth judging is
            # a window of an argument with the small talk taken out.
            self.context.add(self._channel_key(tenant, m), m.text)

        # Local rules cost nothing per message, so they run before every gate below: a tenant that is out of
        # quota, over the shared budget, or has never enabled a single Jev category still gets its link
        # filter, its word list and its patterns. A message a local rule decides is logged like any other
        # decision and never reaches a gate that only governs what is left for the model.
        decided: dict[str, Decision] = {}
        remaining: list[Message] = []
        for m in messages:
            d = local.check(m, policy, self.seen)
            if d is None:
                remaining.append(m)
            else:
                decided[m.id] = d
                self.store.log_decision(tenant, m, d, rid)

        if not remaining:
            return [decided[m.id] for m in messages]

        judged = self._gate_and_judge(tenant, policy, remaining, rid)
        by_id: dict[str, Decision] = {**decided, **{d.message_id: d for d in judged}}
        return [by_id[m.id] for m in messages]

    def _gate_and_judge(self, tenant: str, policy: Policy, messages: list[Message], rid: str) -> list[Decision]:
        """Everything that only governs what is left after local rules have already decided what they can:
        the per-tenant quota, the shared spend ceiling, and the batch-size cap."""
        if self.store.over_quota(tenant):
            # note_quota_hit is a once-a-month latch, and the adapters use it to decide whether to post the
            # notice. Consuming it here meant the adapter always got False, so the notice never reached anyone.
            return [Decision(m.id, "none", None, 0.0, {}, False, "quota") for m in messages]
        # The tenant is inside its own quota. The question the per-tenant quota cannot answer is whether the
        # service as a whole can still afford to judge: fifty servers each inside their own ceiling still add
        # up to more than one person budgeted for, and that is the success case rather than an attack.
        hit = self.store.over_budget(tenant)
        if hit:
            log.warning({"event": "budget", "tenant": tenant, "rid": rid, "ceiling": hit})
            return [Decision(m.id, "none", None, 0.0, {}, False, hit) for m in messages]
        # A batch is checked once and then spent whole, so its size is the amount any ceiling can be
        # overshot by. Two seconds of a raid is otherwise one batch of whatever arrived.
        if len(messages) > MAX_BATCH:
            head, tail = messages[:MAX_BATCH], messages[MAX_BATCH:]
            return self._gate_and_judge(tenant, policy, head, rid) + [
                Decision(m.id, "none", None, 0.0, {}, False, "over_batch") for m in tail
            ]
        return self._judge_batch(tenant, policy, messages, rid)

    def _judge_batch(self, tenant: str, policy: Policy, messages: list[Message], rid: str) -> list[Decision]:
        j = self.judge
        before = (j.requests, j.input_tokens, j.judged_messages)
        t0 = time.perf_counter()
        # Padding is drawn from the same window the context comes from, so a message's cache key
        # already varies with it: `_key` hashes `m.context`, and both are this channel's recent
        # history. There is nothing to add to the key here.
        #
        # Only when the whole batch is one conversation. `Batcher` groups by tenant, and a Discord
        # guild's two seconds can hold #general and #support at once; padding such a batch from one
        # of them would be picking a channel arbitrarily and calling it context. A batch that spans
        # channels is also several conversations' worth of messages already, which is the thing
        # padding exists to supply.
        padding: tuple[str, ...] = ()
        channels = {m.channel for m in messages}
        if PAD_BATCH and len(messages) < PAD_TO and len(channels) == 1:
            # The batch is already in the buffer by now, because `moderate` writes it before any of
            # this runs. Its own texts are filtered out here rather than left for `judge` to drop,
            # so what this function passes is what it means: the history, not the present.
            own = {m.text for m in messages}
            window = self.context.window_for(self._channel_key(tenant, messages[0]))
            padding = tuple(text for text in window if text not in own)
        try:
            verdicts = j.judge(messages, policy.enabled_categories(), policy.rules, padding)
        except TypeSafeError as exc:
            log.warning(
                {"event": "judge_error", "tenant": tenant, "rid": rid, "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            if not self.fail_open:
                raise
            return [Decision(m.id, "none", None, 0.0, {}, False, "error_open") for m in messages]
        ms = int((time.perf_counter() - t0) * 1000)
        judged = j.judged_messages - before[2]
        reqs = j.requests - before[0]
        toks = j.input_tokens - before[1]
        self.store.add_usage(tenant, judged, reqs, toks)
        decisions = [decide(policy, v) for v in verdicts]
        for m, d in zip(messages, decisions, strict=True):
            if d.action != "none":
                self.store.log_decision(tenant, m, d, rid)
        log.info(
            {
                "event": "moderate",
                "tenant": tenant,
                "rid": rid,
                "messages": len(messages),
                "judged": judged,
                "requests": reqs,
                "input_tokens": toks,
                "usd": round(toks * JEV_USD_PER_M_INPUT / 1e6, 6),
                "ms": ms,
                "actions": {a: sum(1 for d in decisions if d.action == a) for a in ("flag", "delete", "timeout")},
            }
        )
        return decisions


class Batcher:
    """Collects messages per tenant for `window_s`, then hands the batch to `handler` (async). One Jev request per
    tenant per window instead of one per message."""

    def __init__(self, window_s: float, handler: Callable[[str, list[Any]], Any]) -> None:
        self.window = window_s
        self.handler = handler
        self.pending: dict[str, list[Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def add(self, tenant: str, item: Any) -> None:
        self.pending.setdefault(tenant, []).append(item)
        t = self.tasks.get(tenant)
        if t is None or t.done():
            self.tasks[tenant] = asyncio.create_task(self._flush(tenant))

    async def _flush(self, tenant: str) -> None:
        await asyncio.sleep(self.window)
        batch = self.pending.pop(tenant, [])
        if batch:
            try:
                await self.handler(tenant, batch)
            except Exception as exc:  # an adapter bug must not stop future batches
                log.exception({"event": "batch_handler_error", "tenant": tenant, "error": str(exc)[:200]})
