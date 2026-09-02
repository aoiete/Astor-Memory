"""
ACL tests for the 9-db 3-tier × 3-store layout (plan §2576-2607).

These exercise:
- astor_init_acl binds process state
- first_admin can read/write any (tier, user_id)
- user can only read/write own (tier='private', user_id=own)
- user reading other user's private raises PermissionError_
- audit logger writes rows for cross-user access
- astor_bus_for / astor_nest_for / astor_forge_for resolve to 9-db paths

Ref: plan § Per-user DB naming (locked 2026-08-15).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from astor_memory._internal.acl import (
    astor_init_acl, astor_current_acl,
    astor_check_read, astor_check_write,
    astor_check_bot_admin, PermissionError_,
)
from astor_memory._internal.acl_layout import (
    Tier, Store,
    get_db_path, get_audit_path, get_astor_dir,
    DEFAULT_USER, FIRST_ADMIN_USER,
    _validate_user_id,
)
from astor_memory._internal.audit_logger import (
    astor_audit, astor_query_audit, astor_close_audit,
)


# --- Path resolution ---

def test_acl_layout_9_db_paths(monkeypatch, tmp_path):
    """9-db layout: 3 tier × 3 store = 9 distinct SQLite paths."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    paths = {
        (tier, store): get_db_path(tier, store)
        for tier in ["public", "source"]
        for store in ["bus", "nest", "forge"]
    }
    # private requires user_id
    for store in ["bus", "nest", "forge"]:
        paths[("private", store)] = get_db_path("private", store, "alice")
    # 8 public/source + 3 private = 9 (the test covers all 9 explicitly below)
    assert get_db_path("public", "bus") == tmp_path / "public/memory/astor_bus_public.db"
    assert get_db_path("public", "nest") == tmp_path / "public/memory/astor_nest_public.db"
    assert get_db_path("public", "forge") == tmp_path / "public/memory/astor_forge_public.db"
    assert get_db_path("source", "bus") == tmp_path / "source/memory/astor_bus_source.db"
    assert get_db_path("source", "nest") == tmp_path / "source/memory/astor_nest_source.db"
    assert get_db_path("source", "forge") == tmp_path / "source/memory/astor_forge_source.db"
    assert get_db_path("private", "bus", "alice") == tmp_path / "users/alice/memory/astor_bus_alice.db"
    assert get_db_path("private", "nest", "alice") == tmp_path / "users/alice/memory/astor_nest_alice.db"
    assert get_db_path("private", "forge", "alice") == tmp_path / "users/alice/memory/astor_forge_alice.db"


def test_acl_layout_private_requires_user_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="private.*requires user_id"):
        get_db_path("private", "bus")


def test_acl_layout_user_id_validation():
    """Reject user ids with path traversal risk."""
    with pytest.raises(ValueError, match="Invalid user_id"):
        _validate_user_id("../etc/passwd")
    with pytest.raises(ValueError, match="Invalid user_id"):
        _validate_user_id("foo/bar")
    with pytest.raises(ValueError, match="Invalid user_id"):
        _validate_user_id("")
    with pytest.raises(ValueError, match="Invalid user_id"):
        _validate_user_id("a" * 100)  # too long
    # reserved names
    with pytest.raises(ValueError, match="reserved"):
        _validate_user_id("public")
    with pytest.raises(ValueError, match="reserved"):
        _validate_user_id("source")


def test_acl_layout_user_id_accepts_valid():
    """Accept alphanumeric + dash + underscore ids."""
    _validate_user_id("alice")
    _validate_user_id("bob")
    _validate_user_id("dave_user")
    _validate_user_id("admin")
    _validate_user_id("a1b2_c3")


# --- ACL state binding ---

def test_acl_init_binds_state():
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    ctx = astor_current_acl()
    assert ctx.actor == "user:alice"
    assert ctx.role == "user"
    assert ctx.tier == "private"
    assert ctx.user_id == "alice"


