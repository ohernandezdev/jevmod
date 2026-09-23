"""No key needed: policy decisions, store, quota, retention, erasure, pre-filters."""

import os
import time
from pathlib import Path

import pytest

from jevmod import Message, Policy, decide
from jevmod.core import Decision, Store
from jevmod.judge import Verdict, prefilter


def v(mid, scores, custom=None, judged=True, reason="jev"):
    return Verdict(mid, scores, judged, reason, custom or {})


def test_most_severe_action_wins_then_probability():
    p = Policy()
    p.set_category("spam", "flag", 0.8)
    p.set_category("scam", "delete", 0.8)
    d = decide(p, v("1", {"spam": 0.95, "scam": 0.85}))
    assert d.action == "delete" and d.category == "scam"
    d = decide(p, v("2", {"spam": 0.95, "scam": 0.5}))
    assert d.action == "flag" and d.category == "spam"
    d = decide(p, v("3", {"spam": 0.5, "scam": 0.5}))
    assert d.action == "none" and d.judged


def test_rules_have_their_own_threshold_and_off_disables():
    p = Policy()
    p.set_rule("no_politics", "No politics", "flag", 0.6)
    assert decide(p, v("1", {}, {"no_politics": 0.65})).action == "flag"
    p.set_rule("no_politics", "No politics", "off")
    assert decide(p, v("1", {}, {"no_politics": 0.99})).action == "none"


def test_unjudged_messages_never_act():
    p = Policy()
    assert decide(p, v("1", {}, judged=False, reason="too short")).action == "none"


def test_nudge_both_ways_and_clamp():
    p = Policy()
    assert p.nudge("spam", 0.03) == 0.88
    assert p.nudge("spam", -0.02) == 0.86
    for _ in range(30):
        p.nudge("spam", 0.03)
    assert p.thresholds["spam"] == 0.99
    for _ in range(60):
        p.nudge("spam", -0.02)
    assert p.thresholds["spam"] == 0.5


def test_policy_roundtrip_and_validation():
    p = Policy()
    p.set_category("harassment", "timeout", 0.7)
    p.set_rule("english_only", "English only in this channel")
    q = Policy.from_dict(p.to_dict())
    assert q.actions["harassment"] == "timeout" and q.thresholds["harassment"] == 0.7 and "english_only" in q.rules
    for bad in (("nope", "flag"), ("spam", "nuke")):
        try:
            p.set_category(*bad)
            raise AssertionError("should fail")
        except ValueError:
            pass


def test_store_quota_retention_and_erasure():
    assert not Store(":memory:").over_quota("x")  # unlimited by default
    s = Store(":memory:", keep_text_chars=50, retention_days=1, monthly_quota=100)
    t = "discord:1"
    assert not s.over_quota(t)
    s.add_usage(t, 100, 10, 1000)
    assert s.over_quota(t)
    assert s.note_quota_hit(t) is True and s.note_quota_hit(t) is False
    s.set_plan(t, "pro")
    assert not s.over_quota(t)
    m = Message("m1", "x" * 200, author="42", channel_topic="general")
    d = Decision("m1", "flag", "spam", 0.9, {"spam": 0.9}, True, "jev")
    s.log_decision(t, m, d, "rid")
    rows = s.recent_decisions(t)
    assert len(rows) == 1 and len(rows[0]["text"]) == 50
    assert s.delete_user(t, "42") == 1 and not s.recent_decisions(t)
    s.log_decision(t, m, d, "rid")
    s.db.execute("UPDATE decisions SET ts=?", (time.time() - 3 * 86400,))
    assert s.purge_expired() == 1
    s.delete_tenant(t)
    assert s.usage(t) == (0, 0, 0) and s.plan(t) == "inactive"


def test_api_keys_are_hashed_lookups():
    s = Store(":memory:")
    s.create_api_key("api:acme", "deadbeef", "prod")
    assert s.tenant_for_key("deadbeef") == "api:acme" and s.tenant_for_key("nope") is None


def test_prefilter():
    assert prefilter(Message("a", "lol")) == "too short"
    assert prefilter(Message("b", "see http://x.y")) is None
    assert prefilter(Message("c", "long enough text here", author_trusted=True)) == "trusted author"


