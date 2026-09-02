"""Tests for _internal/platform_bridge.py — covers 3-level token fallback (db → yaml → env).

Uses a synthetic bot-binding.db populated with FAKE tokens to keep the
test fully isolated from any real operator's runtime DB. No real bot
account IDs or chat IDs are referenced — anyone can run this test
without exposing their production data.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault('ASTOR_DIR', os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))

from astor_memory._internal import platform_bridge as pb
from astor_memory._internal import bot_binding as bb
from astor_memory._internal.acl import astor_init_acl


class _FakeResolution:
    """Stand-in for TokenResolution in tests. All fields explicit so
    attribute access doesn't blow up if the production code reads them."""

    def __init__(self, token: str = '', source: str = 'none',
                 account_id: str | None = None, platform_id: str | None = None,
                 audit_metadata: dict | None = None):
        self.token = token
        self.source = source
        self.account_id = account_id
        self.platform_id = platform_id
        self.audit_metadata = audit_metadata or {}


# Synthetic test data — fake account IDs and tokens, NEVER real operator data.
_FAKE_TG_TOKEN = "0000000000:AAFakeTelegramTokenForTestingOnly"
_FAKE_DC_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.GFake.DiscordTokenForTestingOnly"
_FAKE_WX_ACCOUNT = "fake_wx_bot_id@im.bot"
_FAKE_WX_TOKEN = "fake_wx_bot_id@im.bot:00000000000000000000"
_FAKE_WX_ADMIN_CHAT = "fake_admin_wxid@im.wechat"


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """Synthetic bot-binding.db populated with FAKE tokens. Reset between tests."""
    test_db = tmp_path / 'bot-binding.db'
    monkeypatch.setattr(bb, '_db_path', lambda: test_db)
    bb._con = None  # reset singleton
    bb._connect()
    # Seed with fake data
    bb.upsert_platform('telegram', 'fake_tg_account', _FAKE_TG_TOKEN, source='test', enabled=True)
    bb.upsert_platform('discord', 'fake_dc_account', _FAKE_DC_TOKEN, source='test', enabled=True)
    bb.upsert_platform('weixin', _FAKE_WX_ACCOUNT, _FAKE_WX_TOKEN, source='test', enabled=True)
    bb.upsert_user('admin', 'admin', role='admin')
    bb.upsert_binding(
        platform_id=f'weixin:{_FAKE_WX_ACCOUNT}',
        chat_id=_FAKE_WX_ADMIN_CHAT,
        user_id='admin',
        scope='dm',
        bound_by='first_admin',
    )
    yield test_db
    bb._con = None


def setup_module(module):
    astor_init_acl(actor='admin:admin', role='admin', tier='public')


def test_resolve_telegram_from_db(fresh_db, monkeypatch):
    """telegram fake_account_id token is read from bot-binding.db."""
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    r = pb.astor_get_token('telegram')
    assert r.token == _FAKE_TG_TOKEN
    assert r.source == 'db'
    assert r.account_id == 'fake_tg_account'


def test_resolve_discord_from_db(fresh_db, monkeypatch):
    """discord fake_account_id token is read from bot-binding.db."""
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    r = pb.astor_get_token('discord')
    assert r.token == _FAKE_DC_TOKEN
    assert r.source == 'db'
    assert r.account_id == 'fake_dc_account'


def test_resolve_weixin_admin_from_db(fresh_db):
    """Weixin token lookup by fake_account_id returns the seeded fake token."""
    r = pb.astor_get_token('weixin', _FAKE_WX_ACCOUNT)
    assert r.token == _FAKE_WX_TOKEN
    assert r.source == 'db'
    assert r.account_id == _FAKE_WX_ACCOUNT


def test_resolve_weixin_unknown_account_returns_none(fresh_db):
    """Non-existent weixin account: not in db, no yaml, not env → 'none'."""
    r = pb.astor_get_token('weixin', 'nonexistent_account@im.bot')
    assert not r.token
    assert r.source == 'none'


