// The judgment core, ported from jevmod/judge.py: a batch of messages in, one Jev request, a probability per
// category per message out. Cost controls live here: local pre-filters decide what is worth judging, a cache
// reuses verdicts for repeated text, and only the categories a policy enabled are asked.
//
// Messages go to Jev as a dict keyed by position (`messages.m3.text`), not a list: with a list, probabilities
// leaked between neighbouring positions in multilingual batches. See the Python module for the full findings.
import { createHash } from "node:crypto";
import { TypeSafeClient, noul, type NoulQuestion, type NoulResponse } from "@typesafe-ai/sdk";
import { CATEGORIES, isCategory, type CategoryName } from "./categories.js";
import { normalize, prefilter, type Message } from "./normalize.js";

export type { Message } from "./normalize.js";

export type Scores = Record<string, number>;

// How many messages a padded request holds in total. Measured, not chosen: spam recall at the
// shipped threshold is 17.3% with one message in the request, 32.0% with five, 38.7% with ten and
// 37.3% with twenty-five (jevmod/benchmark/BATCH_EFFECT.md section 7, on 300 real messages). It
// saturates at ten, and twenty-five costs 2.5 times more per judged message to get slightly less.
// Changing this number means re-running benchmark/batch_effect.py, not arguing about it.
export const PAD_TO = 10;

// What sits at `messages.m0` when there is no padding to put there instead. Deliberately
// ordinary: a sentence no moderator would ever act on, in the register a chat channel is in, and
// short enough that paying for it is not the point. It is never judged and never returned.
export const LEAD_FILLER = "hey everyone, how is it going today";

// The Python package bounds its conversation-history padding by this budget in
// jevmod/core/context.py (`MAX_CONTEXT_TOKENS`, `CHARS_PER_TOKEN`). This package has no buffer of
// its own, but the same bound applies to whatever padding the caller passes in, for the same
// reason: ten items of four thousand characters is not ten items of sixty. Tokens are estimated
// at four characters each rather than tokenised, so the estimate errs low and the cap is applied
// to a number that is never larger than the truth.
export const MAX_CONTEXT_TOKENS = 600;
export const CHARS_PER_TOKEN = 4;

/**
 * Trim padding to fit a token budget, keeping the most recent items. Ported from the Python
 * package's `core.context.assemble` (jevmod/core/context.py); see that docstring for the full
 * reasoning, reproduced here because this package carries no conversation buffer to point at it.
 *
 * Newest-first is the direction that matters: if only three of ten fit, the three immediately
 * before the judged message are worth more than the three from the start of the list. The result
 * is returned oldest-first again, because that is the order a reader, and the model, expects.
 *
 * A single item longer than the whole budget is skipped, not truncated — `continue`, not `break`.
 * It was `break` in an earlier version of the Python module, and that dropped the *entire*
 * remaining window whenever the newest item happened to be oversized: a wall of pasted spam
 * arrives, the next message is judged with no padding at all, and the feature turns itself off
 * exactly when it would have helped most. Skipping the one that does not fit and carrying on is
 * the intended behaviour, carried across on purpose.
 */
export function assemble(items: readonly string[], maxTokens: number = MAX_CONTEXT_TOKENS): string[] {
  const out: string[] = [];
  let spent = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    const text = items[i] as string;
    const cost = Math.max(1, Math.floor(text.length / CHARS_PER_TOKEN));
    if (spent + cost > maxTokens) continue;
    out.push(text);
    spent += cost;
  }
  return out.reverse();
}

export class Verdict {
  constructor(
    public readonly messageId: string,
    /** category -> probability */
    public readonly scores: Scores,
    /** false when a pre-filter skipped Jev */
    public readonly judged: boolean,
    /** why it was skipped, or "cache" / "jev" */
    public readonly reason: string = "",
    /** server-defined rules -> probability */
    public readonly custom: Scores = {},
  ) {}

  top(): [string, number] | null {
    const all: Scores = { ...this.scores, ...this.custom };
    let best: [string, number] | null = null;
    for (const [k, p] of Object.entries(all)) {
      if (best === null || p > best[1]) best = [k, p];
    }
    return best;
  }
}

