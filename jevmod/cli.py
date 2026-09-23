"""Command line for developers and scripts: judge text from the terminal, no server, no database.

    jevmod check "FREE NITRO claim at discord-gifts.ru"          # one message
    jevmod check -  < messages.txt                                # one message per line, one Jev request per 50
    jevmod check --topic "support" --rule "No politics" -- "..."  # context and a plain-language rule
    jevmod check --json ...                                       # machine-readable, one object per line
    jevmod init                                                   # store the TypeSafe key (keyring, or .env)
    jevmod mcp                                                    # MCP server over stdio for coding agents

Exit code 0 when no message triggers an action, 1 when at least one does, 2 on an error. Pipe-friendly.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable

from . import Moderator, Policy
from .core.policy import EXPERIMENTAL
from .judge import CATEGORIES


def _read_texts(args: argparse.Namespace) -> list[str]:
    if args.text == ["-"] or not args.text:
        return [line.rstrip("\n") for line in sys.stdin if line.strip()]
    return [" ".join(args.text)]


def _chunks(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def check(args: argparse.Namespace) -> int:
    policy = Policy()
    for c in CATEGORIES:
        if c in EXPERIMENTAL and c not in (args.category or []):  # opt in, like the bot
            continue
        policy.set_category(c, "off" if c == "offtopic" and not args.topic else "flag", args.threshold)
    for i, rule in enumerate(args.rule or []):
        policy.set_rule(f"rule{i + 1}", rule)
    mod = Moderator(policy=policy)
    texts = _read_texts(args)
    if not texts:
        print("nothing to check", file=sys.stderr)
        return 2
    hit = False
    for batch in _chunks(texts, 50):
        for text, d in zip(batch, mod.check_many(batch, channel_topic=args.topic), strict=True):
            hit = hit or d.action != "none"
            if args.json:
                print(json.dumps({"text": text, **d.to_dict()}, ensure_ascii=False))
            else:
                top = ", ".join(f"{k} {v:.2f}" for k, v in sorted(d.scores.items(), key=lambda kv: -kv[1])[:3])
                verdict = f"{d.category} {d.probability:.2f}" if d.action != "none" else "ok"
                if not d.judged:
                    verdict = f"skipped ({d.reason})"
                print(f"{verdict:<28} {text[:70]!r}  [{top}]")
    return 1 if hit else 0


def init(args: argparse.Namespace) -> int:
    from .keys import forget
    from .keys import init as store_key

    if args.forget:
        return forget()
    return store_key(prefer_env_file=args.env_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jevmod", description="Moderation decisions powered by Jev.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="judge one message (argument) or many (stdin, one per line with '-')")
    p.add_argument("text", nargs="*", help="the message; '-' or nothing reads stdin")
    p.add_argument("--topic", default="", help="what the channel is about (enables the offtopic check)")
    p.add_argument("--rule", action="append", help="a rule in plain language; repeatable, up to 5")
    p.add_argument("--threshold", type=float, default=None, help="one threshold for every category (0.5-0.99)")
    p.add_argument(
        "--category",
        action="append",
        choices=sorted(EXPERIMENTAL),
        help="also ask an experimental category, off by default; repeatable",
    )
    p.add_argument("--json", action="store_true", help="one JSON object per line")
    p.set_defaults(func=check)
    p = sub.add_parser("init", help="ask for the TypeSafe key (hidden input), verify it, store it in the OS keyring")
    p.add_argument("--env-file", action="store_true", help="write .env in the current directory instead of the keyring")
    p.add_argument("--forget", action="store_true", help="remove the key from the OS keyring")
    p.set_defaults(func=init)
    sub.add_parser("mcp", help="MCP server over stdio (tools: moderate, categories)")
    # Read from the dispatcher rather than typed again here. The two lists were written separately and drifted:
    # `demo` and `hosted` are commercial roles that left the open package, and `jevmod demo` still parsed and
    # then died on "unknown role"; `twitch` and `youtube` shipped as adapters and never got a command at all.
    from .__main__ import ROLES

    for role in ROLES:
        if role != "mcp":
            sub.add_parser(role, help=f"run the {role} role (same as JEVMOD_ROLE={role})")
    args = parser.parse_args(argv)
    if args.cmd in ("check", "init"):
        try:
            return int(args.func(args))
        except Exception as exc:  # network, key, quota: say it in one line, exit 2
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    from .__main__ import run_role

    run_role(args.cmd)
    return 0
