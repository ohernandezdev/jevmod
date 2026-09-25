# jevmod (npm)

Moderation for user text, powered by [Jev](https://typesafe.ai) (TypeSafe's System One model). Every message
gets a probability for spam, scam, harassment, adult content, off-topic, self-harm, doxxing and sexual content
involving minors, plus for rules you write in plain language. You own the thresholds and the actions; nothing
is deleted until you turn that on.

This is the TypeScript port of the Python package in this repository. It asks Jev the same questions
(`categories.json` is shared byte for byte), applies the same `m0` discipline described below, applies the
same policy and returns the same decision shape. The one difference: the Python package fills the `m0`/padding
positions automatically from a per-channel conversation buffer (`jevmod/core/context.py`); this package has no
buffer, so the caller passes `padding` explicitly.

## Install

```sh
npm install jevmod
```

Node 20 or newer (the TypeSafe SDK requires it). ESM and CommonJS builds with type declarations are included.
The only runtime dependency is `@typesafe-ai/sdk`.

## Usage

```ts
import { Moderator, Policy } from "jevmod";

const mod = new Moderator(); // TYPESAFE_API_KEY from the environment
const d = await mod.check("FREE NITRO for the first 100!! claim at discord-gifts.ru/nitro", { channelTopic: "gaming" });
d.action; d.category; d.probability; // "flag", "scam", 0.97
d.scores; // { spam: 0.95, scam: 0.97, harassment: 0.03, ... }

const policy = new Policy();
policy.setCategory("scam", "delete", 0.7);
policy.setRule("no_politics", "No political discussion. Game news is fine.", "flag", 0.8);
const decisions = await new Moderator({ policy }).checkMany(["...", "...", "..."], { channelTopic: "support" });
```

`checkMany` is the cheap path: every message that passes the pre-filter goes to Jev in one request. Messages
under eight letters without a link, trusted authors (`authorTrusted: true`) and repeats of already-judged text
are never sent. Only the message text and the channel topic reach TypeSafe; author names and ids do not.

A decision looks like this, the same as the Python `Decision.to_dict()` and the HTTP API:

```json
{"message_id": "0", "action": "flag", "category": "scam", "probability": 0.97,
 "scores": {"spam": 0.95, "scam": 0.97, "harassment": 0.03, "nsfw": 0.01, "selfharm": 0.0, "doxxing": 0.0, "minors": 0.0},
 "judged": true, "reason": "jev", "policy_version": 1}
```

`action` is `none`, `flag`, `delete` or `timeout`. `category` is the winning category or `rule:<name>`.
`reason` is `jev`, `cache`, or why the pre-filter skipped it (`too short`, `empty`, `trusted author`).

### Policy

Defaults: every category flags except `offtopic`, which is off (it needs a `channelTopic` to be useful).
Thresholds: spam 0.85, scam 0.75, harassment 0.75, nsfw 0.8, offtopic 0.9, selfharm 0.5, doxxing 0.8, minors 0.7.
Custom rules default to `flag` at 0.8; at most five per policy. `policy.nudge("spam")` raises a threshold by 0.03
(false-positive feedback). `JSON.stringify(policy)` and `Policy.fromJSON(obj)` use the same shape as the Python
package and `GET/PUT /v1/policy`, so a policy can move between the three.

### Lower level

`Judge` does the batching, caching and the single Jev request:
`judge(messages, categories, customRules, padding)` returns `Verdict`s with raw probabilities; counters
`requests`, `inputTokens`, `judgedMessages`. `decide(policy, verdict)` turns a verdict into a decision.
`normalize` and `prefilter` are exported for tests and tooling.

`padding` is optional text, oldest first, that rides along in the request and is asked the same questions, but
whose answers are discarded — it exists because the batch's size and composition change the answers Jev gives
for the messages you actually care about (measured in the Python package's `benchmark/BATCH_EFFECT.md`, section
7: the same spam message reaches 17.3% recall judged alone and 38.7% batched with nine others). The request
never puts a real message at `messages.m0`: that position always holds either the oldest padding item or, with
no padding at all, a constant filler (`hey everyone, how is it going today`). `benchmark/position_zero.py` in
the Python package found that position gains nothing from its neighbours while every other position gains
about 0.22, asymmetrically, so a message judged alone is always at the one position that costs it recall. This
package never fills `padding` on its own — there is no conversation buffer here — so a caller that wants the
same batch-size benefit the Python package's bots get for free needs to keep its own short rolling window of
recent channel text and pass it in. `check` and `checkMany` take it as an option, so you do not have to drop
down to `Judge` to use it.

The padding is part of the cache key, which costs hit rate deliberately. The same text scored beside different
neighbours is not the same score: regrouping the same messages into different batches moves 12% of spam
positives across their threshold, against 2.7% for a request repeated unchanged. The hit a cache is actually
for here — the same text posted forty times in a minute — still shares a key, because those forty arrive with
near-identical padding.

## API key

The TypeSafe key comes from the `TYPESAFE_API_KEY` environment variable or from the constructor:

```ts
new Moderator({ apiKey: process.env.MY_SECRET_STORE_KEY });
```

It is passed straight to the TypeSafe SDK and is never logged, never included in error messages, and never
written to disk by this package. Keep it out of git: put it in your process environment or a `.env` file that
is ignored. The SDK's `debug` log level prints request bodies (message text), not credentials; the default
level is `warn`.

## HTTP client for a deployed jevmod API

If you run the Python HTTP API (`jevmod api` or the Docker image) so that only the server holds the TypeSafe
key, callers use a tenant key minted with `POST /v1/keys` and this client:

```ts
import { JevmodClient } from "jevmod";

const api = new JevmodClient({ baseUrl: "https://mod.example.com", apiKey: process.env.JEVMOD_API_KEY });
const res = await api.moderate(["hello there", { id: "b", text: "DM me to double your ETH", channel_topic: "gaming" }]);
res.decisions; // same decision objects, without policy_version
await api.getPolicy();
await api.putPolicy({ actions: { scam: "delete" }, thresholds: { scam: 0.7 }, rules: { no_politics: "No politics." } });
await api.decisions(50); // the tenant's recent decisions
```

`baseUrl` and `apiKey` fall back to `JEVMOD_API_URL` and `JEVMOD_API_KEY`. Failures throw `JevmodApiError` with
the HTTP status and the response body; the key is not part of the message. Uses the global `fetch`.

## Examples

- `examples/node.mjs`: check, checkMany, a custom policy, the HTTP client.
- `examples/express.mjs`: an Express middleware that moderates a body field, rejects `delete`/`timeout`
  decisions with 422, lets staff through, and fails open when Jev is unreachable.

Build first (`npm run build`), then `TYPESAFE_API_KEY=... node examples/node.mjs`. Express is a dev dependency
of this package only for the example.

## Shared questions

The questions Jev is asked live in `categories.json` at the root of the Python package (`jevmod/categories.json`).
This package ships a verbatim copy in `src/categories.json`; `npm run sync-categories` refreshes it and a test
fails if the two files differ. Do not edit the wording here: every implementation (Python, npm, MCP) must ask
Jev exactly the same thing so their numbers are comparable.

Two details of the port worth knowing: text is normalised with the same rules as Python (HTML entities, NFKC,
combining marks removed using a table generated from Python's `unicodedata`, format characters removed), and the
cache key is the same SHA-256 prefix, so verdicts and keys line up across the two packages. `htmlUnescape`
covers numeric entities and the common named ones, not the full HTML5 table.

## Development

```sh
npm install
npm run typecheck   # tsc --strict
npm run build       # tsup: dist/index.js (ESM), dist/index.cjs, .d.ts
npm test            # vitest; live tests run only when TYPESAFE_API_KEY is set
```

Live tests call the real Jev API on the same sample messages as the Python test suite. They are skipped, not
mocked, without a key.
