"""Tests for `am doctor` health-check CLI."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault('ASTOR_DIR', os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))

from astor_memory.cli.main import main
from astor_memory._internal import bot_binding as _bb


@pytest.fixture(autouse=True)
def fresh_bot_binding_db(monkeypatch, tmp_path):
    """Setup a minimal bot-binding.db + admin.lock in tmp so `am` CLI commands work.

    R-class: test fixtures must use isolated tmp dirs so `am` CLI sees a
    real (but minimal) bot-binding.db rather than failing on missing tables.
    """
    # Use a tmp ASTOR_DIR for the CLI to find
    test_astor = tmp_path / 'astor'
    test_astor.mkdir()
    test_db = test_astor / 'bot-binding.db'
    # Write admin.lock so _require_first_admin() passes
    import json
    (test_astor / 'admin.lock').write_text(json.dumps({
        'user_id': 'admin',
        'locked_at': '2026-09-02T00:00:00+00:00',
        'plan': 'power',
        'role': 'admin',
    }))
    monkeypatch.setenv('ASTOR_DIR', str(test_astor))
    monkeypatch.setattr(_bb, '_db_path', lambda: test_db)
    _bb._con = None
    # Init schema via real _init_schema (exercises public tables)
    _bb._init_schema(sqlite3.connect(str(test_db)))
    # Seed admin user_meta so platform verify doesn't trip "every binding.user_id has user_meta"
    con = sqlite3.connect(str(test_db))
    con.execute(
        "INSERT OR IGNORE INTO user_meta "
        "(user_id, short_alias, role, subscription_plan, active) "
        "VALUES ('admin', 'admin', 'admin', 'power', 1)"
    )
    con.commit()
    con.close()
    yield test_db
    _bb._con = None


def test_am_doctor_runs():
    """`am doctor` returns 0 and shows health summary."""
    sys.argv = ['am', 'doctor']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_version_returns_0_3_0():
    """`am version` reports v0.3.0."""
    sys.argv = ['am', 'version']
    rc = main(sys.argv[1:])
    assert rc == 0
    # version line should contain "0.3.0"


def test_am_platform_verify_invariants_pass():
    """`am platform verify` returns 0 (all 6 invariants pass)."""
    sys.argv = ['am', 'platform', 'verify']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_platform_list_works():
    """`am platform list` returns 0 and lists 7 platforms (1 TG + 1 DC + 5 weixin)."""
    sys.argv = ['am', 'platform', 'list']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_bot_list_users_works():
    """`am bot list-users` returns 0."""
    sys.argv = ['am', 'bot', 'list-users']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_admin_whoami_works():
    """`am admin whoami` returns 0 with admin.lock info."""
    sys.argv = ['am', 'admin', 'whoami']
    rc = main(sys.argv[1:])
    assert rc == 0
