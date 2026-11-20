"""The live bot_binding_auth plugin must authorize a chat, not just a user.

The plugin used to fall back to "any active binding for this user_id",
so a user bound to one chat was authorized in every other chat they
could reach the bot in. The live copy also ignored user_meta.active, so
a deactivated user kept their access.

This tests the installed plugin (not the repo example) against a
synthetic binding table.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(os.environ.get(
    "HERMES_HOME",
    r"C:/Users/TheNuts/AppData/Local/hermes")) / "plugins" / "bot_binding_auth" / "__init__.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("live_bot_binding_auth", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(
    PLUGIN.exists(),
    "the installed hermes plugin is not on this machine")
class TestLiveBotBindingAuth(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_binding_auth_")
        self.db = Path(self.tmp) / "bot-binding.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript("""
            CREATE TABLE user_meta (
                user_id TEXT PRIMARY KEY, short_alias TEXT, role TEXT,
                subscription_plan TEXT, real_name TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE bindings (
                binding_id INTEGER PRIMARY KEY, platform_id TEXT, chat_id TEXT,
                user_id TEXT, scope TEXT, role_inherit TEXT, allow_from TEXT,
                active INTEGER DEFAULT 1);
        """)
        conn.executemany(
            "INSERT INTO user_meta (user_id, short_alias, role, active) "
            "VALUES (?, ?, ?, ?)",
            [("alice", "alice", "user", 1),
             ("bob", "bob", "user", 1),
             ("gone", "gone", "user", 0)])
        conn.executemany(
            "INSERT INTO bindings (binding_id, platform_id, chat_id, user_id, "
            "role_inherit, active) VALUES (?, ?, ?, ?, ?, ?)",
            [(1, "weixin:bot1@im.bot", "chat-alice", "alice", "user", 1),
             (2, "weixin:bot1@im.bot", "chat-gone", "gone", "user", 1),
             (3, "discord:discord_main", "chat-discord", "alice", "user", 1)])
        conn.commit()
        conn.close()

        self.mod = _load_plugin()
        self.mod._resolve_db_path = lambda: self.db

    def test_the_bound_chat_is_authorized(self):
        row = self.mod._lookup_user("weixin", "chat-alice", "alice")
        self.assertIsNotNone(row, "the bound chat was not authorized")
        self.assertEqual(row["user_id"], "alice")

    def test_a_different_chat_is_not_authorized(self):
        """The hole: same user, another chat."""
        row = self.mod._lookup_user("weixin", "chat-somewhere-else", "alice")
        self.assertIsNone(
            row,
            "a user was authorized in a chat they are not bound to -- the "
            "fallback ignored the chat entirely")

    def test_a_revoked_user_is_not_authorized(self):
        row = self.mod._lookup_user("weixin", "chat-gone", "gone")
        self.assertIsNone(
            row,
            "a deactivated user was still authorized -- the lookup ignores "
            "user_meta.active")

    def test_another_platform_binding_is_not_authorized(self):
        row = self.mod._lookup_user("weixin", "chat-discord", "alice")
        self.assertIsNone(
            row,
            "a discord chat was authorized through the weixin fallback")

    def test_the_discord_binding_still_works(self):
        row = self.mod._lookup_user("discord", "chat-discord", "alice")
        self.assertIsNotNone(row, "the discord binding stopped working")
        self.assertEqual(row["user_id"], "alice")


if __name__ == "__main__":
    unittest.main()
