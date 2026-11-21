"""Regression tests for docs/examples/bot_binding_auth_plugin.py.

Finding under test ("Shared-chat binding authorization ignores sender
allowlists"): the plugin resolved the inbound user with a *user_id-only*
fallback ("Fallback 2: any binding for this user_id"), so a message whose
(platform, chat_id) had NO active binding of its own was still authorized as
soon as the sender's user_id matched ANY active binding row of that user --
including a binding for a different chat on a different platform. Revoking
the binding of the chat that actually sent the message, or deactivating the
user in user_meta, did not stop it.

The fix constrains the lookup to the binding that belongs to the incoming
platform/chat and requires user_meta.active = 1.

ANTI-PROOF (re-run after every change to the plugin):

    cp docs/examples/bot_binding_auth_plugin.py "$TMP/plugin_fixed.py"
    git checkout -- docs/examples/bot_binding_auth_plugin.py   # pre-fix file
    python -m pytest tests/test_bot_binding_auth_plugin.py -q  # must FAIL
    cp "$TMP/plugin_fixed.py" docs/examples/bot_binding_auth_plugin.py
    python -m pytest tests/test_bot_binding_auth_plugin.py -q  # must PASS

Isolation: ASTOR_DIR *and* the bot-binding.db path are redirected into
pytest's tmp_path, so this file can never read or write the live runtime.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO_ROOT / "docs" / "examples" / "bot_binding_auth_plugin.py"

SCHEMA = """
CREATE TABLE user_meta (
    user_id          TEXT PRIMARY KEY,
    short_alias      TEXT UNIQUE NOT NULL,
    display_name     TEXT,
    real_name        TEXT,
    role             TEXT NOT NULL DEFAULT 'user',
    subscription_plan TEXT DEFAULT 'trial',
    active           INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE bindings (
    binding_id   TEXT PRIMARY KEY,
    platform_id  TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'single',
    role_inherit TEXT NOT NULL DEFAULT 'user',
    allow_from   TEXT,
    active       INTEGER NOT NULL DEFAULT 1
);
"""


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    """Load the plugin with a throw-away bot-binding.db under tmp_path."""
    db_path = tmp_path / "bot-binding.db"
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "astor"))
    monkeypatch.setenv("BOT_BINDING_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_BOT_BINDING_DB", str(db_path))
    monkeypatch.setenv("ASTOR_BOT_BINDING_DB", str(db_path))

    spec = importlib.util.spec_from_file_location("bot_binding_auth_under_test", PLUGIN_PATH)
    assert spec and spec.loader, f"cannot load {PLUGIN_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 插件必须在临时目录里找 DB，绝不能碰 live runtime。
    assert str(tmp_path) in str(mod._resolve_db_path())
    return mod


@pytest.fixture()
def db(plugin):
    """The throw-away bot-binding.db (schema already created)."""
    db_path = Path(str(plugin._resolve_db_path()))
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return db_path


def _add_user(db_path: Path, user_id: str, active: int = 1) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO user_meta (user_id, short_alias, real_name, role, subscription_plan, active)"
        " VALUES (?, ?, ?, 'user', 'free', ?)",
        (user_id, user_id, user_id, active),
    )
    con.commit()
    con.close()


def _add_binding(
    db_path: Path,
    binding_id: str,
    platform_id: str,
    chat_id: str,
    user_id: str,
    active: int = 1,
) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO bindings (binding_id, platform_id, chat_id, user_id, scope,"
        " role_inherit, allow_from, active) VALUES (?, ?, ?, ?, 'single', 'user', ?, ?)",
        (binding_id, platform_id, chat_id, user_id, chat_id, active),
    )
    con.commit()
    con.close()


def _dispatch(
    mod: Any,
    platform: str,
    chat_id: str,
    user_id: Optional[str],
) -> Optional[dict]:
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_id=chat_id,
        user_id=user_id,
    )
    event = SimpleNamespace(source=source)
    return mod._on_pre_gateway_dispatch(event, gateway=None)


# ---------------------------------------------------------------------------
# The finding: a user_id-only fallback authorizes chats that are not bound.
# ---------------------------------------------------------------------------


def test_unbound_chat_not_authorized_by_sibling_binding(plugin, db):
    """A chat with no binding of its own must not be authorized just because
    the sender's user_id has *some* other active binding."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "weixin:bot@im.bot", "chat-b", "u1")
    # inbound from a chat that was never bound to anybody
    assert _dispatch(plugin, "weixin", "chat-z", "u1") is None


def test_revoked_chat_not_authorized_by_sibling_binding(plugin, db):
    """Revoking the binding of the chat that sent the message must stop it,
    even when the same user still has another active binding elsewhere."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "discord:discord_main", "chat-a", "u1", active=0)  # revoked
    _add_binding(db, "b2", "weixin:bot@im.bot", "chat-b", "u1", active=1)
    assert _dispatch(plugin, "discord", "chat-a", "u1") is None


def test_binding_of_other_platform_not_authorized(plugin, db):
    """A binding on one platform must not authorize a message from another."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "discord:discord_main", "chat-a", "u1")
    assert _dispatch(plugin, "telegram", "chat-a", "u1") is None


def test_deactivated_user_meta_not_authorized(plugin, db):
    """user_meta.active = 0 must deny, even with an active binding row."""
    _add_user(db, "u1", active=0)
    _add_binding(db, "b1", "discord:discord_main", "chat-a", "u1")
    assert _dispatch(plugin, "discord", "chat-a", "u1") is None


def test_no_user_id_fallback_when_platform_unknown(plugin, db):
    """No platform / no chat_id => no lookup at all (never user_id-only)."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "discord:discord_main", "chat-a", "u1")
    assert _dispatch(plugin, "", "chat-a", "u1") is None
    assert _dispatch(plugin, "discord", "", "u1") is None
    assert _dispatch(plugin, "discord", None, "u1") is None


# ---------------------------------------------------------------------------
# The happy paths must keep working (the fix is not a blanket deny).
# ---------------------------------------------------------------------------


def test_active_discord_dm_authorized(plugin, db):
    _add_user(db, "u1")
    _add_binding(db, "b1", "discord:discord_main", "chat-a", "u1")
    res = _dispatch(plugin, "discord", "chat-a", "u1")
    assert res is not None
    assert res["action"] == "authorized"
    assert res["_db_user_id"] == "u1"


def test_active_weixin_dm_authorized(plugin, db):
    """WeChat bindings are per-bot accounts, so the lookup matches the
    platform kind, not one exact platform_id."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "weixin:bot0001@im.bot", "chat-a", "u1")
    res = _dispatch(plugin, "weixin", "chat-a", "u1")
    assert res is not None
    assert res["action"] == "authorized"


def test_active_telegram_dm_authorized(plugin, db):
    _add_user(db, "u1")
    _add_binding(db, "b1", "telegram:hermes_bot", "chat-a", "u1")
    assert _dispatch(plugin, "telegram", "chat-a", "u1") is not None


def test_platform_kind_with_underscore_matches_own_binding_only(plugin, db):
    """A platform kind containing "_" must not act as a LIKE wildcard."""
    _add_user(db, "u1")
    _add_binding(db, "b1", "my_bot:acc1", "chat-a", "u1")
    assert _dispatch(plugin, "my_bot", "chat-a", "u1") is not None
    # "myXbot" must not match "my_bot:..." — the "_" is literal.
    _add_user(db, "u2")
    _add_binding(db, "b2", "myXbot:acc1", "chat-b", "u2")
    assert _dispatch(plugin, "myXbot", "chat-b", "u2") is not None
