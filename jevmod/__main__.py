"""`python -m jevmod` / `jevmod`: `check` judges text from the terminal, `init` stores the key, `mcp` serves the
MCP tools over stdio; `api`, `discord`, `telegram`, `reddit`, `twitch`, `youtube` start that role (default role
from JEVMOD_ROLE, then `api`)."""

from __future__ import annotations

import os
import sys

# Every variable the role's `run()` reads with `os.environ[...]`, so a missing one is a sentence instead of a
# bare KeyError. The check has to happen here, before the adapter module is imported: each of them opens its
# `Store` at import time, so an adapter that is about to fail on a missing variable would already have left a
# `jevmod.sqlite` behind in whatever directory the operator happened to be standing in.
REQUIRED_ENV = {
    "discord": ("DISCORD_TOKEN",),
    "telegram": ("TELEGRAM_TOKEN",),
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD",
               "REDDIT_SUBREDDITS"),
    "twitch": ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_REFRESH_TOKEN", "TWITCH_BOT_LOGIN",
               "TWITCH_CHANNELS"),
    "youtube": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CHANNEL_ID",
                "YOUTUBE_VIDEO_ID"),
}

# Where an operator goes to get them. Each one is the first step, not the whole procedure; the adapter's own
# docstring carries the scopes, the standing the bot account needs on the platform, and the rest.
WHERE = {
    "discord": "Developer Portal -> Bot -> Reset Token",
    "telegram": "@BotFather -> /newbot",
    "reddit": "https://www.reddit.com/prefs/apps (script app)",
    "twitch": "https://dev.twitch.tv/console/apps, then a user token for the bot account with chat:read, "
              "moderator:manage:banned_users and moderator:manage:chat_messages; see twitch_bot.py",
    "youtube": "https://console.cloud.google.com/apis/credentials, then a user token with the "
               "youtube.force-ssl scope; see youtube_bot.py",
}

ROLES = ("api", "discord", "telegram", "reddit", "twitch", "youtube", "mcp")


def run_role(role: str) -> None:
    role = role.lower()
    missing = [v for v in REQUIRED_ENV.get(role, ()) if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} ({WHERE[role]})")
    if role == "api":
        import uvicorn

        uvicorn.run(
            "jevmod.api.server:app",
            host=os.environ.get("JEVMOD_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8080")),
            log_level="info",
        )
    elif role == "discord":
        from .adapters.discord_bot import main as run

        run()
    elif role == "telegram":
        from .adapters.telegram_bot import main as run

        run()
    elif role == "reddit":
        from .adapters.reddit_bot import run

        run()
    elif role == "twitch":
        from .adapters.twitch_bot import main as run

        run()
    elif role == "youtube":
        from .adapters.youtube_bot import main as run

        run()
    elif role == "mcp":
        from .mcp_server import main as run

        run()
    else:
        raise SystemExit(f"unknown role {role!r}; use check | init | {' | '.join(ROLES)}")


def main() -> None:
    if len(sys.argv) > 1:
        from .cli import main as cli

        sys.exit(cli(sys.argv[1:]))
    run_role(os.environ.get("JEVMOD_ROLE", "api"))


if __name__ == "__main__":
    main()
