"""bot_binding_auth plugin — source of truth for platform allowlists.

Replaces the env-var-driven per-platform allowlist (``DISCORD_ALLOWED_USERS``,
``TELEGRAM_ALLOWED_USERS``, etc.) with a single SSoT: the ``bot-binding.db``
SQLite database. The plugin runs as a `` ``pre_gateway_dispatch`` `` hook and
returns ``{"action": "authorized"}`` when the inbound user is in the bindings
table; the gateway then skips the env-var ``_is_user_authorized`` check.

Why a plugin (vs in-place patch in core)?

- Plugin API is a stable contract; env-var path is stable too but this
  plugin can be disabled without touching hermes-agent source.
- Other ACL sources (rate-limit, payment tier, SSO) can plug into the same
  hook later without further core changes.
- Public release: anyone running hermes + bot-binding.db gets this for free
  just by installing the plugin (no source patch).

Configuration:

- BOT_BINDING_DB_PATH  (default ``<runtime_dir>bot-binding.db``)
  Override via env var if you keep the DB at a different path.
- HERMES_BOT_BINDING_DB  same as above, alternate env var.

The plugin is a no-op if the DB file does not exist (e.g. first-run before
``bot-binding.db`` is initialized); in that case the env-var allowlist path
takes over and admins see no behavior change.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["register", "PLUGIN_NAME", "PLUGIN_VERSION"]


PLUGIN_NAME = "bot_binding_auth"
PLUGIN_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path(r"<runtime_dir>bot-binding.db")


def _resolve_db_path() -> Optional[Path]:
    """Resolve bot-binding.db path from env or default. None if not configured."""
    env_path = os.environ.get("HERMES_BOT_BINDING_DB") or os.environ.get(
        "BOT_BINDING_DB_PATH"
    )
    if env_path:
        return Path(env_path)
    if _DEFAULT_DB_PATH.exists():
        return _DEFAULT_DB_PATH
    return None


def _platform_id_for(platform: str, chat_id: Optional[str]) -> Optional[str]:
    """Map hermes Platform enum value + chat_id to a bot-binding.db platform_id.

    bot-binding.db's ``platforms`` table has rows like
    ``weixin:11d658c3e7f7@im.bot`` or ``discord:discord_main`` — only one per
    platform kind, so for Discord/Telegram the lookup is just the platform
    + a stable bot identifier. WeChat is special: there can be multiple
    ``weixin:<id>@im.bot`` rows; the chat_id alone is not enough. But for the
    common case of one weixin bot per installation, the chat_id (an
    ``@im.wechat`` style id) maps to that single bot.

    Returns the platform_id to query, or None if the platform isn't tracked
    in bot-binding.db.
    """
    if not platform or not chat_id:
        return None
    if platform in {"discord", "discord_main", "discord_main_bot"}:
        return "discord:discord_main"
    if platform in {"telegram", "telegram_main", "telegram_bot"}:
        return "telegram:hermes_bot"
    if platform in {"weixin", "wechat"}:
        # WeChat bots are per-account. The chat_id is the user's @im.wechat
        # id; the platform_id is whichever weixin:<id>@im.bot row(s) bind
        # this user. Since each user binds to exactly one weixin bot in our
        # setup, we look up via chat_id (not platform_id). The helper below
        # does that for weixin.
        return None  # signal "use chat_id-only path"
    return None


def _lookup_user(platform: str, chat_id: Optional[str], user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a user in bot-binding.db.

    Returns a dict with at least ``user_id`` and ``role`` keys, or None.
    We accept both the chat_id (binding row) and user_id (sender id) so the
    platform adapter doesn't have to know which one we trust.
    """
    db_path = _resolve_db_path()
    if not db_path:
        return None

    pid = _platform_id_for(platform, chat_id)
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            if pid:
                # Standard path: bindings.platform_id = pid AND chat_id matches
                cur.execute(
                    """
                    SELECT b.user_id, b.role_inherit AS role, b.active,
                           m.subscription_plan, m.short_alias, m.real_name
                    FROM bindings b
                    LEFT JOIN user_meta m ON b.user_id = m.user_id
                    WHERE b.platform_id = ?
                      AND b.chat_id = ?
                      AND b.active = 1
                    LIMIT 1
                    """,
                    (pid, chat_id),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)

            # Fallback 1: chat_id-only lookup for weixin-style platforms
            # where each user binds to exactly one weixin bot row
            if chat_id and (platform in {"weixin", "wechat"} or pid is None):
                cur.execute(
                    """
                    SELECT b.user_id, b.role_inherit AS role, b.active,
                           m.subscription_plan, m.short_alias, m.real_name
                    FROM bindings b
                    LEFT JOIN user_meta m ON b.user_id = m.user_id
                    WHERE b.platform_id LIKE 'weixin:%'
                      AND b.chat_id = ?
                      AND b.active = 1
                    LIMIT 1
                    """,
                    (chat_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)

            # Fallback 2: any binding for this user_id (catches weixin DMs
            # where sender is known by user_id, not by chat_id)
            if user_id:
                cur.execute(
                    """
                    SELECT b.user_id, b.role_inherit AS role, b.active,
                           m.subscription_plan, m.short_alias, m.real_name
                    FROM bindings b
                    LEFT JOIN user_meta m ON b.user_id = m.user_id
                    WHERE b.user_id = ?
                      AND b.active = 1
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)

            return None
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("bot_binding_auth: sqlite error looking up user: %s", e)
        return None
    except Exception as e:
        logger.warning("bot_binding_auth: unexpected error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Hook callback
# ---------------------------------------------------------------------------

def _on_pre_gateway_dispatch(event: Any, gateway: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """``pre_gateway_dispatch`` hook callback.

    Inspects ``event.source`` (a ``SessionSource``) and checks bot-binding.db.
    Returns ``{"action": "authorized", "reason": "..."}`` if the user has an
    active binding; otherwise returns None so the gateway falls through to the
    env-var allowlist path.
    """
    source = getattr(event, "source", None)
    if source is None:
        return None
    platform = source.platform.value if source.platform else ""
    chat_id = source.chat_id or None
    user_id = source.user_id or None

    if not platform or not chat_id:
        return None  # leave the gateway's chat-id-less handling to the core

    user = _lookup_user(platform, chat_id, user_id)
    if user is None:
        return None  # not in bot-binding.db — let the env-var path decide

    if not user.get("active"):
        # binding is in the DB but marked inactive — fall through to deny path
        return None

    return {
        "action": "authorized",
        "reason": (
            f"bot-binding.db match: user_id={user['user_id']} "
            f"alias={user.get('short_alias') or '?'} "
            f"role={user.get('role') or 'user'} "
            f"plan={user.get('subscription_plan') or '?'}"
        ),
        # extra data for observability (not used by gateway)
        "_db_user_id": user["user_id"],
        "_db_role": user.get("role"),
    }


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Plugin entrypoint — register the ``pre_gateway_dispatch`` hook."""
    # Validate the DB is reachable; log if not so admin knows the plugin is
    # loaded but currently a no-op.
    db_path = _resolve_db_path()
    if db_path is None:
        logger.warning(
            "bot_binding_auth: no bot-binding.db found at default path %s; "
            "plugin will be a no-op until the DB exists",
            _DEFAULT_DB_PATH,
        )
    else:
        logger.info("bot_binding_auth: using DB at %s", db_path)

    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.info(
        "bot_binding_auth v%s registered pre_gateway_dispatch hook",
        PLUGIN_VERSION,
    )