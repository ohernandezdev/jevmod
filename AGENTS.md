# AGENTS.md: how an AI agent works with jevmod

Read this whole file. Then you need no other document unless it points you to one.

## What it is

jevmod is an open-source moderation layer: a batch of messages goes to Jev (TypeSafe's System One
model) in one request and each message comes back with a probability per category (`spam`, `scam`,
`harassment`, `nsfw`, `offtopic`, `selfharm`, `doxxing`, `minors`) and per plain-language rule.
A `Policy` turns probabilities into an action (`none`, `flag`, `delete`, `timeout`); flag-only by
default, fail-open when Jev is unreachable. The same core serves a Python SDK, a CLI, an HTTP API,
an MCP server and the Discord, Telegram, Reddit, Twitch and YouTube bots.

## APIs, exact signatures

Python (`pip install jevmod`, Python 3.10+):

```python
from jevmod import Moderator, Policy, Decision

Moderator(policy: Policy | None = None, judge: Judge | None = None)
Moderator.check(text: str, *, author: str = "", channel_topic: str = "", author_trusted: bool = False) -> Decision
Moderator.check_many(texts: Sequence[str], *, author: str = "", channel_topic: str = "",
                     author_trusted: bool = False, ids: Sequence[str] | None = None) -> list[Decision]

Policy()                                   # DEFAULT_THRESHOLDS / DEFAULT_ACTIONS below
Policy.set_category(category: str, action: str, threshold: float | None = None) -> None   # action in ("off","flag","delete","timeout")
Policy.set_rule(name: str, text: str | None, action: str = "flag", threshold: float | None = None) -> None  # text=None removes; max 5 rules, 200 chars
Policy.nudge(category: str, delta: float = 0.03) -> float   # "rule:<name>" for rules; clamps to 0.50..0.99
Policy.enabled_categories() -> list[str]
Policy.to_dict() / Policy.from_dict(d)

Decision: message_id: str, action: str, category: str | None, probability: float,
          scores: dict[str, float], judged: bool, reason: str, policy_version: int
Decision.to_dict() -> dict   # probabilities rounded to 4 places
```

Defaults (`jevmod/core/policy.py`): spam 0.85, scam 0.75, harassment 0.75, nsfw 0.80, offtopic
0.90 (off), selfharm 0.80 (flag only), doxxing 0.80, minors 0.70, rules 0.80. `decide()` picks the
most severe action whose threshold is crossed, ties to the higher probability.

Lower level: `Judge(client=None, cache_ttl_s=86400, timeout_s=20.0).judge(messages: list[Message],
categories: list[str], custom_rules: dict[str, str] | None = None) -> list[Verdict]`;
`Message(id, text, author="", channel_topic="", author_trusted=False, channel="", context=())`;
`Verdict(message_id, scores, judged, reason, custom)`. `context` is what was said in this channel
just before, oldest first, filled by `ModerationService` from `core/context.py` so every adapter
gets it unchanged; `channel` only matters on Discord, where one tenant has many conversations.
Both are local, like `author`: neither reaches Jev. Errors surface as `typesafe_sdk.TypeSafeError` after 3 retries
(429/5xx, backoff, Retry-After) and a 20 s timeout. `Moderator` does not catch them.

CLI: `jevmod check [text | -] [--topic T] [--rule R]... [--threshold X] [--json]`; exit 0 clean,
1 something triggered, 2 error. `jevmod init` stores the key.
`jevmod api|discord|telegram|reddit|twitch|youtube|mcp` runs that role. The list of roles lives in one place, `ROLES` in `jevmod/__main__.py`; `cli.py` reads it rather
than typing it again, because the two lists drifted in both directions once. A role whose credentials are
missing exits naming every variable still unset, and does so before the adapter is imported, because each
adapter opens its `Store` at import time and would otherwise leave a `jevmod.sqlite` behind.

