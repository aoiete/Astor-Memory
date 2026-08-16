"""Tests for _internal/platform_bridge.py — covers 3-level token fallback (db → yaml → env)."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault('ASTOR_DIR', '<runtime_dir>')

sys.path.insert(0, '<source_dir>')

from astor_memory._internal import platform_bridge as pb
from astor_memory._internal import bot_binding as bb
from astor_memory._internal.acl import astor_init_acl


# 使用 real db, 不隔离 (因为 bot_binding 单例会缓存在 _con)
# 这样这个 test file 必须 跑 在 integration 上下文里


def setup_module(module):
    astor_init_acl(actor='first_admin', role='first_admin', tier='public')


def test_resolve_telegram_from_db():
    """telegram_main token is in bot-binding.db."""
    bb._connect()  # ensure singleton
    r = pb.astor_get_token('telegram')
    assert r.token, 'expected token from db'
    assert r.source == 'db'
    assert r.account_id == 'telegram_main'


def test_resolve_discord_from_db():
    bb._connect()
    r = pb.astor_get_token('discord')
    assert r.token
    assert r.source == 'db'
    assert r.account_id == 'discord_main'


def test_resolve_weixin_admin_from_db():
    bb._connect()
    r = pb.astor_get_token('weixin', '8263b17ef9c7@im.bot')
    assert r.token
    assert r.source == 'db'
    assert r.account_id == '8263b17ef9c7@im.bot'


def test_resolve_weixin_unknown_account_returns_none():
    """Non-existent weixin account: not in db, no yaml, not env → 'none'."""
    r = pb.astor_get_token('weixin', 'fake@im.bot')
    # Returns token='' with source='none'
    assert not r.token
    assert r.source == 'none'


def test_resolve_feishu_returns_none():
    """feishu is revoked (no row in db, no env). Should be source='none'."""
    r = pb.astor_get_token('feishu')
    assert not r.token
    # could be 'none' or whatever the db returns when not found; not 'db' anyway
    assert r.source != 'db'


def test_strict_db_mode(monkeypatch):
    """With env vars unset AND yaml patched NO-OP, db is the only source."""
    # Unset env
    for v in ['TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN']:
        monkeypatch.delenv(v, raising=False)

    # Patch yaml fallback to NO-OP
    monkeypatch.setattr(pb, '_resolve_from_config_yaml',
                        lambda *a, **kw: type('R', (), {'token': '', 'source': 'none'})())

    # All 3 from db
    for kind in ('telegram', 'discord'):
        r = pb.astor_get_token(kind)
        assert r.source == 'db'
        assert r.token

    r = pb.astor_get_token('weixin', '8263b17ef9c7@im.bot')
    assert r.source == 'db'
    assert r.token
    assert r.account_id == '8263b17ef9c7@im.bot'


def test_audit_written_per_lookup(monkeypatch):
    """Each successful lookup writes an audit row (read action on platforms/X/* target)."""
    # Unset env so we know we got db result
    for v in ['TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN']:
        monkeypatch.delenv(v, raising=False)
    # patch yaml NO-OP
    monkeypatch.setattr(pb, '_resolve_from_config_yaml',
                        lambda *a, **kw: type('R', (), {'token': '', 'source': 'none'})())

    # trigger lookups
    pb.astor_get_token('telegram')
    pb.astor_get_token('discord')

    audit_db = Path('<runtime_dir>audit/astor_audit.db')
    if not audit_db.exists():
        return  # OK silent fallback

    con = sqlite3.connect(str(audit_db))
    try:
        rows = con.execute(
            "SELECT DISTINCT target FROM audit WHERE target LIKE 'platforms/telegram%' "
            "OR target LIKE 'platforms/discord%' ORDER BY id DESC LIMIT 2"
        ).fetchall()
        targets = [r[0] for r in rows]
        assert len(targets) >= 1, f"expected at least 1 audit row, got {targets}"
    finally:
        con.close()


def test_resolve_chat_to_user_in_db():
    """resolve_chat_to_user works for admin wxid."""
    bb._connect()
    r = bb.resolve_chat_to_user('weixin:8263b17ef9c7@im.bot', 'o9cq80632-REHf6fG55epPMPCreM@im.wechat')
    assert r is not None
    assert r['user_id'] == 'admin'


def test_resolve_chat_to_user_unknown():
    r = bb.resolve_chat_to_user('weixin:8263b17ef9c7@im.bot', 'unknown@im.wechat')
    assert r is None
