"""What came before a message, bounded so a long-running bot cannot grow without limit.

A moderation question is often not answerable from one message. "shut up already" is banter between
two people who have been joking for ten minutes and a pile-on when it is the fourth person to say it
in twenty seconds. The engine has never been able to tell those apart, because `Message` carries the
text and nothing around it.

This module is the window, and only the window. Whether the window improves a verdict is JEV-18's
question and needs JEV-5's harness; shipping the capability and shipping the claim are separate
things and this file is the first one.

**The window is ten messages.** Not a guess: `benchmark/BATCH_EFFECT.md` section 7 measured spam
recall at the shipped threshold across batch sizes on the same 300 messages, 17.3% at one, 32.0% at
five, 38.7% at ten and 37.3% at twenty-five. It saturates at ten, and twenty-five costs 2.5 times
more per judged message to get slightly less. Ten is where the measurement stops paying.

**Only message text lives here.** No author, no id, no timestamp beyond what the bound needs.
`AGENTS.md` and the privacy notice both promise that only message text and the channel topic reach
TypeSafe, and context is more of the same kind of data rather than a new kind. Adding an author name
to this buffer would quietly break that promise, which is why the buffer physically cannot hold one.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

# Measured, see the module docstring. A server may ask for less; it cannot ask for more without
# changing this number and re-running benchmark/batch_effect.py to justify it.
WINDOW = 10

# A hard cap on what an assembled context may cost, independent of the message count, because ten
# messages of four thousand characters is not ten messages of sixty. Tokens are estimated at four
# characters each rather than tokenised: a tokeniser would mean a dependency on the model's own
# vocabulary, which the open package does not have and should not acquire for a bound whose job is
# to be conservative. The estimate errs low per character, so the cap is applied to a number that is
# never larger than the truth.
MAX_CONTEXT_TOKENS = 600
CHARS_PER_TOKEN = 4

# How long a message stays relevant. A channel that went quiet for an hour is a new conversation,
# and pasting the last thing said before lunch under the first thing said after it would be worse
# than no context at all.
MAX_AGE_S = 900.0

# How many channels the buffer will hold at once before evicting the least recently used. A bot in
# two thousand Discord servers with twenty active channels each would otherwise hold forty thousand
# deques for as long as the process lives. This is the bound that makes the class safe to keep in a
# module-level singleton, which is how every adapter uses it.
MAX_CHANNELS = 500


class ConversationBuffer:
    """The last few messages per channel, oldest first, bounded three ways: per channel, by age, and
    by how many channels are held at all.

    Not persisted, on purpose. A restart losing the last ten messages of a conversation costs one
    slightly worse verdict; a table of everybody's recent messages is a data retention question the
    privacy notice does not currently answer, and JEV-20 to JEV-22 own that decision. In-memory only
    keeps this task inside what is already promised.
    """

    def __init__(self, window: int = WINDOW, max_age_s: float = MAX_AGE_S,
                 max_channels: int = MAX_CHANNELS) -> None:
        self.window = window
        self.max_age_s = max_age_s
        self.max_channels = max_channels
        # OrderedDict rather than dict: eviction needs an order, and `move_to_end` makes the channel
        # cache an LRU in two lines instead of a timestamp scan.
        self._channels: OrderedDict[str, deque[tuple[float, str]]] = OrderedDict()

    def add(self, channel: str, text: str, now: float | None = None) -> None:
        """Record a message as having been said. Called for every message that arrives, judged or
        not: a pre-filtered "lol" is still part of what the conversation looked like."""
        if not text:
            return
        t = time.time() if now is None else now
        q = self._channels.get(channel)
        if q is None:
            q = self._channels[channel] = deque(maxlen=self.window)
            if len(self._channels) > self.max_channels:
                self._channels.popitem(last=False)  # evict the least recently used channel
        self._channels.move_to_end(channel)
        q.append((t, text))

    def window_for(self, channel: str, exclude: str = "", now: float | None = None) -> tuple[str, ...]:
        """The recent messages in this channel, oldest first, dropping anything older than the age
        bound.

        `exclude` drops one text, because the message being judged is usually already in the buffer
        by the time it is judged, and showing a message its own text as context would tell the model
        it was said twice.
        """
        q = self._channels.get(channel)
        if not q:
            return ()
        t = time.time() if now is None else now
        self._channels.move_to_end(channel)
        return tuple(text for at, text in q if t - at <= self.max_age_s and text != exclude)

    def forget(self, channel: str) -> None:
        """Drop a channel. `/mod forget` and leaving a server both have to reach this, or the thing
        a server owner was told is deleted is still sitting in a deque."""
        self._channels.pop(channel, None)

    def forget_all(self) -> None:
        self._channels.clear()

    def channels(self) -> tuple[str, ...]:
        """The channel keys currently held. Exists so an erasure request can find every window
        belonging to a tenant without reaching into the buffer's internals, and so a test can prove
        the eviction bound by counting."""
        return tuple(self._channels)

    def __len__(self) -> int:
        return len(self._channels)


def assemble(window: tuple[str, ...], max_tokens: int = MAX_CONTEXT_TOKENS) -> tuple[str, ...]:
    """Trim a window to fit the budget, keeping the most recent messages.

    Newest-first is the direction that matters: if only three of ten fit, the three immediately
    before this message are worth more than the three from the start of the window. The result is
    returned oldest-first again, because that is the order a reader, and the model, expects.

    A single message longer than the whole budget is dropped rather than cut. Half a sentence is
    worse than no sentence: it changes what the text says instead of leaving it out.
    """
    out: list[str] = []
    spent = 0
    for text in reversed(window):
        cost = max(1, len(text) // CHARS_PER_TOKEN)
        if spent + cost > max_tokens:
            if not out and cost > max_tokens:
                break  # one oversized message; leaving it out beats truncating it
            continue
        out.append(text)
        spent += cost
    return tuple(reversed(out))