export interface JudgeOptions {
  /** A configured TypeSafe client. When omitted one is built from `apiKey` or `TYPESAFE_API_KEY`. */
  client?: TypeSafeClient;
  /** TypeSafe API key. Never logged. Falls back to the `TYPESAFE_API_KEY` environment variable. */
  apiKey?: string;
  /** How long a verdict for identical text stays reusable, in seconds. Default one day. */
  cacheTtlS?: number;
  /** Per-attempt request timeout in seconds. Default 20. */
  timeoutS?: number;
}

interface CacheEntry {
  at: number;
  scores: Scores;
  custom: Scores;
}

export class Judge {
  readonly client: TypeSafeClient;
  readonly cache = new Map<string, CacheEntry>();
  readonly cacheTtl: number;
  requests = 0;
  inputTokens = 0;
  judgedMessages = 0;

  constructor(options: JudgeOptions = {}) {
    this.client = options.client ?? makeClient(options);
    this.cacheTtl = options.cacheTtlS ?? 86_400;
  }

  /**
   * One Jev request for every message that passes the pre-filter and is not cached.
   *
   * `padding` is text that rides along in the request, oldest first, is asked the same
   * questions, and whose answers are discarded. It exists because the size of the batch changes
   * the answers: the same spam message reaches 17.3% recall judged alone and 38.7% in a batch of
   * ten (jevmod/benchmark/BATCH_EFFECT.md section 7). Without padding, a caller that judges one
   * message at a time gets worse moderation than one that batches, and nobody is told. This
   * package has no conversation buffer to fill `padding` automatically — the Python package does,
   * via `core.context.ConversationBuffer` — so the caller supplies it.
   *
   * The discard lives here on purpose, not in the caller: an invariant kept by whoever remembers
   * to keep it is one that eventually is not kept, so no padded text can reach a verdict or the
   * cache by any path through this method.
   */
  async judge(
    messages: readonly Message[],
    categories: readonly string[],
    customRules: Readonly<Record<string, string>> = {},
    padding: readonly string[] = [],
  ): Promise<Verdict[]> {
    const cats = categories.filter(isCategory);
    const out = new Map<string, Verdict>();
    const toJudge: Array<[Message, string]> = [];
    const now = Date.now() / 1000;
    for (const m of messages) {
      const why = prefilter(m);
      if (why) {
        out.set(m.id, new Verdict(m.id, {}, false, why));
        continue;
      }
      const text = normalize(m.text);
      const key = cacheKey(text, m.channelTopic ?? "", cats, customRules, padding);
      const hit = this.cache.get(key);
      if (hit && now - hit.at < this.cacheTtl) {
        out.set(m.id, new Verdict(m.id, { ...hit.scores }, true, "cache", { ...hit.custom }));
        continue;
      }
      toJudge.push([m, text]);
    }

    const ruleNames = Object.keys(customRules);
    if (toJudge.length > 0 && (cats.length > 0 || ruleNames.length > 0)) {
      // only the text and the channel topic reach Jev: no author names, no ids beyond the position.
      // Real messages start at m1; m0 is filled below and is never one of them.
      const stateMessages: Record<string, { text: string; channel_topic: string }> = {};
      toJudge.forEach(([m, text], i) => {
        stateMessages[`m${i + 1}`] = { text, channel_topic: m.channelTopic || "general chat" };
      });
      const topic = (toJudge[0] as [Message, string])[0].channelTopic || "general chat";

      // Padding, deduplicated preserving order, normalised so it cannot smuggle in text the
      // pre-filter would have cleaned, and with anything already being judged removed.
      const judgedTexts = new Set(toJudge.map(([, text]) => text));
      const seen = new Set<string>();
      const dedupedPad: string[] = [];
      for (const raw of padding) {
        const norm = normalize(raw);
        if (!norm || judgedTexts.has(norm) || seen.has(norm)) continue;
        seen.add(norm);
        dedupedPad.push(norm);
      }
      // Trimmed by the same budget the Python package's context is, and for the same reason:
      // `PAD_TO` bounds how many positions the padding takes and enforces nothing about their
      // length, so a handful of large pasted items could otherwise send tens of kilobytes of
      // padding and ask every category about each of them.
      const pad = assemble(dedupedPad, MAX_CONTEXT_TOKENS * PAD_TO);

      // m0 is never a real message, and that is the whole of this. Measured on 300 messages in
      // benchmark/position_zero.py (Python package): a message at m0 gains nothing from its
      // neighbours (+0.014 in a request of ten) while every other position gains about 0.22, and
      // the cost is asymmetric, spam positives losing 0.15 there while clean text moves 0.01.
      // Index zero costs recall and buys no precision. Real padding is preferred over the
      // constant because it is real text and costs nothing extra to have; the constant is the
      // fallback for a caller with no padding to offer.
      stateMessages["m0"] = { text: pad.length > 0 ? (pad.shift() as string) : LEAD_FILLER, channel_topic: topic };
      const trailingCount = Math.max(0, PAD_TO - toJudge.length - 1);
      pad.slice(0, trailingCount).forEach((text, k) => {
        stateMessages[`m${toJudge.length + 1 + k}`] = { text, channel_topic: topic };
      });

      const state = { messages: stateMessages, custom_rules: { ...customRules } };
      const questions: Record<string, NoulQuestion> = {};
      // Questions are generated for every position present, m0 included: a neighbour in the
      // state with no question pointed at it recovers only 23% of the batch-size gap
      // (benchmark/batch_context.py, Python package). That is why padding costs a real request's
      // worth of tokens rather than a few. Answers for m0 and any trailing padding are read below
      // and thrown away; only m1..mN map back to messages.
      const positions = Object.keys(stateMessages).length;
      for (let i = 0; i < positions; i++) {
        const path = `messages.m${i}`;
        for (const c of cats) {
          const cat = CATEGORIES[c];
          questions[`${c}_${i}`] = noul(cat.instructions.replaceAll("{m}", path), cat.criteria);
        }
        for (const name of ruleNames) {
          const rule = customRules[name] as string;
          questions[`custom__${name}_${i}`] = noul(
            `Does \`${path}.text\` break this community rule: \`custom_rules.${name}\` (${pyRepr(rule)})?`,
            {
              true: "the message does what the rule forbids, as a moderator who wrote it would read it",
              false:
                "the message is ordinary conversation, or the rule does not clearly cover it; " +
                "when the rule lists exceptions, those are allowed",
            },
          );
        }
      }
      const resp = await this.client.systemOne({ state, questions });
      this.requests += 1;
      this.inputTokens += resp.usage?.input_tokens ?? 0;
      this.judgedMessages += toJudge.length;
      // Offset by one: the real messages start at m1 because m0 holds the lead filler or padding.
      toJudge.forEach(([m, text], idx) => {
        const i = idx + 1;
        const scores: Scores = {};
        for (const c of cats) scores[c] = probability(resp.answers[`${c}_${i}`]);
        const custom: Scores = {};
        for (const name of ruleNames) custom[name] = probability(resp.answers[`custom__${name}_${i}`]);
        this.cache.set(cacheKey(text, m.channelTopic ?? "", cats, customRules, padding), { at: now, scores, custom });
        out.set(m.id, new Verdict(m.id, scores, true, "jev", custom));
      });
    } else if (toJudge.length > 0) {
      for (const [m] of toJudge) out.set(m.id, new Verdict(m.id, {}, false, "no categories enabled"));
    }
    return messages.map((m) => out.get(m.id) as Verdict);
  }
}