HTTP (`jevmod api`, FastAPI, port 8080): `POST /v1/moderate` (`{"messages":[{"id","text",
"author","channel_topic","author_trusted"}]}`, max 50, bearer tenant key) returns `{"request_id",
"decisions":[Decision without policy_version],"usage"}`; `GET/PUT /v1/policy`; `GET
/v1/decisions?limit=50`; `DELETE /v1/tenant`; `POST /v1/keys` (admin token
`JEVMOD_ADMIN_TOKEN`); `GET /v1/health`; `GET /metrics`. OpenAPI at `/docs`. Jev down:
`action="none", judged=false, reason="error_open"`. Quota exceeded: `reason="quota"`. A server that is not paying is `inactive` and comes back
`reason="inactive"`: it is not judged and its local rules do not run either. The Free plan was removed on
2026-09-21; `JEVMOD_ENFORCE_PLANS` (default off) is what makes plans mean anything, and a self-hosted copy
leaves it off and judges everything.

MCP (`jevmod mcp`, stdio, official MCP Python SDK, extra `pip install "jevmod[mcp]"`): tools `moderate(texts: list[str], channel_topic: str = "",
rules: dict[str, str] | None = None)` (up to 50 texts, actions `none`/`flag` only, nothing stored)
and `categories()`. Register it in Claude Code with the plugin
in `plugin/` or `claude mcp add jevmod -- jevmod mcp`; in Cursor or Codex, add
`{"command": "jevmod", "args": ["mcp"], "env": {"TYPESAFE_API_KEY": "..."}}` to their MCP config.

npm (`packages/jevmod-js/`, TypeScript): same questions (`jevmod/categories.json`), same policy,
`check`, `checkMany`, and an HTTP client for a deployed API. Check the package's own README for
the exact exports; it is developed in parallel with this file.

## Where things live

| path | what |
|---|---|
| `jevmod/judge.py` | the judgment core: normalisation, pre-filters, cache, one Jev request per batch; docstring lists the red-team findings that shaped it |
| `jevmod/categories.json` | the questions and criteria every implementation asks Jev; the only place they are defined |
| `jevmod/core/policy.py` | `Policy`, `Decision`, `decide`, defaults |
| `jevmod/core/service.py` | `ModerationService` (tenant policy, quota, audit log, fail-open) and `Batcher` (2 s window) |
| `jevmod/core/context.py` | the conversation window: a bounded rolling buffer per channel and the assembler that trims it to a token budget |
| `jevmod/core/store.py` | SQLite store: tenants, hashed API keys, usage, decisions (30-day retention) |
| `jevmod/keys.py` | key lookup: `TYPESAFE_API_KEY`, then the OS keyring (extra `keyring`, in `[all]`), then `.env`; `jevmod init`, `jevmod init --forget` |
| `jevmod/cli.py`, `jevmod/__main__.py` | `jevmod check` and the role runner |
| `jevmod/api/server.py` | the HTTP API |
| `jevmod/mcp_server.py` | the MCP server |
| `jevmod/adapters/` | Discord, Telegram, Reddit, Twitch and YouTube bots over the same core |
| `packages/jevmod-js/` | npm package |
| `plugin/` | Claude Code plugin: skills `jevmod-integrate`, `jevmod-moderate`, `.mcp.json` |
| `tests/` | `test_offline.py` (no key); `test_judge.py`, `test_cli.py`, `test_api.py`, `test_redteam.py`, `test_mcp.py`, `test_keys.py`, `test_examples.py` (real Jev; `test_examples.py` needs the `examples` extra) |
| `tests/data/redteam.csv` | 98 labelled adversarial messages; the regression floor |
| `benchmark/` | comparison against Llama Guard 3, ShieldGemma, toxic-bert |
| `api/` | the OpenAPI schema and the Postman collection for the open API |
| _(private)_ | the site, the demo, Stripe billing and the admin panel live in the jevmod-hosted repo |

## Running tests

```
python -m venv .venv && .venv/Scripts/pip install -e ".[all,dev,examples]"    # Linux/macOS: .venv/bin/pip
ruff check . && mypy jevmod
pytest tests -q            # offline tests always run; the rest skip without TYPESAFE_API_KEY
```

Set `TYPESAFE_API_KEY` in the environment (or run `jevmod init`) to run the real-API tests. The
red-team suite calls Jev about a hundred times; expect a minute and a few cents.

## Coding rules