def test_every_role_is_dispatchable(monkeypatch):
    """`jevmod <role>` must reach its runner: the hosted role once fell out of the dispatcher unnoticed."""
    import jevmod.__main__ as m

    seen = []
    monkeypatch.setattr(m, "run_role", lambda r: seen.append(r))
    # demo and hosted are gone: they belong to the commercial repo, which installs this package and adds its
    # own entry point. The open package must not know they exist.
    roles = ("api", "discord", "telegram", "reddit", "twitch", "youtube", "mcp")
    assert roles == m.ROLES
    for role in roles:
        m.run_role(role)
    assert seen == list(roles)
    import inspect

    src = inspect.getsource(m).split("def run_role")[1].split("def main")[0]
    for role in roles:
        assert f'"{role}"' in src, role
    for gone in ("demo", "hosted"):
        assert f'"{gone}"' not in src, f"{gone} is commercial and must not be in the open dispatcher"


def test_the_cli_offers_exactly_the_roles_the_dispatcher_runs(capsys):
    """The two lists used to be typed out separately and drifted in both directions: `jevmod demo` parsed and
    then died on "unknown role", and `jevmod twitch` never existed although the adapter did. One list now."""
    from jevmod.__main__ import ROLES
    from jevmod.cli import main as cli

    with pytest.raises(SystemExit):
        cli(["--help"])
    help_text = capsys.readouterr().out
    for role in ROLES:
        assert role in help_text, f"{role} runs but is not offered on the command line"
    for gone in ("demo", "hosted"):
        assert f"    {gone}" not in help_text, f"{gone} is commercial and must not be offered"


def test_a_role_says_which_variables_are_missing(monkeypatch):
    """A missing credential must be a sentence naming every variable still unset, not a bare KeyError from
    inside the adapter -- and it must be raised before the adapter is imported, because importing one opens
    its Store and leaves a jevmod.sqlite behind in whatever directory the operator was standing in."""
    from jevmod.__main__ import run_role

    for var in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_REFRESH_TOKEN", "TWITCH_BOT_LOGIN",
                "TWITCH_CHANNELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "abc")
    with pytest.raises(SystemExit) as exc:
        run_role("twitch")
    message = str(exc.value)
    assert "TWITCH_CLIENT_ID" not in message, "it is set; naming it sends the operator after the wrong thing"
    assert "TWITCH_CHANNELS" in message and "TWITCH_REFRESH_TOKEN" in message


def test_keep_text_chars_env_and_zero(monkeypatch, tmp_path):
    """The hosted bot must be able to run without storing any message text (JEVMOD_KEEP_TEXT_CHARS=0)."""
    monkeypatch.setenv("JEVMOD_KEEP_TEXT_CHARS", "0")
    s = Store(tmp_path / "k.sqlite")
    assert s.keep_text_chars == 0
    m = Message("m1", "some flagged message text", author="42", channel_topic="general")
    d = Decision("m1", "flag", "spam", 0.9, {"spam": 0.9}, True, "jev")
    s.log_decision("discord:1", m, d, "rid")
    row = s.recent_decisions("discord:1")[0]
    assert row["text"] == "" and row["message_id"] == "m1" and row["scores"] == {"spam": 0.9}
    assert Store(tmp_path / "k2.sqlite", keep_text_chars=50).keep_text_chars == 50


def test_ai_generated_is_opt_in_and_not_nudged():
    """Measured in benchmark/ai_detect/REPORT.md: good ranking, but it flags humans who write encyclopedically,
    so it ships off by default, flag-only, and outside the reaction feedback loop."""
    from jevmod.core.policy import DEFAULT_ACTIONS, DEFAULT_THRESHOLDS
    from jevmod.judge import CATEGORIES

    assert CATEGORIES["ai_generated"]["experimental"] is True
    assert "not by themselves signs of a language model" in CATEGORIES["ai_generated"]["criteria"]["false"].lower()
    assert DEFAULT_ACTIONS["ai_generated"] == "off" and DEFAULT_THRESHOLDS["ai_generated"] == 0.85
    p = Policy()
    assert "ai_generated" not in p.enabled_categories()
    before = p.thresholds["ai_generated"]
    assert p.nudge("ai_generated", 0.03) == before and p.thresholds["ai_generated"] == before
    p.set_category("ai_generated", "flag")
    assert "ai_generated" in p.enabled_categories()
    assert p.nudge("spam", 0.03) > DEFAULT_THRESHOLDS["spam"]