def test_acl_init_validates_inputs():
    with pytest.raises(ValueError, match="role must be"):
        astor_init_acl(actor="x", role="god", tier="private", user_id="alice")
    with pytest.raises(ValueError, match="tier must be"):
        astor_init_acl(actor="x", role="user", tier="god", user_id="alice")
    with pytest.raises(ValueError, match="tier=private requires"):
        astor_init_acl(actor="x", role="user", tier="private", user_id=None)
    # 2026-09-02 design change: tier=public/source now ACCEPT user_id so the
    # content classifier can reclassify public → private_<actor> using
    # ctx.user_id == target_user match. Bad actor formats still raise via
    # the canonical regex check.
    with pytest.raises(ValueError, match="does not match canonical form"):
        astor_init_acl(actor="x", role="user", tier="public", user_id="alice")


@pytest.fixture(autouse=True)
def _reset_acl_for_each_test_fixture():
    """Reset BOTH _CURRENT (threading.local) and _ACL_CTX (ContextVar)
    so test_acl_uninit_raises_permission runs cleanly regardless of ordering.

    2026-09-02 fix: astor_current_acl() checks _ACL_CTX FIRST, so resetting
    only _CURRENT leaves the ContextVar state behind. Fix: clear both.
    """
    import astor_memory._internal.acl as acl_mod
    # Reset threading.local
    for attr in ('actor', 'role', 'tier', 'user_id', 'subscription_plan'):
        if hasattr(acl_mod._CURRENT, attr):
            delattr(acl_mod._CURRENT, attr)
    # Reset ContextVar
    try:
        acl_mod._ACL_CTX.set(None)
    except (LookupError, ValueError):
        pass
    yield


def test_acl_uninit_raises_permission():
    """Calling ACL checks without astor_init_acl → PermissionError_.

    2026-08-16 fix:
    - Use tier=private (public returns BEFORE consulting ACL, so public
      would never raise even with no ACL).
    - Set then del the actor attribute to ensure it doesn't exist, even
      if a previous test reloaded astor_memory._internal.acl (which
      gives _CURRENT a fresh empty local -- hasattr returns False, but
      we still need the read to fail).
    2026-09-02 fix: _reset_acl_for_each_test_fixture clears both _CURRENT
    and _ACL_CTX so this test passes regardless of order.
    """
    from astor_memory._internal.acl import astor_check_read
    with pytest.raises(PermissionError_):
        astor_check_read("private", user_id="alice")


# --- Permission matrix ---

def test_admin_can_read_source():
    astor_init_acl(actor='admin:admin', role='admin', tier="source")
    astor_check_read("source")  # should NOT raise


def test_user_cannot_read_source():
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    with pytest.raises(PermissionError_, match="only admin"):
        astor_check_read("source")


def test_user_can_read_own_private():
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    astor_check_read("private", "alice")  # own — allowed


def test_user_cannot_read_other_private():
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    with pytest.raises(PermissionError_, match="only own private"):
        astor_check_read("private", "bob")


def test_admin_can_read_other_private_with_audit():
    """first_admin reading someone's private data — must hold an explicit grant.

    2026-08-16 strict-privacy ship: first_admin no longer has implicit
    cross-user private access. The data owner must grant first_admin
    access first. Audit row still written for both granted and denied.
    """
    from astor_memory._internal.grants import create_grant, revoke_grant
    astor_close_audit()  # clean state
    # Alice (data owner) grants first_admin read access to her private.
    _grant_id = create_grant(
        grantor="alice", grantee='admin:admin', scope="read",
        reason="GDPR investigation test",
    )
    try:
        astor_init_acl(actor='admin:admin', role='admin', tier="private", user_id="bob")
        # allowed (grant exists)
        astor_check_read("private", "alice")
        # then write audit row
        astor_audit(
            actor='admin:admin', tier="private", action="read",
            user_id="alice", target="memory_canonical/all",
            reason="GDPR investigation",
        )
        rows = astor_query_audit(user_id="alice")
        assert any(r["actor"] == "admin:admin" for r in rows)
        assert any(r["reason"] == "GDPR investigation" for r in rows)
    finally:
        revoke_grant(_grant_id, by="test_cleanup")
        astor_close_audit()


