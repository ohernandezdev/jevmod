"""The conversation window: bounds, assembly, the cache key, and what erasure has to reach.

No key needed. Everything here is pure code except one test that captures the request the judge
would have sent, which it does by handing `Judge` a fake client rather than by calling Jev.

The bounds are each proved by exceeding them. A test that stays inside a limit proves the limit was
not in the way, which is not the same as proving it holds.
"""

import os
import time

import pytest
from typesafe_sdk import NoulAnswer

from jevmod.core.context import (
    CHARS_PER_TOKEN,
    MAX_CONTEXT_TOKENS,
    WINDOW,
    ConversationBuffer,
    assemble,
)
from jevmod.core.service import ModerationService
from jevmod.core.store import Store
from jevmod.judge import PAD_TO, Judge, Message, Verdict, _key


def test_the_window_holds_the_last_ten_and_drops_the_eleventh():
    """Ten is measured, not chosen: benchmark/BATCH_EFFECT.md section 7 has spam recall saturating
    there. If this number moves, that measurement has to move with it."""
    b = ConversationBuffer()
    assert WINDOW == 10
    for i in range(15):
        b.add(("t", "c"), f"message {i}")
    w = b.window_for(("t", "c"))
    assert len(w) == WINDOW
    assert w[0] == "message 5" and w[-1] == "message 14", "oldest first, and the oldest five are gone"


def test_a_message_older_than_the_age_bound_is_not_context():
    """A channel that went quiet for an hour is a new conversation. Pasting the last thing said
    before lunch under the first thing after it is worse than sending nothing."""
    b = ConversationBuffer(max_age_s=900)
    now = time.time()
    b.add(("t", "c"), "said ages ago", now=now - 1000)
    b.add(("t", "c"), "said just now", now=now - 10)
    assert b.window_for(("t", "c"), now=now) == ("said just now",)


def test_the_number_of_channels_held_is_bounded_and_evicts_the_least_recent():
    """The bound that makes this safe as a module-level singleton. A bot in two thousand servers
    with twenty channels each would otherwise hold forty thousand deques for the life of the
    process."""
    b = ConversationBuffer(max_channels=3)
    for c in ("a", "b", "c"):
        b.add(("t", c), "hello there friend")
    b.window_for(("t", "a"))  # touching it makes it the most recently used
    b.add(("t", "d"), "hello there friend")
    assert len(b) == 3
    assert ("t", "b") in b.channels() or ("t", "c") in b.channels()
    assert ("t", "a") in b.channels(), "the one that was read must survive an eviction"
    assert ("t", "d") in b.channels()


def test_a_message_is_never_its_own_context():
    """The buffer holds the message being judged by the time it is judged, and showing a message its
    own text would tell the model somebody said it twice."""
    b = ConversationBuffer()
    b.add(("t", "c"), "first thing")
    b.add(("t", "c"), "the message being judged")
    assert b.window_for(("t", "c"), exclude="the message being judged") == ("first thing",)


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
    assert svc.context.channels() == (("discord:2", "general"),), "only the tenant asked for"


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
    sent = captured["state"]["messages"]["m1"]  # m0 is the lead filler; real messages start at m1
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
    assert "context" not in captured["state"]["messages"]["m1"]


# ------------------------------------------------------------------ JEV-57: padding a small batch


class _Capture:
    """A client that records the request and answers every question with the same number, so a test
    can tell a real verdict from a padded position by which ones come back at all."""

    def __init__(self, value: float = 0.5) -> None:
        self.state: dict = {}
        self.questions: dict = {}
        self.value = value

    def system_one(self, state, questions):
        self.state, self.questions = state, questions

        class Resp:
            def __init__(self, qs, v):
                # A real NoulAnswer: `_p` rejects anything else, and a fake that `_p` would accept
                # would be a fake of the check rather than of the answer.
                self.answers = {k: NoulAnswer(noul=v) for k in qs}
                self.usage = None

        return Resp(questions, self.value)


def test_padding_is_asked_about_and_never_answered_for():
    """The whole mechanism. `benchmark/batch_context.py` measured that neighbours present but not
    asked about recover 23% of the gap, so the padding has to carry questions; and nothing about it
    may reach a verdict, which is why the discard lives in `judge` rather than in the caller."""
    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    out = j.judge([Message("1", "the one real message here")], ["spam"],
                  padding=("first bit of history", "second bit", "third bit"))
    assert len(out) == 1 and out[0].message_id == "1"
    assert len(c.state["messages"]) == 4, "one real message and three padded ones in the request"
    assert set(c.questions) == {f"spam_{i}" for i in range(4)}, "every position is asked"
    assert j.judged_messages == 1, "usage counts the real message only"


def test_padding_never_exceeds_the_measured_size():
    from jevmod.judge import PAD_TO

    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    j.judge([Message("1", "the one real message here")], ["spam"],
            padding=tuple(f"filler number {i}" for i in range(50)))
    assert len(c.state["messages"]) == PAD_TO