function makeClient(options: JudgeOptions): TypeSafeClient {
  const timeoutS = options.timeoutS ?? 20;
  return new TypeSafeClient({
    ...(options.apiKey !== undefined ? { apiKey: options.apiKey } : {}),
    timeout: timeoutS * 1000,
    retry: {
      maxRetries: 3,
      backoffInitialMs: 500,
      backoffMaxMs: 8000,
      httpStatuses: new Set([429, 500, 502, 503, 504, 529]),
    },
  });
}

function probability(answer: unknown): number {
  const a = answer as Partial<NoulResponse> | undefined;
  if (!a || a.type !== "noul" || typeof a.noul !== "number") {
    throw new TypeError(`expected a Noul answer, got ${describe(answer)}`);
  }
  return a.noul;
}

function describe(x: unknown): string {
  if (x === null) return "null";
  if (typeof x !== "object") return typeof x;
  const t = (x as { type?: unknown }).type;
  return typeof t === "string" ? `${t} answer` : "object";
}

/**
 * The cache key: the same five fields the Python `_key` hashes, for the same reasons.
 *
 * **The padding is part of it, and that costs hit rate on purpose.** Without it, a verdict
 * computed while one set of neighbours rode along in the request is handed back under another.
 * JEV-56 measured how much that matters: regrouping the same messages into different batches
 * moves 12% of spam positives across their threshold, against 2.7% for a request repeated
 * unchanged. A key that leaves the neighbours out asserts that two scorings of the same text are
 * interchangeable, and they are not. The hit a cache is actually for here — the same text posted
 * forty times in a minute — still shares a key, because those forty arrive with near-identical
 * padding. What stops sharing one is the same text a day later in a different conversation,
 * which is exactly the hit that was wrong.
 *
 * **A JSON array rather than a join on a separator**, which is also Python's fix and for a
 * failure that was demonstrated there rather than theorised: the old key joined the fields with a
 * character described as one no message text contains. `normalize` does not strip it, padding is
 * raw message text, and a message containing it collapsed two different requests onto one key.
 * JSON quotes and escapes, so no field can impersonate a delimiter. This package had the same
 * shape of bug latent in a `|` join, where a channel topic containing a pipe was enough.
 *
 * **Categories are sorted**, because the same two categories in either order are the same
 * question and used to be two keys.
 *
 * **Byte-identical to the Python key**, which `tests/normalize.test.ts` checks by running the
 * Python function when the monorepo is present, the way it already checks `categories.json`. It
 * did not used to be: the assertion was a hash somebody computed once and pasted in, so when the
 * Python side moved from a joined string to a JSON payload the golden value went on passing
 * against a function that no longer existed. A claim of sameness that cannot notice a change is
 * not a check.
 */