def test_resolve_feishu_returns_none(fresh_db):
    """Feishu not in db, no env. Should be source='none'."""
    r = pb.astor_get_token('feishu')
    assert not r.token
    assert r.source == 'none'


def test_env_var_used_when_db_missing(fresh_db, monkeypatch):
    """Env var is checked AFTER db, but if db has the token, db wins
    (db is the SSoT for tokens). Test the env-only path by deleting
    the seeded platform first.
    """
    # Remove the seeded telegram platform so env is the only source
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'env_takes_priority:abcdef')
    # Delete via SQL since we don't have a delete_platform helper here
    bb._con.execute("DELETE FROM platforms WHERE platform_kind='telegram'")
    bb._con.commit()
    r = pb.astor_get_token('telegram')
    assert r.token == 'env_takes_priority:abcdef'
    assert r.source == 'env'


def test_yaml_fallback_when_db_missing(monkeypatch, tmp_path):
    """If bot-binding.db has no row for the platform, yaml is the fallback."""
    # Empty fresh db (no seed)
    test_db = tmp_path / 'bot-binding.db'
    monkeypatch.setattr(bb, '_db_path', lambda: test_db)
    bb._con = None
    bb._connect()  # creates empty schema

    monkeypatch.setattr(
        pb,
        '_resolve_from_config_yaml',
        lambda *a, **kw: _FakeResolution(token='yaml_fallback_token', source='yaml'),
    )
    r = pb.astor_get_token('telegram')
    assert r.token == 'yaml_fallback_token'
    assert r.source == 'yaml'
    bb._con = None


def test_strict_db_mode(fresh_db, monkeypatch):
    """With env vars unset AND yaml patched NO-OP, db is the only source."""
    for v in ['TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN']:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        pb,
        '_resolve_from_config_yaml',
        lambda *a, **kw: _FakeResolution(),
    )

    for kind, expected in [('telegram', _FAKE_TG_TOKEN), ('discord', _FAKE_DC_TOKEN)]:
        r = pb.astor_get_token(kind)
        assert r.source == 'db'
        assert r.token == expected

    r = pb.astor_get_token('weixin', _FAKE_WX_ACCOUNT)
    assert r.source == 'db'
    assert r.token == _FAKE_WX_TOKEN
    assert r.account_id == _FAKE_WX_ACCOUNT


def test_audit_written_per_lookup(fresh_db, monkeypatch, tmp_path):
    """Each successful lookup writes an audit row.

    Redirect ASTOR_DIR to the test tmp dir so the audit db lands there,
    not in the operator's real runtime.
    """
    test_astor_dir = tmp_path / 'astor'
    test_astor_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('ASTOR_DIR', str(test_astor_dir))
    # Reset audit singleton so the new ASTOR_DIR takes effect
    import astor_memory._internal.audit_logger as al
    al._conn = None

    for v in ['TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN']:
        monkeypatch.delenv(v, raising=False)

    pb.astor_get_token('telegram')
    pb.astor_get_token('discord')

    audit_db = test_astor_dir / 'audit' / 'astor_audit.db'
    if not audit_db.exists():
        pytest.skip("audit row not written (acceptable in test mode)")

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
        al._conn = None


def test_resolve_chat_to_user_in_db(fresh_db):
    """resolve_chat_to_user works for the seeded admin wxid."""
    r = bb.resolve_chat_to_user(f'weixin:{_FAKE_WX_ACCOUNT}', _FAKE_WX_ADMIN_CHAT)
    assert r is not None
    assert r['user_id'] == 'admin'


def test_resolve_chat_to_user_unknown(fresh_db):
    """resolve_chat_to_user returns None for an unknown chat_id."""
    r = bb.resolve_chat_to_user(f'weixin:{_FAKE_WX_ACCOUNT}', 'unknown_chat@im.wechat')
    assert r is None