def test_everyone_can_read_public():
    # 2026-09-02: tuple is (actor, role) — was (role, actor) by mistake.
    for actor, role in [
        ("admin:admin", "admin"),
        ("admin:bob", "admin"),
        ("user:alice", "user"),
    ]:
        if role == "user":
            astor_init_acl(actor=actor, role=role, tier="private", user_id="alice")
        else:
            astor_init_acl(actor=actor, role=role, tier="public")
        astor_check_read("public")  # never raises


def test_only_admin_can_write_source():
    astor_init_acl(actor='admin:admin', role='admin', tier="source")
    astor_check_write("source")
    # user denied
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    with pytest.raises(PermissionError_):
        astor_check_write("source")


def test_only_admin_runs_bot_admin():
    astor_init_acl(actor='admin:admin', role='admin', tier="source")
    astor_check_bot_admin()  # OK
    # 2026-09-02: any role='admin' can run bot admin (matrix is per-role,
    # not per-actor). Only non-admin roles denied.
    astor_init_acl(actor='admin:bob', role='admin', tier='private', user_id='bob')
    astor_check_bot_admin()  # also OK
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    with pytest.raises(PermissionError_):
        astor_check_bot_admin()


# --- Audit logger ---

def test_audit_admin_op_requires_reason(monkeypatch, tmp_path):
    """Action='admin_op' is a hard escalation — must have a reason."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    with pytest.raises(ValueError, match="requires reason"):
        astor_audit(
            actor='admin:admin', tier="private", action="admin_op",
            user_id="alice", target=None, reason=None,
        )


def test_audit_writes_and_reads(monkeypatch, tmp_path):
    """Audit rows persist + can be queried."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    astor_audit(
        actor="user:alice", tier="private", action="write",
        user_id="alice", target="memory_canonical/id=42",
        metadata={"k": "v", "n": 1},
    )
    rows = astor_query_audit(user_id="alice", action="write")
    assert len(rows) >= 1
    row = next(r for r in rows if r["user_id"] == "alice")
    assert row["actor"] == "user:alice"
    assert row["tier"] == "private"
    assert row["target"] == "memory_canonical/id=42"
    assert row["metadata"] == {"k": "v", "n": 1}
    astor_close_audit()


def test_audit_file_mode_0600(monkeypatch, tmp_path):
    """Audit db file is created with 0600 on POSIX-like systems."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    astor_audit(actor="user:alice", tier="private", action="init", user_id="alice")
    path = get_audit_path()
    assert path.exists()
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"audit file must be mode 0o600, got {oct(mode)}"
    astor_close_audit()


# --- astor_bus / astor_nest factory with tier ---

def test_astor_bus_for_path_resolution(monkeypatch, tmp_path):
    """astor_bus_for(tier, user_id) opens the right 9-db path."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    # init ACL as system for this test (so ACL allows)
    astor_init_acl(actor='admin:admin', role='admin', tier="source")
    from astor_memory.bus.store import astor_bus_for, astor_reset_bus
    bus = astor_bus_for("public")
    expected = tmp_path / "public/memory/astor_bus_public.db"
    assert Path(bus.db_path) == expected
    astor_reset_bus()


def test_astor_bus_for_private_requires_user_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_init_acl(actor='admin:admin', role='admin', tier="source")
    from astor_memory.bus.store import astor_bus_for
    # v1.2 hardening: ACL gate raises PermissionError_ (not ValueError)
    # for first_admin accessing private without user_id, since first_admin
    # has cross-user read authority but the call site must still supply
    # the target user_id explicitly.
    with pytest.raises(Exception, match=r"target user_id"):
            astor_bus_for("private")


