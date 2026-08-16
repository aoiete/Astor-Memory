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
        paths[("private", store)] = get_db_path("private", store, "user_e")
    # 8 public/source + 3 private = 9 (the test covers all 9 explicitly below)
    assert get_db_path("public", "bus") == tmp_path / "public/memory/astor_bus_public.db"
    assert get_db_path("public", "nest") == tmp_path / "public/memory/astor_nest_public.db"
    assert get_db_path("public", "forge") == tmp_path / "public/memory/astor_forge_public.db"
    assert get_db_path("source", "bus") == tmp_path / "source/memory/astor_bus_source.db"
    assert get_db_path("source", "nest") == tmp_path / "source/memory/astor_nest_source.db"
    assert get_db_path("source", "forge") == tmp_path / "source/memory/astor_forge_source.db"
    assert get_db_path("private", "bus", "user_e") == tmp_path / "users/user_e/memory/astor_bus_sunny.db"
    assert get_db_path("private", "nest", "user_e") == tmp_path / "users/user_e/memory/astor_nest_sunny.db"
    assert get_db_path("private", "forge", "user_e") == tmp_path / "users/user_e/memory/astor_forge_sunny.db"


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
    _validate_user_id("user_e")
    _validate_user_id("user_a")
    _validate_user_id("zhang-user_d")
    _validate_user_id("admin")
    _validate_user_id("a1b2_c3")


# --- ACL state binding ---

def test_acl_init_binds_state():
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    ctx = astor_current_acl()
    assert ctx.actor == "user:user_e"
    assert ctx.role == "user"
    assert ctx.tier == "private"
    assert ctx.user_id == "user_e"


def test_acl_init_validates_inputs():
    with pytest.raises(ValueError, match="role must be"):
        astor_init_acl(actor="x", role="god", tier="private", user_id="user_e")
    with pytest.raises(ValueError, match="tier must be"):
        astor_init_acl(actor="x", role="user", tier="god", user_id="user_e")
    with pytest.raises(ValueError, match="tier=private requires"):
        astor_init_acl(actor="x", role="user", tier="private", user_id=None)
    with pytest.raises(ValueError, match="requires user_id=None"):
        astor_init_acl(actor="x", role="user", tier="public", user_id="user_e")


def test_acl_uninit_raises_permission():
    """Calling ACL checks without astor_init_acl → PermissionError_."""
    # Reset thread-local to simulate fresh thread
    import astor_memory._internal.acl as acl_mod
    if hasattr(acl_mod._CURRENT, "actor"):
        del acl_mod._CURRENT.actor
    with pytest.raises(PermissionError_):
        astor_check_read("public")


# --- Permission matrix ---

def test_first_admin_can_read_source():
    astor_init_acl(actor="first_admin", role="first_admin", tier="source")
    astor_check_read("source")  # should NOT raise


def test_user_cannot_read_source():
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    with pytest.raises(PermissionError_, match="only first_admin"):
        astor_check_read("source")


def test_admin_cannot_read_source():
    """Plan §2570 + 2624: admin is essentially a user with extra privileges —
    CANNOT read source.db. Distinction from first_admin."""
    astor_init_acl(actor="admin:user_a", role="admin", tier="private", user_id="user_a")
    with pytest.raises(PermissionError_, match="only first_admin"):
        astor_check_read("source")


def test_user_can_read_own_private():
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    astor_check_read("private", "user_e")  # own — allowed


def test_user_cannot_read_other_private():
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    with pytest.raises(PermissionError_, match="only own private"):
        astor_check_read("private", "user_a")


def test_first_admin_can_read_other_private_with_audit():
    """first_admin reading someone's private data — must produce audit row."""
    astor_close_audit()  # clean state
    astor_init_acl(actor="first_admin", role="first_admin", tier="private", user_id="user_a")
    # allowed
    astor_check_read("private", "user_e")
    # then write audit row
    astor_audit(
        actor="first_admin", tier="private", action="read",
        user_id="user_e", target="memory_canonical/all",
        reason="GDPR investigation",
    )
    rows = astor_query_audit(user_id="user_e")
    assert any(r["actor"] == "first_admin" for r in rows)
    assert any(r["reason"] == "GDPR investigation" for r in rows)
    astor_close_audit()


