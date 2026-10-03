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

    v1.16.x: also calls astor_init_acl so the per-test fixture boundary
    satisfies the process-entry /v1/post init gate that test fixtures
    rely on. Without this, /v1/* endpoints raise PermissionError because
    the global ACL state isn't seeded.
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

    # v1.16.x: Seed astor_init_acl with admin so test fixtures that
    # call astor_bus() directly don't fail with PermissionError.
    # tests/test_acl.py has its own _reset_acl_for_each_test_fixture
    # (autouse) that runs AFTER this one and clears _CURRENT for the
    # test_acl_uninit_raises_permission case. Other test modules
    # (test_auto_link, test_basic, etc.) need the seeded ACL to
    # call astor_bus() / astor_forge() / astor_nest() without
    # PermissionError_. 2026-10-03: was missing, caused 11 ERROR
    # + 3 FAILED on full pytest run.
    # NOTE: actor MUST be 'admin:admin' (canonical form requires
    # 'system' or 'admin:<id>' or 'user:<id>') — see astor_init_acl's
    # _ACTOR_RE check. A bare 'admin' raises ValueError.
    try:
        from astor_memory._internal.acl import astor_init_acl
        astor_init_acl(
            actor='admin:admin', role='admin', tier='public',
            user_id='admin', subscription_plan='power',
        )
    except Exception:
        pass  # ACL already seeded; idempotent

    yield
