"""v1.16.62 — Centralized test-state cleanup.

When test fixtures monkeypatch ASTOR_DIR to a fresh tmpdir, the
module-level singleton caches (bus / nest / lex / forge / peer)
hold SQLite connections that file-lock the .db + .db-wal + .db-shm
files. If those connections aren't closed before tearDown removes
the tmpdir, the cleanup fails with:

  OSError: [Errno 39] Directory not empty: '/tmp/tmpXXXXXX'

This helper closes ALL module-level caches in one call so test
tearDowns can fully release the tmpdir. Verified fix for CI failure
on Python 3.10 + 3.11: tests/test_peer_*.py (14 tests) had
tearDown_tmp calling close_all_connections() but the bus/nest/lex/
forge singletons kept the tmpdir .db files locked.

Usage in tests (unittest):
    from astor_memory._internal.test_state import astor_close_all_test_state
    def tearDown_tmp(self):
        astor_close_all_test_state()
        self._tmp.cleanup()

Idempotent — safe to call multiple times and from any thread.
Never raises — closes happen under try/except so a half-broken
singleton does not block the rest.
"""
from __future__ import annotations


def astor_close_all_test_state() -> None:
    """Close all module-level singleton caches used in tests.

    Closes (in order):
    - peer_relationships._RELATIONSHIPS_CONN (peer cache)
    - bus._BUS_SINGLETONS + bus._astor_bus_singleton (bus cache)
    - vector_store._nest_singleton (nest/vector cache)
    - lex_index._LEX_SINGLETONS (lex index cache)
    - forge._forge_conns (forge cache)
    """
    # 1. peer_relationships (function already exists since v1.14.68)
    try:
        from astor_memory._internal.peer_relationships import close_all_connections
        close_all_connections()
    except Exception:
        pass

    # 2. bus (astor_reset_bus closes both _astor_bus_singleton + _BUS_SINGLETONS
    #    since v1.16.62)
    try:
        from astor_memory.bus.store import astor_reset_bus
        astor_reset_bus()
    except Exception:
        pass

    # 3. nest / vector_store (astor_reset_nest closes _nest_singleton values
    #    since v1.16.x)
    try:
        from astor_memory.nest.vector_store import astor_reset_nest
        astor_reset_nest()
    except Exception:
        pass

    # 4. lex index (astor_reset_lex added in v1.16.62)
    try:
        from astor_memory.nest.lex_index import astor_reset_lex
        astor_reset_lex()
    except Exception:
        pass

    # 5. forge (astor_reset_forge added in v1.16.62)
    try:
        from astor_memory.forge import astor_reset_forge
        astor_reset_forge()
    except Exception:
        pass

    # 6. bot_binding (close() existed since v1.x; held .db on tmpdir)
    try:
        from astor_memory._internal.bot_binding import close as _bb_close
        _bb_close()
    except Exception:
        pass

    # 7. audit_logger (similar singleton close pattern)
    try:
        from astor_memory._internal.audit_logger import _reset_audit_conn
        _reset_audit_conn()
    except Exception:
        pass

    # 8. peer_rate_limit (in-memory _BUCKET clear)
    try:
        from astor_memory._internal.peer_rate_limit import _BUCKET, _LOCK
        with _LOCK:
            _BUCKET.clear()
    except Exception:
        pass

    # 9. grants (sqlite singleton for grants.db)
    try:
        from astor_memory._internal import grants as _grants_mod
        if hasattr(_grants_mod, '_conn') and _grants_mod._conn is not None:
            try:
                _grants_mod._conn.close()
            except Exception:
                pass
            _grants_mod._conn = None
    except Exception:
        pass


__all__ = ["astor_close_all_test_state"]