export function cacheKey(
  text: string,
  topic: string,
  cats: readonly CategoryName[],
  rules: Readonly<Record<string, string>>,
  padding: readonly string[] = [],
): string {
  const sortedCats = [...cats].sort();
  const sortedRules = Object.entries(rules).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const payload = pyJson([text.toLowerCase(), topic, sortedCats, sortedRules, [...padding]]);
  return createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 32);
}

type Nested = string | Nested[];

/**
 * `json.dumps(value, ensure_ascii=False)` for a structure of strings and lists, which is all the
 * cache key is made of.
 *
 * `JSON.stringify` is the same thing without the space after each comma, and that one space is
 * the whole reason this exists: without it the two packages hash different bytes for the same
 * inputs and `cacheKey`'s claim of sameness cannot be tested. Strings go through
 * `JSON.stringify`, so the escaping is not hand-rolled — it already matches Python's for
 * `ensure_ascii=False`, including the `\n`-style shortcuts and the `\uXXXX` form for other
 * control characters. There are no objects here, so Python's `sort_keys` has nothing to do.
 */
function pyJson(value: Nested): string {
  return typeof value === "string" ? JSON.stringify(value) : `[${value.map(pyJson).join(", ")}]`;
}

/** Python `repr()` of a str: same quoting and escapes, so questions and cache keys match the Python package. */
export function pyRepr(s: string): string {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    const cp = ch.codePointAt(0) as number;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += `\\${quote}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (cp < 0x20 || cp === 0x7f) out += `\\x${cp.toString(16).padStart(2, "0")}`;
    else out += ch;
  }
  return out + quote;
}