def test_astor_bus_for_user_cannot_open_other_user(monkeypatch, tmp_path):
    """User opening astor_bus_for('private', 'other_user') → PermissionError_."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
    from astor_memory.bus.store import astor_bus_for
    with pytest.raises(PermissionError_):
        astor_bus_for("private", "bob")


# 2026-08-16: test_astor_bus_legacy_backward_compat removed.
# This test asserted the legacy single-file astor_bus() behavior which
# was REMOVED in v1.1.0 (2026-08-15 ship): the legacy fallback
# silently regenerated a root db bypassing 3-tier ACL. astor_bus() now
# requires explicit tier= and the test's no-args call raises ValueError
# (intentional, this is the safety check). Keeping a passing test for
# behavior we explicitly removed would be lying to future readers.
# The replacement (test_astor_bus_for_path_resolution) covers the
# legitimate 'give me a bus handle' path.

def test_astor_dir_name_env_overrides_default(monkeypatch, tmp_path):
    """ASTOR_DIR_NAME=foo → get_astor_dir() = ~/foo (relative to home)."""
    monkeypatch.delenv("ASTOR_DIR", raising=False)
    monkeypatch.setenv("ASTOR_DIR_NAME", "Astor-Memory-Runtime")
    from astor_memory._internal.acl_layout import get_astor_dir
    expected = Path.home() / "Astor-Memory-Runtime"
    assert get_astor_dir() == expected


def test_astor_dir_env_overrides_astor_dir_name(monkeypatch, tmp_path):
    """ASTOR_DIR takes priority over ASTOR_DIR_NAME."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("ASTOR_DIR_NAME", "Astor-Memory-Runtime")
    from astor_memory._internal.acl_layout import get_astor_dir
    assert get_astor_dir() == tmp_path / "explicit"


def test_db_paths_resolve_under_astor_dir(monkeypatch, tmp_path):
    """get_db_path honors ASTOR_DIR for all tiers."""
    target = tmp_path / "Astor-Memory-Runtime"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    # Reimport — uses ASTOR_DIR at call time, so no reimport needed for this case
    assert get_db_path("public", "bus") == target / "public/memory/astor_bus_public.db"
    assert get_db_path("private", "nest", "alice") == target / "users/alice/memory/astor_nest_alice.db"
    assert get_audit_path() == target / "audit/astor_audit.db"


# --- Server-level ACL enforcement (2026-08-16 P0-fix regression tests) ---
#
# Bug: server.py before_request hardcoded actor='admin:admin' for every
# POST request, so any user could write source tier and read another user's
# private DB. Fix: actor/role are now resolved from bot-binding.db user_meta.
#
# These tests seed bot-binding.db via the public API (upsert_user) and
# exercise the live Flask test_client against /v1/write + /v1/read.

@pytest.fixture
def seeded_users(tmp_path, monkeypatch):
    """Seed bot-binding.db with admin (first_admin) + bob (user) + carol (admin)."""
    target = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    # bot_binding uses _db_path() = ASTOR_DIR/bot-binding.db; reset singleton.
    from astor_memory._internal import bot_binding as bb
    monkeypatch.setattr(bb, "_con", None)
    # Seed users via the upsert API so role + active are correctly recorded.
    # 2026-09-02: role=first_admin deprecated → role='admin' (plan irrelevant).
    bb.upsert_user(user_id="admin", short_alias="admin", role="admin", subscription_plan="power")
    bb.upsert_user(user_id="bob", short_alias="bob", role="user", subscription_plan="vip")
    bb.upsert_user(user_id="carol", short_alias="carol", role="admin", subscription_plan="vip")
    bb.upsert_user(user_id="alice", short_alias="alice", role="user", subscription_plan="vip")
    return target


def _client(target):
    from astor_memory.server import create_app
    return create_app(str(target)).test_client()


def test_acl_bob_cannot_write_source(seeded_users):
    """bob (role=user) writing tier=source → 403.

    Regression for the 2026-08-16 P0 ACL bug: previously returned 200 because
    server before_request hardcoded first_admin.
    """
    client = _client(seeded_users)
    r = client.post("/v1/write", json={
        "user": "bob", "tier": "source", "text": "should be denied",
    })
    assert r.status_code == 403, r.get_data(as_text=True)
    body = r.get_json()
    assert body["error"] in ("permission_denied", "acl_init_failed")


