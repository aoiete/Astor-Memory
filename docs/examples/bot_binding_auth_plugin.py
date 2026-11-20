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

- BOT_BINDING_DB_PATH  (default: ``$ASTOR_DIR/bot-binding.db`` or ``~/.astor/bot-binding.db``)
  Override via env var if you keep the DB at a different path.
- HERMES_BOT_BINDING_DB  same as above, alternate env var.
- ASTOR_BOT_BINDING_DB  another alternate env var.

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

# 2026-09-01: removed hardcoded <runtime_dir>bot-binding.db
# Use ASTOR_DIR env var or default ~/.astor — works for any install location.
_DEFAULT_DB_PATH = Path(
    os.environ.get(
        "HERMES_BOT_BINDING_DB",
        os.environ.get(
            "BOT_BINDING_DB_PATH",
            os.environ.get(
                "ASTOR_BOT_BINDING_DB",
                str(Path(os.environ.get("ASTOR_DIR", str(Path.home() / ".astor")))
                    / "bot-binding.db"),
            ),
        ),
    )
)


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


# hermes 的平台名不一定等于 bot-binding.db 的 platform_kind
# （见 astor_memory/_internal/bot_binding.py 的 upsert_platform:
#   platform_id = "<platform_kind>:<account_id>"）。
_PLATFORM_KIND_ALIASES = {
    "discord_main": "discord",
    "discord_main_bot": "discord",
    "telegram_main": "telegram",
    "telegram_bot": "telegram",
    "wechat": "weixin",
}


def _platform_kind(platform: str) -> Optional[str]:
    """Map a hermes platform value to the bot-binding.db platform_kind.

    bot-binding.db 里的 platform_id 形如 ``<platform_kind>:<account_id>``
    （weixin:<id>@im.bot），同一个 kind 下可以有多个 bot 账号，而入站事件
    只带平台种类、不带账号 id —— 所以查询只能按 kind 匹配。
    """
    kind = (platform or "").strip().lower()
    if not kind:
        return None
    return _PLATFORM_KIND_ALIASES.get(kind, kind)


def _lookup_user(platform: str, chat_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a user in bot-binding.db.

    Returns a dict with at least ``user_id`` and ``role`` keys, or None.
    绑定 = “某个会话属于某个用户”，所以只有 chat_id 与平台 kind 都对得上
    的绑定才算数。发送者 id 故意不作为查询键：否则属于别的会话（或别的
    平台）的绑定会替这条消息放行，撤销绑定、停用用户都拦不住。
    """
    db_path = _resolve_db_path()
    if not db_path:
        return None
    # 来源平台或会话不确定时没有可核对的绑定，交回网关的 env allowlist 决定，
    # 不要拿一条“看起来像”的绑定放行。
    kind = _platform_kind(platform)
    if not kind or not chat_id:
        return None

    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # chat_id 必须一致，platform 必须是同一个 kind（每个 bot 的账号
            # id 不同，入站事件拿不到）；user_meta.active 是停用开关，必须
            # 一起生效，否则停用用户照样被放行。ESCAPE 保证 kind 里的 "_"
            # 不被当成通配符。
            cur.execute(
                """
                SELECT b.user_id, b.role_inherit AS role, b.active,
                       m.subscription_plan, m.short_alias, m.real_name
                FROM bindings b
                JOIN user_meta m ON m.user_id = b.user_id AND m.active = 1
                WHERE (b.platform_id = ? OR b.platform_id LIKE ? ESCAPE '\\')
                  AND b.chat_id = ?
                  AND b.active = 1
                LIMIT 1
                """,
                (kind, kind.replace("_", r"\_") + ":%", chat_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
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

    if not platform or not chat_id:
        return None  # leave the gateway's chat-id-less handling to the core

    user = _lookup_user(platform, chat_id)
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