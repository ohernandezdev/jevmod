// Offline: the m0 filler and padding discipline ported from jevmod/judge.py (the `m0` filler) and
// jevmod/core/context.py (`assemble`). No network: a fake TypeSafe client stands in for `systemOne`
// so the request shape can be inspected directly. Never mocks `normalize` or the policy.
import { describe, expect, it } from "vitest";
import type { TypeSafeClient } from "@typesafe-ai/sdk";
import { Judge, LEAD_FILLER, Moderator, assemble, type Message } from "../src/index.js";

interface Call {
  state: { messages: Record<string, { text: string; channel_topic: string }>; custom_rules: Record<string, string> };
  questions: Record<string, unknown>;
}

/** A fake client whose `systemOne` records the request and answers every noul question with 0.1. */
function fakeClient(): { calls: Call[]; client: TypeSafeClient } {
  const calls: Call[] = [];
  const client = {
    systemOne: async (request: Call) => {
      calls.push(request);
      const answers: Record<string, { type: "noul"; noul: number }> = {};
      for (const key of Object.keys(request.questions)) answers[key] = { type: "noul", noul: 0.1 };
      return { answers, usage: { input_tokens: 42 } };
    },
  } as unknown as TypeSafeClient;
  return { calls, client };
}

const msg = (id: string, text: string): Message => ({ id, text, channelTopic: "general chat" });

describe("Judge padding and the m0 filler", () => {
  it("a single message lands at m1, with the lead filler at m0, when there is no padding", async () => {
    const { calls, client } = fakeClient();
    const j = new Judge({ client });
    await j.judge([msg("a", "this is a perfectly ordinary sentence")], ["spam"]);
    expect(calls).toHaveLength(1);
    const state = (calls[0] as Call).state;
    expect(state.messages["m0"]?.text).toBe(LEAD_FILLER);
    expect(state.messages["m1"]?.text).toBe("this is a perfectly ordinary sentence");
  });

  it("padding fills m0 instead of the filler when available, one verdict per message", async () => {
    const { calls, client } = fakeClient();
    const j = new Judge({ client });
    const verdicts = await j.judge(
      [msg("a", "this is a perfectly ordinary sentence")],
      ["spam"],
      {},
      ["earlier message one", "earlier message two"],
    );
    expect(verdicts).toHaveLength(1);
    expect(verdicts[0]?.messageId).toBe("a");
    const state = (calls[0] as Call).state;
    // padding is oldest-first; m0 takes the oldest surviving item and newer padding trails after
    // the real messages, matching jevmod/judge.py's `pad.pop(0)`.
    expect(state.messages["m0"]?.text).toBe("earlier message one");
    expect(state.messages["m2"]?.text).toBe("earlier message two");
  });

  it("padding that duplicates a judged message is dropped", async () => {
    const { calls, client } = fakeClient();
    const j = new Judge({ client });
    await j.judge(
      [msg("a", "this is a perfectly ordinary sentence")],
      ["spam"],
      {},
      ["this is a perfectly ordinary sentence", "distinct earlier message"],
    );
    const state = (calls[0] as Call).state;
    const texts = Object.values(state.messages).map((m) => m.text);
    // the duplicate never appears twice: once as m1 (the real message), never again as padding
    expect(texts.filter((t) => t === "this is a perfectly ordinary sentence")).toHaveLength(1);
    expect(state.messages["m0"]?.text).toBe("distinct earlier message");
  });

  it("an oversized padding item is skipped while the rest survive (continue, not break)", () => {
    const oversized = "x".repeat(10_000); // far larger than any reasonable budget
    const items = ["small one", oversized, "small two"];
    const result = assemble(items, 10); // tiny budget: only short items fit
    expect(result).toContain("small one");
    expect(result).toContain("small two");
    expect(result).not.toContain(oversized);
  });

  it("questions exist for every position in the state, including m0", async () => {
    const { calls, client } = fakeClient();
    const j = new Judge({ client });
    await j.judge(
      [msg("a", "message one here please"), msg("b", "message two here please")],
      ["spam"],
      {},
      ["padding item one", "padding item two"],
    );
    const call = calls[0] as Call;
    const positions = Object.keys(call.state.messages).length;
    for (let i = 0; i < positions; i++) {
      expect(call.questions).toHaveProperty(`spam_${i}`);
    }
    // m0 and any trailing padding are asked about but never appear in the returned verdicts
    const verdicts = await j.judge([msg("c", "a distinct message here please")], ["spam"]);
    expect(verdicts).toHaveLength(1);
  });

  it("the returned array has exactly one verdict per input message regardless of padding size", async () => {
    const { client } = fakeClient();
    const j = new Judge({ client });
    const inputs = [msg("x", "first real message content"), msg("y", "second real message content")];
    const padding = Array.from({ length: 20 }, (_, i) => `padding message number ${i}`);
    const verdicts = await j.judge(inputs, ["spam"], {}, padding);
    expect(verdicts).toHaveLength(2);
    expect(verdicts.map((v) => v.messageId)).toEqual(["x", "y"]);
  });
});

describe("the cache and the neighbours", () => {
  it("the same text with different padding is judged again, not served from the cache", async () => {
    const { calls, client } = fakeClient();
    const j = new Judge({ client });
    const one = [msg("a", "this is a perfectly ordinary sentence")];

    await j.judge(one, ["spam"], {}, ["first conversation"]);
    await j.judge(one, ["spam"], {}, ["a completely different conversation"]);
    // Two requests, because the neighbours are part of what produced the score: JEV-56 measured 12%
    // of spam positives crossing their threshold when the same text is regrouped, against 2.7% for
    // a request repeated unchanged. A cache that ignored the padding would hand the first score back
    // under the second conversation.
    expect(calls).toHaveLength(2);

    const again = await j.judge(one, ["spam"], {}, ["first conversation"]);
    expect(calls).toHaveLength(2); // the identical request does hit, which is what the cache is for
    expect(again[0]?.reason).toBe("cache");
  });
});

describe("Moderator passes the window through", () => {
  it("check() puts the caller's padding in the request and still returns one decision", async () => {
    const { calls, client } = fakeClient();
    const mod = new Moderator({ judge: new Judge({ client }) });
    const decisions = await mod.checkMany(["this is a perfectly ordinary sentence"], {
      channelTopic: "general chat",
      padding: ["earlier thing somebody said", "and the one before that"],
    });
    expect(decisions).toHaveLength(1);
    const state = (calls[0] as Call).state;
    // The oldest padding item takes m0, the judged message is at m1, the rest trails behind it.
    expect(state.messages["m0"]?.text).toBe("earlier thing somebody said");
    expect(state.messages["m1"]?.text).toBe("this is a perfectly ordinary sentence");
    expect(state.messages["m2"]?.text).toBe("and the one before that");
  });

  it("without padding the filler still keeps the judged message off m0", async () => {
    const { calls, client } = fakeClient();
    const mod = new Moderator({ judge: new Judge({ client }) });
    await mod.check("this is a perfectly ordinary sentence", { channelTopic: "general chat" });
    const state = (calls[0] as Call).state;
    expect(state.messages["m0"]?.text).toBe(LEAD_FILLER);
    expect(state.messages["m1"]?.text).toBe("this is a perfectly ordinary sentence");
  });
});