def test_padding_carries_no_context_and_no_duplicate_of_a_judged_message():
    """The buffer holds the batch by the time it is judged, so the window would otherwise hand a
    message back its own text as padding and tell the model somebody said it twice."""
    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    j.judge([Message("1", "the one real message here", context=("earlier",))], ["spam"],
            padding=("the one real message here", "something genuinely else"))
    texts = [m["text"] for m in c.state["messages"].values()]
    assert texts.count("the one real message here") == 1
    # The duplicate is dropped, so the one usable padding item becomes the lead at m0 and the real
    # message sits at m1. Two positions in total, which is what the filter is supposed to leave.
    assert set(c.state["messages"]) == {"m0", "m1"}
    assert c.state["messages"]["m0"]["text"] == "something genuinely else"
    assert "context" not in c.state["messages"]["m0"], "padding is context, it does not carry its own"


def test_a_padded_verdict_is_not_cached_under_the_padding(tmp_path):
    """A padded position has no id, so it cannot reach the cache. Asserted rather than assumed,
    because a cache entry for text nobody asked about would be served to somebody later."""
    c = _Capture()
    j = Judge(client=c, cache_ttl_s=86400)
    j.judge([Message("1", "the one real message here")], ["spam"], padding=("something genuinely else",))
    assert len(j.cache) == 1