def test_published_openapi_matches_the_app(tmp_path, monkeypatch):
    """api/openapi.json is what people import into Postman. It drifted once already, naming five of the nine
    categories. Regenerate it with `python scripts/gen_openapi.py` when this fails."""
    import json
    import sys

    monkeypatch.setenv("JEVMOD_DB", str(tmp_path / "oa.sqlite"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import gen_openapi

    published = json.loads((Path(__file__).resolve().parents[1] / "api" / "openapi.json").read_text("utf-8"))
    assert published == json.loads(json.dumps(gen_openapi.schema(), sort_keys=True))


def test_postman_collection_covers_every_endpoint():
    """The collection is a hand-written walkthrough, not generated, so a new endpoint can go missing from it."""
    import json

    root = Path(__file__).resolve().parents[1]
    paths = set(json.loads((root / "api" / "openapi.json").read_text("utf-8"))["paths"])
    collection = json.loads((root / "api" / "jevmod.postman_collection.json").read_text("utf-8"))
    urls: list[str] = []

    def walk(items: list[dict]) -> None:
        for item in items:
            if "item" in item:
                walk(item["item"])
                continue
            url = item.get("request", {}).get("url")
            urls.append(url.get("raw", "") if isinstance(url, dict) else str(url))

    walk(collection["item"])
    raw = " ".join(urls)
    missing = sorted(p for p in paths if p not in raw)
    assert not missing, f"api/jevmod.postman_collection.json has no request for {missing}"


def test_experimental_categories_can_only_be_off_or_flag():
    """The site says the experimental category only ever flags. Nothing enforced that, so `/mod set
    ai_generated delete` worked: a category whose projected precision is 0.19 to 0.37 could remove messages."""
    import pytest

    from jevmod.core.policy import EXPERIMENTAL

    p = Policy()
    for category in EXPERIMENTAL:
        p.set_category(category, "flag")
        assert p.actions[category] == "flag"
        p.set_category(category, "off")
        for forbidden in ("delete", "timeout"):
            with pytest.raises(ValueError):
                p.set_category(category, forbidden)
        assert p.actions[category] == "off"


def test_experimental_categories_ship_off_and_do_not_move():
    from jevmod.core.policy import DEFAULT_THRESHOLDS, EXPERIMENTAL

    p = Policy()
    for category in EXPERIMENTAL:
        assert p.actions.get(category) == "off"
        assert category not in p.enabled_categories()
        assert p.nudge(category, 0.03) == DEFAULT_THRESHOLDS[category]


def test_env_example_never_assigns_a_key_twice():
    """Two assignments of one key means the effective value depends on parse order. JEVMOD_MONTHLY_QUOTA was
    set to 0 and then to 5000 in the same file, so copying half of it gave an unlimited free plan."""
    from collections import Counter

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    keys = [ln.split("=", 1)[0].strip() for ln in text.splitlines() if "=" in ln and not ln.strip().startswith("#")]
    dupes = [k for k, n in Counter(keys).items() if n > 1]
    assert not dupes, f".env.example assigns {dupes} more than once"


def test_the_quota_notice_reaches_the_adapter(tmp_path):
    """note_quota_hit is a once-a-month latch that the adapters read to decide whether to post the notice.
    The service used to call it first, so every adapter got False and no one was ever told judging had paused."""
    from jevmod.core.service import ModerationService

    store = Store(tmp_path / "q.sqlite", monthly_quota=1)
    store.add_usage("t", 5, 1, 100)
    assert store.over_quota("t")
    policy = Policy()
    policy.set_category("spam", "flag")
    store.save_policy("t", policy)
    service = ModerationService(store=store, judge=None)
    decisions = service.moderate("t", [Message(id="m1", text="anything at all here")])
    assert [d.reason for d in decisions] == ["quota"]
    assert store.note_quota_hit("t") is True, "the adapter must still be able to claim the notice"
    assert store.note_quota_hit("t") is False, "and only once"


def test_a_threshold_can_be_typed_as_a_percentage():
    """The bot shows percentages everywhere, so 75 and 0.75 have to mean the same line. Someone who reads
    'from 75%' in /mod status and types 75 into /mod set must not end up with a threshold of 1.0."""
    import os

    os.environ.setdefault("DISCORD_TOKEN", "test")
    from jevmod.adapters.discord_bot import _as_probability

    assert _as_probability(75) == 0.75
    assert _as_probability(0.75) == 0.75
    assert _as_probability(99) == 0.99
    assert _as_probability(1) == 1
    assert _as_probability(None) is None


def test_a_flag_title_maps_back_to_its_category():
    """The flag title shows a human label and the buttons need the key behind it. Taking the first word of the
    title worked only while the title was the key, so changing the title silently broke both buttons."""
    import os

    os.environ.setdefault("DISCORD_TOKEN", "test")
    from jevmod.adapters.discord_bot import LABEL, category_of, label

    for key in LABEL:
        for verb in ("Flagged", "Deleted", "Muted the member"):
            assert category_of(f"{verb} — {label(key)}") == key
    assert category_of('Flagged — your rule "no politics"') == "rule:no politics"
    assert category_of("Deleted — something nobody ships") == ""


def test_a_rule_that_cannot_work_says_so():
    """Measured against real Jev: a one word rule never fired, a permissive rule never fires and does not
    relax the category it seems to contradict, and an exception about who the member is cannot be enforced
    because jevmod is never told who wrote the message."""
    import os

    os.environ.setdefault("DISCORD_TOKEN", "test")
    from jevmod.adapters.discord_bot import _rule_warnings

    assert "too short" in _rule_warnings("behave")
    assert "can only forbid" in _rule_warnings("swearing and roasting each other is fine here")
    assert "who the member is" in _rule_warnings("no self promo unless you are a regular")
    good = _rule_warnings("Do not advertise your own youtube channel, twitch stream or discord server.")
    assert good == "", good


def test_the_policy_endpoint_keeps_a_rule_threshold():
    """PUT /v1/policy accepted rule_thresholds and threw it away, answering 200 with a policy that did not
    contain it. An unknown field is now a 422 rather than silence."""
    import pytest
    from pydantic import ValidationError

    from jevmod.api.server import PolicyIn

    body = PolicyIn(rules={"no_politics": "No politics."}, rule_thresholds={"no_politics": 0.65})
    assert body.rule_thresholds == {"no_politics": 0.65}
    with pytest.raises(ValidationError):
        PolicyIn(rule_threshold={"typo": 0.65})


def test_the_hosted_bot_can_still_say_where_to_pay(monkeypatch):
    """The split moved billing out of the open package. The bot that sells Pro is the open one, so the link
    it hands a server owner has to survive that move: this failed silently in production once."""
    import importlib

    from jevmod.api import paylink

    monkeypatch.delenv("JEVMOD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("JEVMOD_BILLING_SECRET", raising=False)
    importlib.reload(paylink)
    assert paylink.enabled() is False, "a self-hosted copy has nothing to sell"

    monkeypatch.setenv("JEVMOD_PUBLIC_URL", "https://example.test/")
    monkeypatch.setenv("JEVMOD_BILLING_SECRET", "secret-token")
    importlib.reload(paylink)
    assert paylink.enabled() is True
    url = paylink.checkout_url("discord:123")
    assert url.startswith("https://example.test/billing/checkout?tenant=discord:123&exp=")
    assert "&sig=" in url

    # A signature for one guild must not open another guild's billing portal, for the same expiry.
    exp = int(time.time()) + 600
    assert paylink.sign_tenant("discord:123", exp) != paylink.sign_tenant("discord:124", exp)

    # No fallback key, and in particular not the admin token: one secret for three jobs meant one leak
    # opened the admin panel, the key minting and every customer's billing portal at once.
    monkeypatch.delenv("JEVMOD_BILLING_SECRET")
    importlib.reload(paylink)
    with pytest.raises(RuntimeError):
        paylink.sign_tenant("discord:123", exp)


def test_billing_link_signature_expires():
    """The finding this closes: `HMAC(tenant)` alone never expires, so a screenshot of a link is a
    forever-valid capability token. The fix folds the expiry into the signed message, so tampering with
    `exp` to extend a link's life breaks the signature instead of quietly working."""
    import importlib

    from jevmod.api import paylink

    os.environ["JEVMOD_BILLING_SECRET"] = "secret-token"
    importlib.reload(paylink)
    try:
        now = int(time.time())

        # A fresh link (issued just now, expiring in the future) verifies.
        future_exp = now + 600
        fresh_sig = paylink.sign_tenant("discord:1", future_exp)
        assert paylink.verify_signature("discord:1", str(future_exp), fresh_sig) is True
        assert paylink.link_expired(str(future_exp)) is False

        # An honestly-signed link whose expiry has already passed is expired, even though the signature
        # itself is genuine.
        past_exp = now - 1
        expired_sig = paylink.sign_tenant("discord:1", past_exp)
        assert paylink.verify_signature("discord:1", str(past_exp), expired_sig) is True
        assert paylink.link_expired(str(past_exp)) is True

        # Extending `exp` on a link without re-signing is a tamper, not a renewal: the old signature was
        # over the old `exp`, so it does not match the stretched one.
        assert paylink.verify_signature("discord:1", str(future_exp + 100000), expired_sig) is False

        # A signature for a different tenant, replayed against this tenant's link, does not verify either.
        other_sig = paylink.sign_tenant("discord:2", future_exp)
        assert paylink.verify_signature("discord:1", str(future_exp), other_sig) is False
    finally:
        del os.environ["JEVMOD_BILLING_SECRET"]
        importlib.reload(paylink)


def test_the_store_survives_two_processes(tmp_path):
    """The bot, the API and the Stripe webhook are three processes on one file. SQLite's defaults make that
    fail: the rollback journal locks readers out during a write, and a zero busy timeout raises on the first
    collision rather than waiting. This asserts the pragmas that make it safe, because nothing else would
    notice they were gone until a customer paid and the webhook lost the write."""
    import sqlite3

    db = tmp_path / "s.sqlite"
    s = Store(db)
    assert s.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert s.db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    # A second connection, standing in for another container, writes while the first holds a read.
    other = sqlite3.connect(str(db), timeout=5)
    s.save_policy("discord:1", Policy())
    cur = s.db.execute("SELECT id FROM tenants")
    other.execute("INSERT INTO usage (tenant, month, judged, requests, tokens) VALUES ('discord:2','2026-09',1,1,1)")
    other.commit()
    assert [r[0] for r in cur.fetchall()] == ["discord:1"], "a concurrent write must not break an open read"
    other.close()


def test_the_three_operator_secrets_are_independent(monkeypatch):
    """One secret used to sign billing links, authenticate the admin panel and authorise minting an API key
    for any tenant. A leak of it opened all three. They are separate now, and holding one must not work as
    another: this asserts that, because the only symptom of a regression would be a quiet re-merge."""
    import importlib

    from jevmod.api import paylink, server

    monkeypatch.setenv("JEVMOD_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("JEVMOD_BILLING_SECRET", "billing-secret")
    monkeypatch.setenv("JEVMOD_KEYMINT_TOKEN", "keymint-secret")
    monkeypatch.setenv("JEVMOD_ADMIN_TOKEN", "admin-secret")
    importlib.reload(paylink)

    # The billing signature must come from the billing secret alone.
    exp = int(time.time()) + 600
    sig = paylink.sign_tenant("discord:1", exp)
    monkeypatch.setenv("JEVMOD_BILLING_SECRET", "a-different-billing-secret")
    importlib.reload(paylink)
    assert paylink.sign_tenant("discord:1", exp) != sig, "the signature must follow its own secret"

    # Neither of the other two may mint a key.
    from fastapi import HTTPException

    for wrong in ("admin-secret", "billing-secret", "a-different-billing-secret"):
        with pytest.raises(HTTPException):
            server.keymint_only(f"Bearer {wrong}")
    server.keymint_only("Bearer keymint-secret")  # the right one does not raise


def test_forgetting_a_tenant_reaches_tables_this_file_has_never_heard_of(tmp_path):
    """`/mod forget` promises everything about a server is gone, and the list of tables it deleted from was
    written by hand. The hosted service creates its own tables on this same database, so the first one added
    after that list was written was already being missed and the promise was quietly false. The tables are
    discovered from the schema now, and this asserts it by inventing one the core has never heard of."""
    from jevmod.core import Store

    store = Store(tmp_path / "forget.sqlite")
    store.set_plan("discord:1", "pro")
    store.set_plan("discord:2", "pro")
    store.db.execute("CREATE TABLE contacts (tenant TEXT PRIMARY KEY, email TEXT)")
    store.db.executemany("INSERT INTO contacts VALUES (?, ?)", [("discord:1", "a@b.c"), ("discord:2", "d@e.f")])
    store.db.commit()

    store.delete_tenant("discord:1")

    rows = store.db.execute("SELECT tenant FROM contacts").fetchall()
    assert rows == [("discord:2",)], "the other server's contact must survive"
    assert store.db.execute("SELECT COUNT(*) FROM tenants WHERE id='discord:1'").fetchone()[0] == 0


def test_no_adapter_can_ban_anybody():
    """The terms say it plainly: "The bot never bans anyone: its only actions are off, flag, delete and
    timeout." That was false on Reddit, where timeout mapped to removal plus a one day ban, because Reddit
    has no per-comment mute and somebody reached for the nearest thing. A promise in the binding document
    that the code contradicts is worse than no promise, so this asserts it across every adapter rather than
    trusting a comment."""
    from pathlib import Path

    adapters = Path(__file__).resolve().parents[1] / "jevmod" / "adapters"
    files = list(adapters.glob("*_bot.py"))
    assert files, "no adapters found, so this test is not checking what it thinks"

    for f in files:
        code = "\n".join(
            line for line in f.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in ("banned.add", ".ban(", "ban_reason", ".kick("):
            assert forbidden not in code, f"{f.name} can ban or kick somebody: {forbidden}"