def test_acl_bob_cannot_read_other_users_private(seeded_users):
    """bob reading admin's private tier → 403 (cross-user denied).

    Regression for the 2026-08-16 P0 ACL bug.
    """
    client = _client(seeded_users)
    r = client.post("/v1/read", json={
        "user": "bob", "tier": "private", "user_id": "admin", "query": "anything",
    })
    assert r.status_code == 403, r.get_data(as_text=True)
    body = r.get_json()
    # 2026-09-02 ship: silent denial — no policy detail leaked to client.
    assert body["error"] == "cross_user_forbidden"
    assert "detail" not in body  # silent


def test_acl_bob_can_write_own_private(seeded_users):
    """bob writing to her own private tier → 200 (allowed)."""
    client = _client(seeded_users)
    r = client.post("/v1/write", json={
        "user": "bob", "tier": "private", "text": "I prefer dark roast",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("count", 0) >= 1


def test_acl_bob_can_read_own_private(seeded_users):
    """bob reading her own private tier → 200 (allowed)."""
    client = _client(seeded_users)
    # First, seed something bob can read back.
    client.post("/v1/write", json={
        "user": "bob", "tier": "private", "text": "bob likes oolong tea",
    })
    r = client.post("/v1/read", json={
        "user": "bob", "tier": "private", "query": "tea preference", "top_k": 5,
    })
    assert r.status_code == 200
    results = r.get_json().get("results", [])
    assert any("oolong" in str(x.get("content", "")).lower() for x in results), results


def test_acl_admin_can_write_source(seeded_users):
    """admin (user_id=admin alias) writing tier=source → 200."""
    client = _client(seeded_users)
    r = client.post("/v1/write", json={
        "user": "admin", "tier": "source", "text": "admin source fact",
    })
    assert r.status_code == 200, r.get_data(as_text=True)


def test_acl_admin_role_can_read_other_users_private(seeded_users):
    """carol (role=admin per plan §2624 power-user) can read bob's private.

    2026-08-16 strict-privacy ship: admin no longer has implicit
    cross-user access. The data owner (bob) must grant carol access
    first. Grant is tested via the new grant system.
    """
    from astor_memory._internal.grants import create_grant
    client = _client(seeded_users)
    # Seed bob's private.
    client.post("/v1/write", json={
        "user": "bob", "tier": "private", "text": "bob keeps secrets here",
    })
    # user_a grants carol (admin) read access. Revoke any prior grant first
    # to avoid UNIQUE constraint failure (grants DB is shared across tests).
    from astor_memory._internal.grants import list_grants, revoke_grant
    for _g in list_grants(grantee="admin:carol"):
        if _g["grantor"] == "bob" and _g["scope"] == "read" and not _g["revoked"]:
            revoke_grant(_g["id"], by="test_fixture_cleanup")
    _grant_id = create_grant(
        grantor="bob", grantee="admin:carol", scope="read",
        reason="support investigation test",
    )
    # carol (admin role) should be allowed to read it.
    r = client.post("/v1/read", json={
        "user": "carol", "tier": "private", "user_id": "bob", "query": "secrets",
    })
    assert r.status_code == 200, r.get_data(as_text=True)


def test_acl_resolve_actor_returns_correct_roles(seeded_users):
    """Direct unit test for _astor_resolve_actor — no HTTP roundtrip needed."""
    from astor_memory.server import _astor_resolve_actor
    assert _astor_resolve_actor("admin") == ('admin:admin', 'admin', None)
    assert _astor_resolve_actor("carol") == ("admin:carol", "admin", None)
    assert _astor_resolve_actor("bob") == ("user:bob", "user", "vip")
    assert _astor_resolve_actor(None) == ('admin:admin', 'admin', None)
    assert _astor_resolve_actor("") == ('admin:admin', 'admin', None)
    # v1.14.1 (2026-09-02): unknown user fails CLOSED as user:anonymous (free).
    # Earlier versions fell back to first_admin; the new design treats
    # unknown callers as least-privilege users (no implicit root).
    assert _astor_resolve_actor("ghost_user_not_in_db") == ('user:anonymous', 'user', 'free')