def test_the_service_pads_a_small_batch_and_leaves_a_full_one_alone(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    seen: list[tuple] = []

    def fake(msgs, cats, rules=None, padding=()):
        seen.append(padding)
        return _skipped(msgs)

    monkeypatch.setattr(svc.judge, "judge", fake)
    t = "discord:1"
    for i in range(4):
        svc.moderate(t, [Message(f"a{i}", f"an earlier message number {i}", channel="general")])
    svc.moderate(t, [Message("x", "the message under test", channel="general")])
    assert len(seen[-1]) == 4, "the four earlier messages become padding"
    from jevmod.judge import PAD_TO

    svc.moderate(t, [Message(f"b{i}", f"a message in a full batch {i}", channel="general")
                     for i in range(PAD_TO)])
    assert seen[-1] == (), "a batch that already fills the request is not padded"


def test_the_service_does_not_pad_across_channels(tmp_path, monkeypatch):
    """A Discord guild's two seconds can hold #general and #support. Padding that from one of them
    is picking a channel arbitrarily and calling it context."""
    svc = _service(tmp_path)
    seen: list[tuple] = []
    monkeypatch.setattr(svc.judge, "judge",
                        lambda msgs, cats, rules=None, padding=(): (seen.append(padding), _skipped(msgs))[1])
    t = "discord:1"
    svc.moderate(t, [Message("a", "an earlier message in general", channel="general")])
    svc.moderate(t, [Message("b", "one message from general here", channel="general"),
                     Message("c", "one message from support here", channel="support")])
    assert seen[-1] == ()


def test_the_switch_turns_padding_off(tmp_path, monkeypatch):
    """`JEVMOD_PAD_BATCH=0` buys back the cheaper, worse version. The number it trades away is
    twenty-one points of spam recall, written next to the switch."""
    monkeypatch.setattr("jevmod.core.service.PAD_BATCH", False)
    svc = _service(tmp_path)
    seen: list[tuple] = []
    monkeypatch.setattr(svc.judge, "judge",
                        lambda msgs, cats, rules=None, padding=(): (seen.append(padding), _skipped(msgs))[1])
    t = "discord:1"
    svc.moderate(t, [Message("a", "an earlier message in general", channel="general")])
    svc.moderate(t, [Message("b", "the message under test here", channel="general")])
    assert seen[-1] == ()


def test_no_real_message_ever_sits_at_position_zero():
    """The measurement that reframed JEV-57. A message at m0 gains nothing from its neighbours,
    +0.014 in a request of ten against about +0.22 everywhere else, and it is asymmetric: spam
    positives lose 0.15 there while clean text moves 0.01. Index zero costs recall and buys no
    precision (`benchmark/position_zero.py`, 300 messages).

    A message judged by itself is always at m0, which turned out to be the whole of the effect the
    issue was opened on.
    """
    from jevmod.judge import LEAD_FILLER

    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    j.judge([Message("1", "the one real message here")], ["spam"])
    assert c.state["messages"]["m0"]["text"] == LEAD_FILLER, "no history, so the constant"
    assert c.state["messages"]["m1"]["text"] == "the one real message here"

    c2 = _Capture()
    j2 = Judge(client=c2, cache_ttl_s=0)
    j2.judge([Message(str(i), f"a real message number {i}") for i in range(4)], ["spam"],
             padding=("what was said before",))
    assert c2.state["messages"]["m0"]["text"] == "what was said before", "history beats the constant"
    real = {m["text"] for k, m in c2.state["messages"].items() if k != "m0"}
    assert real == {f"a real message number {i}" for i in range(4)}


def test_the_lead_filler_never_reaches_a_verdict_or_the_cache():
    from jevmod.judge import LEAD_FILLER

    c = _Capture()
    j = Judge(client=c, cache_ttl_s=86400)
    out = j.judge([Message("1", "the one real message here")], ["spam"])
    assert [v.message_id for v in out] == ["1"]
    assert len(j.cache) == 1, "one entry, for the one real message"
    assert LEAD_FILLER not in {m["text"] for k, m in c.state["messages"].items() if k != "m0"}


# ------------------------------------------------------ what the QA pass found, one test each


def test_an_oversized_newest_message_does_not_take_the_window_with_it():
    """It was `break`, so a wall of pasted spam arriving just before a message left that message
    with no context at all. The feature turned itself off exactly when it would have helped."""
    long = "x" * (MAX_CONTEXT_TOKENS * CHARS_PER_TOKEN * 2)
    assert assemble(("short one", "short two", long)) == ("short one", "short two")


def test_the_cache_key_cannot_be_spoofed_by_text_a_user_can_type():
    """The key joined the window with U+241F, described in the code as a character no message
    contains. `normalize` does not strip it and the buffer stores raw text, so one message could
    collapse two different conversations into one key and be served the other's verdict."""
    base = ("hello", "world", ["spam"], {})
    assert _key(*base, ("a", "b")) != _key(*base, ("a␟b",))
    assert _key("x|general", "chat", ["spam"], {}) != _key("x", "general|chat", ["spam"], {})


def test_context_is_normalised_on_the_way_out():
    """Every other piece of text on the request goes through `normalize`. This one did not, so
    zalgo, fullwidth and enclosed alphanumerics reached Jev raw through the context field."""
    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    j.judge([Message("1", "the message being judged", context=("ｆｕｌｌｗｉｄｔｈ",))], ["spam"])
    assert c.state["messages"]["m1"]["context"] == {"c0": "fullwidth"}


def test_padding_is_bounded_by_length_and_not_only_by_count():
    """`PAD_TO` bounds how many positions the padding takes and used to bound nothing about their
    size, so a window of long pastes sent tens of kilobytes and asked every category about each.
    The documented cost of padding assumed chat-sized messages and nothing enforced it."""
    c = _Capture()
    j = Judge(client=c, cache_ttl_s=0)
    j.judge([Message("1", "the message being judged")], ["spam"],
            padding=tuple(f"{i} " + "y" * 4000 for i in range(9)))
    # Asserted in tokens, because tokens are what the budget is in and `assemble`'s estimate rounds
    # down per message. Before the fix this was unbounded: nine four-thousand character pastes went
    # out whole, about 8,800 tokens, with every category asked about each of them.
    sent = sum(len(m["text"]) // CHARS_PER_TOKEN
               for k, m in c.state["messages"].items() if k != "m1")
    assert sent <= MAX_CONTEXT_TOKENS * PAD_TO, f"{sent} tokens of padding"


def test_the_buffer_survives_being_used_from_several_threads():
    """Every adapter calls `moderate` through `asyncio.to_thread`, so two tenants are two threads.
    `add` appending while `window_for` iterated raised "deque mutated during iteration", and two
    threads racing the insert-then-evict raised KeyError on a channel the other had just evicted."""
    import threading

    b = ConversationBuffer(max_channels=8)
    errors: list[str] = []

    def hammer(n: int) -> None:
        try:
            for i in range(600):
                b.add(("t", f"c{(n + i) % 40}"), f"message number {i}")
                b.window_for(("t", f"c{i % 40}"))
        except Exception as exc:  # noqa: BLE001 - the point of the test is that there are none
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


def test_the_padding_switch_understands_off():
    """`JEVMOD_PAD_BATCH=off` used to leave padding on and say nothing, so an operator who believed
    they had turned off eight times the model spend had not."""
    import importlib

    import jevmod.core.service as svc

    for value, expected in (("off", False), ("0", False), ("no", False), ("", False),
                            ("1", True), ("on", True), ("banana", True)):
        os.environ["JEVMOD_PAD_BATCH"] = value
        assert importlib.reload(svc).PAD_BATCH is expected, value
    os.environ.pop("JEVMOD_PAD_BATCH", None)
    importlib.reload(svc)


def test_forget_context_cannot_reach_another_tenant(tmp_path, monkeypatch):
    """The window key was `f"{tenant}<sep>{channel}"`, so a tenant named `acme<sep>evil` was emptied
    by `forget_context("acme")`. Only the admin key-minting API can create such a tenant, and a key
    that cannot be spoofed costs nothing."""
    svc = _service(tmp_path)
    monkeypatch.setattr(svc.judge, "judge", lambda msgs, *a, **k: _skipped(msgs))
    svc.moderate("acme", [Message("1", "something said by the real tenant")])
    svc.moderate("acme␟evil", [Message("2", "something said by the other one")])
    assert len(svc.context) == 2
    svc.forget_context("acme")
    assert svc.context.channels() == (("acme␟evil", ""),)
