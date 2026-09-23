"""Real Jev calls on realistic messages. Checks the categories, the pre-filters and the cache."""

import pytest
from conftest import KEY, NO_KEY_REASON

from jevmod.judge import CATEGORIES, Judge, Message, prefilter

pytestmark = pytest.mark.skipif(not KEY, reason=NO_KEY_REASON)

CATS = list(CATEGORIES)

SAMPLES = [
    # (id, text, expected category with p>=0.7, or None for clean)
    ("1", "🔥 FREE NITRO for the first 100 people!! claim at discord-gifts.ru/nitro 🔥", "scam"),
    ("2", "Anyone know if the new patch fixed the inventory bug? Mine still duplicates items.", None),
    ("3", "DM me if you want to double your ETH in 24h, guaranteed, I work with Binance support", "scam"),
    ("4", "Check out my server!! Best giveaways!! discord.gg/xxxx join join join", "spam"),
    ("5", "you're a worthless idiot and everyone here hates you, just leave", "harassment"),
    ("6", "gg everyone, that raid was clean. same time tomorrow?", None),
    ("7", "lol", None),  # prefilter: too short
    ("8", "Hi all, I'm new here, where do I find the rules?", None),
    ("9", "I don't want to be here anymore. nobody would notice if I was gone. sorry everyone", "selfharm"),
    ("10", "this boss fight makes me want to die lol, 40 tries", None),
    ("11", "lol his real name is Marcos Pereira, lives at Calle Colón 14 in Valencia, phone 612 33 44 55", "doxxing"),
    (
        "12",
        "hey you said you're 13? you seem mature, add me on snap, don't tell your parents, I'll send you gifts",
        "minors",
    ),
    ("13", "my daughter is 12 and loves this game, any tips on parental controls?", None),
]


def test_prefilter_skips_trusted_and_tiny_messages():
    assert prefilter(Message("a", "lol")) == "too short"
    assert prefilter(Message("b", "buy now http://x.y")) is None  # links are never too short
    assert prefilter(Message("c", "long enough message here", author_trusted=True)) == "trusted author"
    assert prefilter(Message("d", "   ")) == "empty"


def test_categories_on_realistic_messages():
    j = Judge()
    msgs = [Message(i, t, channel_topic="gaming community general chat") for i, t, _ in SAMPLES]
    verdicts = j.judge(msgs, CATS)
    assert j.requests == 1  # one Jev request for the whole batch
    by_id = {v.message_id: v for v in verdicts}
    for i, text, expected in SAMPLES:
        v = by_id[i]
        if i == "7":
            assert not v.judged and v.reason == "too short"
            continue
        assert v.judged, (i, v.reason)
        if expected is None:
            assert all(p < 0.5 for p in v.scores.values()), (text, v.scores)
        else:
            assert v.scores[expected] >= 0.7, (text, v.scores)
    print(f"\n{j.judged_messages} messages, {j.input_tokens} tokens, ${j.input_tokens * 0.042 / 1e6:.6f}")


def test_cache_reuses_verdicts_and_custom_rules_work():
    j = Judge()
    rules = {"no_politics": "No political discussion in this server."}
    m = [
        Message("p1", "Who are you all voting for in the election next month? The left is destroying this country."),
        Message("p2", "Which GPU should I get for 1440p, the 5070 or wait for the 5080?"),
    ]
    v1 = j.judge(m, ["spam"], rules)
    assert v1[0].custom["no_politics"] >= 0.7 and v1[1].custom["no_politics"] < 0.4, [x.custom for x in v1]
    v2 = j.judge(m, ["spam"], rules)
    assert j.requests == 1 and all(x.reason == "cache" for x in v2)


def test_context_changes_a_verdict_that_one_message_cannot_settle():
    """JEV-17. The reason the context engine exists, asserted against the live model.

    "yeah she totally deserves it lol" is an opinion about a game on its own and the fourth voice in
    a pile-on when three people just agreed to drive somebody off the server. Judged alone the
    engine cannot tell those apart, because until this change it only ever saw the one line.

    The margin is 0.25 against a measured delta of 0.43 to 0.48 over three runs on 2026-09-23, which
    is roughly twice the headroom. `AGENTS.md` asks for a margin rather than a value here for the
    reason `benchmark/BATCH_EFFECT.md` measured: an exact number is not reproducible, a direction
    this size is.
    """
    text = "yeah she totally deserves it lol"
    pile_on = (
        "everyone go spam her dms",
        "shes such a waste of space",
        "lets make her leave the server",
    )
    j = Judge(cache_ttl_s=0)  # the window is part of the cache key, but say so rather than rely on it
    alone = j.judge([Message("1", text)], ["harassment"])[0]
    in_context = j.judge([Message("1", text, context=pile_on)], ["harassment"])[0]
    assert alone.judged and in_context.judged
    assert in_context.scores["harassment"] - alone.scores["harassment"] >= 0.25, (
        f"context should raise harassment here; got {alone.scores['harassment']:.2f} alone and "
        f"{in_context.scores['harassment']:.2f} in context"
    )


def test_the_same_text_in_a_different_conversation_is_not_a_cache_hit():
    """The cache key includes the window, so a verdict formed in one conversation is not handed back
    in another. Measured reason in `_key`'s docstring: JEV-56 found 12% of spam positives cross
    their threshold when the same text is scored among different neighbours."""
    j = Judge()  # the real 24 hour TTL: the point is that the key misses, not that it expired
    text = "yeah she totally deserves it lol"
    first = j.judge([Message("1", text, context=("talking about the boss fight",))], ["harassment"])[0]
    again = j.judge([Message("2", text, context=("talking about the boss fight",))], ["harassment"])[0]
    other = j.judge([Message("3", text, context=("lets make her leave the server",))], ["harassment"])[0]
    assert first.reason == "jev" and again.reason == "cache", "the same window must still hit"
    assert other.reason == "jev", "a different window must not"
