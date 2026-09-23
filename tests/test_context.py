"""The conversation window: bounds, assembly, the cache key, and what erasure has to reach.

No key needed. Everything here is pure code except one test that captures the request the judge
would have sent, which it does by handing `Judge` a fake client rather than by calling Jev.

The bounds are each proved by exceeding them. A test that stays inside a limit proves the limit was
not in the way, which is not the same as proving it holds.
"""

import time

import pytest

from jevmod.core.context import (
    CHARS_PER_TOKEN,
    MAX_CONTEXT_TOKENS,
    WINDOW,
    ConversationBuffer,
    assemble,
)
from jevmod.core.service import ModerationService
from jevmod.core.store import Store
from jevmod.judge import Judge, Message, Verdict, _key


def test_the_window_holds_the_last_ten_and_drops_the_eleventh():
    """Ten is measured, not chosen: benchmark/BATCH_EFFECT.md section 7 has spam recall saturating
    there. If this number moves, that measurement has to move with it."""
    b = ConversationBuffer()
    assert WINDOW == 10
    for i in range(15):
        b.add("c", f"message {i}")
    w = b.window_for("c")
    assert len(w) == WINDOW
    assert w[0] == "message 5" and w[-1] == "message 14", "oldest first, and the oldest five are gone"


def test_a_message_older_than_the_age_bound_is_not_context():
    """A channel that went quiet for an hour is a new conversation. Pasting the last thing said
    before lunch under the first thing after it is worse than sending nothing."""
    b = ConversationBuffer(max_age_s=900)
    now = time.time()
    b.add("c", "said ages ago", now=now - 1000)
    b.add("c", "said just now", now=now - 10)
    assert b.window_for("c", now=now) == ("said just now",)


def test_the_number_of_channels_held_is_bounded_and_evicts_the_least_recent():
    """The bound that makes this safe as a module-level singleton. A bot in two thousand servers
    with twenty channels each would otherwise hold forty thousand deques for the life of the
    process."""
    b = ConversationBuffer(max_channels=3)
    for c in ("a", "b", "c"):
        b.add(c, "hello there friend")
    b.window_for("a")  # touching it makes it the most recently used
    b.add("d", "hello there friend")
    assert len(b) == 3
    assert "b" in b.channels() or "c" in b.channels()
    assert "a" in b.channels(), "the one that was read must survive an eviction"
    assert "d" in b.channels()


def test_a_message_is_never_its_own_context():
    """The buffer holds the message being judged by the time it is judged, and showing a message its
    own text would tell the model somebody said it twice."""
    b = ConversationBuffer()
    b.add("c", "first thing")
    b.add("c", "the message being judged")
    assert b.window_for("c", exclude="the message being judged") == ("first thing",)