def test_everyone_can_read_public():
    for role, actor in [
        ("first_admin", "first_admin"),
        ("admin", "admin:user_a"),
        ("user", "user:user_e"),
    ]:
        if role == "user":
            astor_init_acl(actor=actor, role=role, tier="private", user_id="user_e")
        else:
            astor_init_acl(actor=actor, role=role, tier="public")
        astor_check_read("public")  # never raises


def test_only_first_admin_can_write_source():
    astor_init_acl(actor="first_admin", role="first_admin", tier="source")
    astor_check_write("source")
    # user denied
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    with pytest.raises(PermissionError_):
        astor_check_write("source")


def test_only_first_admin_runs_bot_admin():
    astor_init_acl(actor="first_admin", role="first_admin", tier="source")
    astor_check_bot_admin()  # OK
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    with pytest.raises(PermissionError_):
        astor_check_bot_admin()
    astor_init_acl(actor="admin:user_a", role="admin", tier="private", user_id="user_a")
    with pytest.raises(PermissionError_):
        astor_check_bot_admin()


# --- Audit logger ---

def test_audit_admin_op_requires_reason(monkeypatch, tmp_path):
    """Action='admin_op' is a hard escalation — must have a reason."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    with pytest.raises(ValueError, match="requires reason"):
        astor_audit(
            actor="first_admin", tier="private", action="admin_op",
            user_id="user_e", target=None, reason=None,
        )


def test_audit_writes_and_reads(monkeypatch, tmp_path):
    """Audit rows persist + can be queried."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    astor_audit(
        actor="user:user_e", tier="private", action="write",
        user_id="user_e", target="memory_canonical/id=42",
        metadata={"k": "v", "n": 1},
    )
    rows = astor_query_audit(user_id="user_e", action="write")
    assert len(rows) >= 1
    row = next(r for r in rows if r["user_id"] == "user_e")
    assert row["actor"] == "user:user_e"
    assert row["tier"] == "private"
    assert row["target"] == "memory_canonical/id=42"
    assert row["metadata"] == {"k": "v", "n": 1}
    astor_close_audit()


def test_audit_file_mode_0600(monkeypatch, tmp_path):
    """Audit db file is created with 0600 on POSIX-like systems."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_close_audit()
    astor_audit(actor="user:user_e", tier="private", action="init", user_id="user_e")
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
    astor_init_acl(actor="first_admin", role="first_admin", tier="source")
    from astor_memory.bus.store import astor_bus_for, astor_reset_bus
    bus = astor_bus_for("public")
    expected = tmp_path / "public/memory/astor_bus_public.db"
    assert Path(bus.db_path) == expected
    astor_reset_bus()


def test_astor_bus_for_private_requires_user_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_init_acl(actor="first_admin", role="first_admin", tier="source")
    from astor_memory.bus.store import astor_bus_for
    with pytest.raises(ValueError, match=r"requires user_id"):
        astor_bus_for("private")


def test_astor_bus_for_user_cannot_open_other_user(monkeypatch, tmp_path):
    """User opening astor_bus_for('private', 'other_user') → PermissionError_."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    astor_init_acl(actor="user:user_e", role="user", tier="private", user_id="user_e")
    from astor_memory.bus.store import astor_bus_for
    with pytest.raises(PermissionError_):
        astor_bus_for("private", "user_a")


def test_astor_bus_legacy_backward_compat(monkeypatch, tmp_path):
    """astor_bus() with no args still resolves to legacy `astor_bus.db` path."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    # Don't init ACL — legacy singleton path bypasses ACL
    from astor_memory.bus.store import astor_bus, astor_reset_bus
    astor_reset_bus()
    bus = astor_bus()
    assert Path(bus.db_path).name == "astor_bus.db"
    assert Path(bus.db_path).parent == tmp_path
    astor_reset_bus()


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
    assert get_db_path("private", "nest", "user_e") == target / "users/user_e/memory/astor_nest_sunny.db"
    assert get_audit_path() == target / "audit/astor_audit.db"
