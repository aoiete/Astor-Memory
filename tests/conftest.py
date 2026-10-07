"""Shared test fixtures.

Conservative: only seed admin.lock (a simple file the CLI commands read).
Doesn't recreate bot-binding.db (other tests need its full schema from
real `am init`); only ensures the admin user_meta row exists.

v1.16.62: also sets ASTOR_TEST_NO_PREWARM=1 to disable the dashboard
prewarm background thread (which holds 16+ transient sqlite connections
that file-lock tmpdir .db files at tearDown). See astor_memory/server.py
create_app() for the gate.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# --- PYTHONPATH scrub (2026-10-07) ----------------------------------------
# Symptom: running pytest from a shell that inherited a Hermes PYTHONPATH
# made 34 tests fail with
#     ImportError: cannot import name '_imaging' from 'PIL'
#     (<hermes>/installs/<hash>/environments/<hash2>/venv/Lib/site-packages/PIL)
# Root cause: fastembed resolves from this venv's site-packages, but PIL
# resolves from the inherited Hermes install venv, whose _imaging binary was
# built for a different CPython. Any /v1/read path that touches
# astor_get_embedding_model() then 500s. The code is fine — the environment is
# wrong. Verified: same suite, same interpreter, scrubbed env => 867 passed.
#
# Why here: conftest.py is imported by pytest before collection, so anything
# spawned from it (subprocesses, the Flask test client, fixture setups)
# inherits the clean value. Same class of bug was fixed for the server's
# watchdog launcher (astor_tunnel_watchdog.py spawns with a scrubbed env);
# this closes the test-side host.
_LEAK_MARKERS = ("hermes\\installs", "hermes_kernel", "hermes-agent")

_pypath = os.environ.get("PYTHONPATH", "")
_kept = [
    p for p in _pypath.split(os.pathsep)
    if p and not any(m in p for m in _LEAK_MARKERS)
]
# The package lives in the repo root; astor_memory resolves from there.
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in _kept:
    _kept.insert(0, _repo_root)
os.environ["PYTHONPATH"] = os.pathsep.join(_kept)

# Drop already-resolved leak entries from this interpreter's sys.path too:
# conftest runs after site initialisation, so the polluted entries are live.
sys.path[:] = [
    p for p in sys.path
    if not any(m in p for m in _LEAK_MARKERS)
]
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope='session')
def _disable_dashboard_prewarm_in_tests():
    """v1.16.62: skip the dashboard prewarm background thread in tests.

    The thread opens 16 transient sqlite3 connections to compute the
    initial dashboard payload. These are only released when the thread
    exits, but on Windows + Linux the thread is still running when the
    test's tearDown fires shutil.rmtree(tmpdir), causing
    PermissionError on bot-binding.db / *.db-wal / *.db-shm.
    """
    os.environ['ASTOR_TEST_NO_PREWARM'] = '1'
    yield
    # Don't unset on session teardown — other tests in the same process
    # may still need it set.


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