def test_assemble_keeps_the_newest_and_returns_them_oldest_first():
    # A third of the budget each, plus four characters of prefix, so exactly two of the three fit
    # and the arithmetic is worked out here rather than asserted loosely with an `or`.
    long = "x" * (MAX_CONTEXT_TOKENS * CHARS_PER_TOKEN // 3)
    out = assemble((f"old {long}", f"mid {long}", f"new {long}"))
    assert out == (f"mid {long}", f"new {long}"), "the two newest, still in the order they were said"
    tight = assemble(("a" * 4000, "b" * 4000, "keep me"), max_tokens=10)
    assert tight == ("keep me",), "the newest that fits, and nothing that does not"


def test_assemble_drops_an_oversized_message_rather_than_cutting_it():
    """Half a sentence changes what the text says. Leaving it out only leaves it out."""
    assert assemble(("y" * 100_000,), max_tokens=10) == ()


def test_the_cache_key_changes_with_the_context_and_repeats_with_it():
    """The point of the whole change. JEV-56 measured that the same text scored in a different batch
    crosses its threshold 12% of the time, so a key that ignores context is asserting something
    measurably false. It still has to hit on a spam wave, which is the same text in the same window."""
    base = ("hello", "world", ["spam"], {})
    a = _key(*base, ("one", "two"))
    assert a != _key(*base, ("one", "three")), "a different conversation is a different question"
    assert a != _key(*base, ()), "no context is not the same as some context"
    assert a == _key(*base, ("one", "two")), "the same window still hits, which is the spam wave"


def test_two_windows_cannot_collide_through_the_separator():
    """The join uses a character no message text contains. With a comma, ('a,b',) and ('a','b')
    would be the same key and two different conversations would share a verdict."""
    base = ("hello", "world", ["spam"], {})
    assert _key(*base, ("a,b",)) != _key(*base, ("a", "b"))


def _service(tmp_path):
    return ModerationService(Store(tmp_path / "ctx.sqlite"), judge=Judge(cache_ttl_s=0))


def _skipped(msgs):
    """One verdict per message, which is the contract `_judge_batch` zips against strictly."""
    return [Verdict(m.id, {}, False, "test") for m in msgs]


def test_the_service_fills_context_from_earlier_batches_and_not_from_within_one(tmp_path, monkeypatch):
    """Messages sharing a batch already sit at neighbouring positions in the same request. Adding
    them as context as well would show the model the same sentence twice."""
    svc = _service(tmp_path)
    seen: list[list[Message]] = []
    monkeypatch.setattr(svc.judge, "judge", lambda msgs, *a, **k: (seen.append(list(msgs)), _skipped(msgs))[1])
    t = "discord:1"
    svc.moderate(t, [Message("1", "the first thing anybody said", channel="general")])
    svc.moderate(t, [Message("2", "the second thing said here", channel="general"),
                     Message("3", "the third thing said here", channel="general")])
    second = {m.id: m for m in seen[-1]}
    assert second["2"].context == ("the first thing anybody said",)
    assert second["3"].context == ("the first thing anybody said",), "not the second: same batch"


def test_channels_do_not_share_a_window(tmp_path, monkeypatch):
    """Only Discord splits a tenant into channels, and without this the window pastes #general under
    a message in #support."""
    svc = _service(tmp_path)
    seen: list[list[Message]] = []
    monkeypatch.setattr(svc.judge, "judge", lambda msgs, *a, **k: (seen.append(list(msgs)), _skipped(msgs))[1])
    t = "discord:1"
    svc.moderate(t, [Message("1", "something said in general", channel="general")])
    svc.moderate(t, [Message("2", "something said in support", channel="support")])
    assert seen[-1][0].context == ()


def test_erasure_reaches_the_window(tmp_path, monkeypatch):
    """`/mod forget`, leaving a server and DELETE /v1/tenant all promise the data is gone. The
    window is in memory, so deleting rows does not reach it."""
    svc = _service(tmp_path)
    monkeypatch.setattr(svc.judge, "judge", lambda msgs, *a, **k: _skipped(msgs))
    svc.moderate("discord:1", [Message("1", "something worth remembering", channel="general")])
    svc.moderate("discord:2", [Message("2", "another server entirely here", channel="general")])
    assert len(svc.context) == 2
    svc.forget_context("discord:1")
    assert svc.context.channels() == ("discord:2␟general",), "only the tenant asked for"


def test_an_inactive_tenant_is_not_buffered(tmp_path, monkeypatch):
    """A server whose messages are not evaluated should not have them held in memory either."""
    monkeypatch.setattr("jevmod.core.service.ENFORCE_PLANS", True)
    svc = _service(tmp_path)
    monkeypatch.setattr(svc.judge, "judge", lambda msgs, *a, **k: _skipped(msgs))
    svc.moderate("discord:1", [Message("1", "nothing should be kept", channel="general")])
    assert len(svc.context) == 0


def test_the_context_reaches_the_request_and_nothing_else_does(tmp_path):
    """The privacy promise, asserted against the bytes that would go out rather than against the
    docstring that describes them: text, channel topic and context, and no author or channel."""
    captured: dict = {}

    class FakeClient:
        def system_one(self, state, questions):
            captured.update(state=state, questions=questions)
            raise RuntimeError("stop here; the request is what this test is about")

    j = Judge(client=FakeClient(), cache_ttl_s=0)
    m = Message("1", "the message being judged", author="user-42", channel="chan-7",
                channel_topic="support", context=("what came before",))
    with pytest.raises(RuntimeError):
        j.judge([m], ["spam"])
    sent = captured["state"]["messages"]["m0"]
    assert sent["text"] == "the message being judged"
    assert sent["channel_topic"] == "support"
    assert sent["context"] == {"c0": "what came before"}
    blob = repr(captured["state"])
    assert "user-42" not in blob and "chan-7" not in blob, "author and channel are local only"


def test_a_message_with_no_context_is_sent_exactly_as_it_was_before(tmp_path):
    """Nobody with an empty window pays a token for an empty dict."""
    captured: dict = {}

    class FakeClient:
        def system_one(self, state, questions):
            captured.update(state=state)
            raise RuntimeError("stop")

    j = Judge(client=FakeClient(), cache_ttl_s=0)
    with pytest.raises(RuntimeError):
        j.judge([Message("1", "a message with no history")], ["spam"])
    assert "context" not in captured["state"]["messages"]["m0"]
