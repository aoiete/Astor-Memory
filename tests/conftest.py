"""Shared test fixtures.

Conservative: only seed admin.lock (a simple file the CLI commands read).
Doesn't recreate bot-binding.db (other tests need its full schema from
real `am init`); only ensures the admin user_meta row exists.
"""
import json
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _ensure_admin_user_and_lock():
    """Make sure admin user_meta + admin.lock exist for CLI tests.

    Avoids clobbering bot-binding.db (other tests like test_bot_binding
    create their own schemas).
    """
    astor_dir = Path(os.environ.get('ASTOR_DIR', str(Path.home() / '.astor')))
    astor_dir.mkdir(parents=True, exist_ok=True)

    # admin.lock (consumed by `am admin whoami` and ACL bootstrap).
    lock_path = astor_dir / 'admin.lock'
    if not lock_path.exists():
        lock_path.write_text(json.dumps({
            'user_id': 'admin',
            'locked_at': '2026-09-02T00:00:00+00:00',
            'plan': 'power',
            'role': 'admin',
        }, indent=2))

    # Ensure admin user_meta row exists (idempotent INSERT OR IGNORE).
    bb_path = astor_dir / 'bot-binding.db'
    if bb_path.exists():
        try:
            con = sqlite3.connect(str(bb_path))
            con.execute(
                "INSERT OR IGNORE INTO user_meta "
                "(user_id, short_alias, role, subscription_plan, active) "
                "VALUES ('admin', 'admin', 'admin', 'power', 1)"
            )
            con.commit()
            con.close()
        except sqlite3.OperationalError:
            pass  # bot-binding.db has different schema; skip

    yield
