"""Regression test: a demote-to-private write must keep its embedding.

v1.17.36 (2026-11-04): a tier=public write that the content classifier
demotes to private_<caller> stored the fact but never embedded it.

The ACL is bound in server.py's before_request hook with user_id=body_user.
When the request names no target, body_user was None, so ctx.user_id was
None. The demote target is the CALLER's own id, so the later
astor_check_read('private', '<caller>') fell through to the admin branch
and demanded a grant the admin cannot hold from themselves:

    actor='admin:admin' (role=admin) cannot read private_<admin>;
    user grant required (strict privacy model 2026-08-16)

The write itself succeeded; the embedding step then raised, and astor
swallowed it into an embedding_failed audit row -- so the fact was stored
but unsearchable by vector.

Evidence: astor_daily_summary.py posts tier=public with no 'user' field;
every daily summary since 2026-10-29 has that embedding_failed row.

This exercises the REAL request path (create_app + test_client), not a
copy of the bind logic -- a copy would pass with or without the fix.
"""
from __future__ import annotations

import pytest

from astor_memory.server import create_app


# Personal content -> the classifier demotes it from public to private.
PERSONAL = "\u6211\u4eca\u5929\u4e70\u4e86 NVDA 100 \u80a1, \u6211\u7684\u8d26\u6237\u4f59\u989d\u662f 12345 \u7f8e\u5143"
# Neutral method text -> stays public, must not be affected by the fix.


@pytest.fixture()
def app_and_client(tmp_path, monkeypatch):
    runtime = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(runtime))
    app = create_app()
    return app.test_client(), runtime


def _embedding_failures(runtime) -> list:
    """Every embedding_failed error string recorded in the runtime's bus DBs."""
    import sqlite3
    out = []
    for db in runtime.rglob("astor_bus_*.db"):
        con = sqlite3.connect(str(db))
        try:
            for (meta,) in con.execute(
                "SELECT metadata FROM audit_log WHERE event='embedding_failed'"
            ):
                out.append(str(meta))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return out


def _contents(db_path) -> list:
    import sqlite3
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        return [r[0] or "" for r in con.execute(
            "SELECT content FROM memory_canonical WHERE tombstoned=0"
        )]
    finally:
        con.close()


def test_public_write_without_user_field_does_not_fail_embedding(app_and_client):
    """The regression: no 'user' field + demote -> embedding_failed."""
    client, runtime = app_and_client
    resp = client.post("/v1/write", json={
        "text": PERSONAL,
        "kind": "observation",
        "tier": "public",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]

    failures = _embedding_failures(runtime)
    assert not failures, (
        "demote-to-private write failed to embed: "
        f"{failures} -- the ACL bind left ctx.user_id=None"
    )


def test_demoted_write_lands_in_the_callers_own_private_bucket(app_and_client):
    """The demote target is the caller's own bucket."""
    client, runtime = app_and_client
    resp = client.post("/v1/write", json={
        "text": PERSONAL,
        "kind": "observation",
        "tier": "public",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]

    admin_db = runtime / "users" / "admin" / "memory" / "astor_bus_admin.db"
    assert admin_db.exists(), "the demoted fact did not land in private_<admin>"
    assert any(PERSONAL[:20] in c for c in _contents(admin_db)), \
        "demoted fact missing from private_<admin>"


def test_admin_cannot_read_another_users_private_without_a_grant(app_and_client):
    """The fix must not turn admin into an implicit root.

    With no 'user' field the caller is admin:admin, and ctx.user_id is now
    'admin'. Reading someone ELSE's private still needs a grant from them.
    """
    client, _ = app_and_client
    resp = client.post("/v1/read", json={
        "query": "anything",
        "tier": "private",
        "user": "admin",
        "user_id": "someone-else",
    })
    assert resp.status_code == 403, resp.get_data(as_text=True)[:200]
