"""HTTP API for developers and for any chatbot: POST a batch of messages, get decisions.

    JEVMOD_KEYMINT_TOKEN=... TYPESAFE_API_KEY=... uvicorn jevmod.api.server:app --port 8080

Auth: `Authorization: Bearer <api key>`. Keys are minted with JEVMOD_KEYMINT_TOKEN (`POST /v1/keys`) and stored
hashed. That token does one job: minting keys. It used to be JEVMOD_ADMIN_TOKEN, which also authenticated the
operator's panel and signed every billing link, so a leak of it handed over all three at once.
Every response carries the request id; every judged decision is in the tenant's audit log.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core import ModerationService, Store
from ..judge import CATEGORIES, Message

app = FastAPI(
    title="jevmod",
    version="0.2.0",
    description="Moderation decisions for user content: spam, scam, harassment, adult, off-topic and your own rules. "
    "Powered by Jev (TypeSafe). Probabilities, thresholds you own, decisions you can audit.",
)
store = Store(os.environ.get("JEVMOD_DB", "jevmod.sqlite"))
service = ModerationService(store)
STARTED = time.time()
_metrics = {"requests": 0, "messages": 0, "errors": 0}


# ------------------------------------------------------------------ models
class InMessage(BaseModel):
    id: str = Field(default="", description="your id for the message; echoed back")
    text: str = Field(..., max_length=8000)
    author: str = ""
    channel_topic: str = Field(default="", description="what the channel/thread is about; used by offtopic")
    author_trusted: bool = Field(default=False, description="true skips judgment (moderators, verified staff)")


class ModerateRequest(BaseModel):
    messages: list[InMessage] = Field(..., min_length=1, max_length=50)


class DecisionOut(BaseModel):
    message_id: str
    action: str
    category: str | None
    probability: float
    scores: dict[str, float]
    judged: bool
    reason: str


class ModerateResponse(BaseModel):
    request_id: str
    decisions: list[DecisionOut]
    usage: dict[str, int]


class PolicyIn(BaseModel):
    # `extra="forbid"` because this model used to have no `rule_thresholds` field: a PUT carrying one returned
    # 200 with a policy object that quietly did not contain it. On a write path, silence is the worst answer.
    model_config = ConfigDict(extra="forbid")

    thresholds: dict[str, float] | None = None
    actions: dict[str, str] | None = None
    rules: dict[str, str] | None = None
    rule_actions: dict[str, str] | None = None
    rule_thresholds: dict[str, float] | None = None
    timeout_minutes: int | None = None


class KeyRequest(BaseModel):
    tenant: str = Field(..., min_length=1, max_length=80)
    label: str = ""


# ------------------------------------------------------------------ auth
def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def tenant_from_auth(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    tenant = store.tenant_for_key(_hash(authorization[7:].strip()))
    if not tenant:
        raise HTTPException(401, "unknown api key")
    return tenant


def keymint_only(authorization: str = Header(default="")) -> None:
    """Minting a key for a tenant is the one thing this token does. Header only: a credential in a query
    string ends up in the proxy log and the browser history."""
    want = os.environ.get("JEVMOD_KEYMINT_TOKEN", "")
    given = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not want or not hmac.compare_digest(given, want):
        raise HTTPException(403, "key-minting token required")


# ------------------------------------------------------------------ routes
@app.get("/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "uptime_s": int(time.time() - STARTED), "categories": list(CATEGORIES)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    j = service.judge if service._judge else None
    lines = [
        f"jevmod_http_requests_total {_metrics['requests']}",
        f"jevmod_messages_total {_metrics['messages']}",
        f"jevmod_http_errors_total {_metrics['errors']}",
        f"jevmod_jev_requests_total {j.requests if j else 0}",
        f"jevmod_jev_input_tokens_total {j.input_tokens if j else 0}",
    ]
    return "\n".join(lines) + "\n"


@app.post("/v1/moderate", response_model=ModerateResponse)
def moderate(
    req: ModerateRequest, request: Request, response: Response, tenant: str = Depends(tenant_from_auth)
) -> ModerateResponse:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response.headers["X-Request-Id"] = rid
    _metrics["requests"] += 1
    _metrics["messages"] += len(req.messages)
    msgs = [
        Message(m.id or str(i), m.text, author=m.author, channel_topic=m.channel_topic, author_trusted=m.author_trusted)
        for i, m in enumerate(req.messages)
    ]
    try:
        decisions = service.moderate(tenant, msgs, request_id=rid)
    except Exception as exc:
        _metrics["errors"] += 1
        raise HTTPException(502, f"judgment failed: {type(exc).__name__}") from exc
    judged, requests, tokens = store.usage(tenant)
    return ModerateResponse(
        request_id=rid,
        decisions=[DecisionOut(**{k: v for k, v in d.to_dict().items() if k != "policy_version"}) for d in decisions],
        usage={"judged_this_month": judged, "jev_requests_this_month": requests, "input_tokens_this_month": tokens},
    )


@app.get("/v1/policy")
def get_policy(tenant: str = Depends(tenant_from_auth)) -> dict[str, Any]:
    return service.policy(tenant).to_dict()


@app.put("/v1/policy")
def put_policy(body: PolicyIn, tenant: str = Depends(tenant_from_auth)) -> dict[str, Any]:
    p = service.policy(tenant)
    try:
        for c, a in (body.actions or {}).items():
            p.set_category(c, a, (body.thresholds or {}).get(c))
        for c, t in (body.thresholds or {}).items():
            if c in CATEGORIES and c not in (body.actions or {}):
                p.set_category(c, p.actions.get(c, "flag"), t)
        for n, text in (body.rules or {}).items():
            p.set_rule(
                n,
                text,
                (body.rule_actions or {}).get(n, p.rule_actions.get(n, "flag")),
                (body.rule_thresholds or {}).get(n),
            )
        # a threshold for a rule whose text is not being changed in the same request
        for n, t in (body.rule_thresholds or {}).items():
            if n in p.rules and n not in (body.rules or {}):
                p.set_rule(n, p.rules[n], p.rule_actions.get(n, "flag"), t)
        if body.timeout_minutes is not None:
            p.timeout_minutes = max(1, min(int(body.timeout_minutes), 1440))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    service.save_policy(tenant, p)
    return p.to_dict()


@app.get("/v1/decisions")
def decisions(limit: int = 50, tenant: str = Depends(tenant_from_auth)) -> list[dict[str, Any]]:
    return store.recent_decisions(tenant, max(1, min(limit, 500)))


@app.delete("/v1/tenant")
def delete_tenant(tenant: str = Depends(tenant_from_auth)) -> dict[str, bool]:
    """GDPR: forget this tenant's policy, usage and decision log.

    `leave_tenant` rather than `delete_tenant`, because this call deletes the subscription row too and the
    card would otherwise keep being charged for a tenant that no longer exists. It reads the subscription id
    first and queues it after, and a deployment with nobody draining that queue - a self-hosted copy, which
    has no Stripe at all - is left with one harmless row.

    This is the same hole `on_guild_remove` had. The difference here is that nobody is present to read a
    warning: `/mod forget` is typed by a person who can be told to cancel, and an API call is not."""
    store.leave_tenant(tenant)
    # The conversation window lives in memory, not in the store, so deleting rows does not reach it.
    # An erasure that leaves the last ten messages of every channel sitting in a deque has not done
    # what it told the caller it did.
    service.forget_context(tenant)
    return {"deleted": True}


@app.post("/v1/keys", dependencies=[Depends(keymint_only)])
def create_key(body: KeyRequest) -> dict[str, str]:
    key = "jm_" + secrets.token_urlsafe(32)
    store.create_api_key(body.tenant, _hash(key), body.label)
    return {"tenant": body.tenant, "api_key": key, "note": "shown once; stored hashed"}