- `ruff check .` and `mypy jevmod` clean before any commit. Line length 120, Python 3.10 syntax.
- Real tests against Jev, no mocks of the TypeSafe client. Offline tests are for pure code
  (`Policy`, `Store`, pre-filters). Assert against thresholds with margin, never exact values.
  **How much margin depends on whether the request repeats exactly**, measured in
  `benchmark/BATCH_EFFECT.md` on 2026-09-22 and not the plus or minus 0.03 this file used to
  claim for every case:
  - The same request sent again moves the mean score by 0.004 to 0.011, but the tail reaches
    0.11, and 9% to 12% of the messages that score in the middle move further than 0.03.
  - A test that batches differently, which is what almost every test does, is a different
    request. There the p95 is 0.15 to 0.20 and the maximum 0.55.
  - So: assert a category and a direction, or a margin of 0.2, not 0.03. A test that batches
    its own fixtures alongside anything else is asserting on a number that moves by a fifth.
- The Jev state is a dict keyed by position (`messages.m3.text`), never a list. Lists leaked
  probabilities between neighbours in multilingual batches. That fixed the leak and did not fix
  the coupling: regrouping the same messages into different batches of 25 still moves 12% of
  spam positives across their threshold, against 2.7% for a repeated request. Position alone is
  not the cause; membership is. `benchmark/BATCH_EFFECT.md` has the measurement, and JEV-56 is
  where it goes next.
- Every question carries `criteria` with `true` and `false`. Change questions only in
  `categories.json`, and re-run `tests/test_redteam.py`.
- `selfharm` stays flag-only. `offtopic` stays off by default.
- **The context window is ten messages and that number is measured.** `benchmark/BATCH_EFFECT.md`
  section 7: spam recall at the shipped threshold is 17.3% at one message, 32.0% at five, 38.7% at
  ten, 37.3% at twenty-five. It saturates at ten and twenty-five costs 2.5 times more per judged
  message. Moving `WINDOW` means re-running `benchmark/batch_effect.py`, not arguing about it.
- The conversation window is in memory and never persisted. Holding everybody's recent messages on
  disk is a retention question the privacy notice does not answer; JEV-20 to JEV-22 own it. Erasure
  still has to reach the window: `ModerationService.forget_context` exists because deleting rows
  does not.
- Only message text and `channel_topic` go to TypeSafe. No author names, ids or emails. This is enforced
  at `jevmod/judge.py:141`, promised in the privacy notice and stated on the home page. A decision exists
  to change it, gated on a rewritten privacy notice and a DPA: **do not change the code first.**
- Never write a key into a file. `.env` is git-ignored. Nothing prints or logs the key.
- Every external call has a timeout and a defined failure behaviour. Fail open, log once per batch.
- Plain English in code and docs. No marketing adjectives, no emoji. Numbers only when measured.
- Do not commit from an agent session unless asked; the coordinator commits.

## Integration recipe, short form

1. `pip install jevmod`; `jevmod init` (or set `TYPESAFE_API_KEY`). Confirm `.env` is git-ignored.
2. Find the one place where user text enters the system. Insert:

   ```python
   from jevmod import Moderator

   mod = Moderator()  # module-level: keeps the 24 h cache
   d = mod.check(text, channel_topic=topic)
   if d.action != "none":
       handle(d)  # d.category, d.probability, d.scores
   ```

   Batches: `mod.check_many(texts, ...)`, 50 or fewer per call. Not Python: `POST /v1/moderate`.
3. Wrap the call in `try/except typesafe_sdk.TypeSafeError` and choose fail-open or fail-closed on
   purpose.
4. Add a test that skips without the key and uses the samples in `tests/test_judge.py` (scam,
   clean, harassment, selfharm, clean-with-minor-mentioned). Assert `d.category` and
   `d.probability >= 0.7`, or `d.action == "none"`.
5. Run `jevmod check "FREE NITRO for the first 100!! claim at discord-gifts.ru/nitro"` (exit 1) and
   `jevmod check "gg everyone, same time tomorrow?"` (exit 0) to prove the key and the network.

The long form, with the decision table and the gotchas, is `plugin/skills/jevmod-integrate/SKILL.md`.
