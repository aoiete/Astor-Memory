"""Regression: a private fact must be recallable after it is written.

v1.17.36 (2026-11-09)

Writing to another user's private tier succeeded while the fact stayed
unrecallable. The bus was opened as the owner of the scope, but the
EMBEDDING that follows re-opens the nest, and the nest open re-runs the
read check under whatever identity is current at that moment. The owner
binding had already been released, so the embed ran as the original
actor and was refused:

    actor='user:admin' (role=user) cannot read private_<user>;
    only own private allowed

The refusal was swallowed into cascade_state as `embedding_failed`. The
fact existed in memory_canonical with no embedding, so every recall for
it returned empty -- a write-only black hole.

These tests pin the invariant: ONE identity authorizes the write AND the
embedding of that same fact into that same scope.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _bus_db(root: Path, user: str) -> Path:
    return root / "users" / user / "memory" / f"astor_bus_{user}.db"


def _nest_db(root: Path, user: str) -> Path:
    return root / "users" / user / "memory" / f"astor_nest_{user}.db"


def _embedded_ids(root: Path, user: str) -> set:
    """Fact ids that actually have an embedding row in the nest DB."""
    nest = _nest_db(root, user)
    if not nest.exists():
        return set()
    conn = sqlite3.connect(str(nest))
    try:
        rows = conn.execute("SELECT fact_id FROM embeddings").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _canonical_ids(root: Path, user: str) -> set:
    bus = _bus_db(root, user)
    conn = sqlite3.connect(str(bus))
    try:
        rows = conn.execute(
            "SELECT id FROM memory_canonical WHERE tombstoned = 0"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


@pytest.fixture
def astor_root(monkeypatch, tmp_path):
    """Isolated ASTOR_DIR so the test never touches the live runtime.

    tmp_path (pytest's own per-test dir) is used rather than
    tempfile.TemporaryDirectory: astor opens a few SQLite handles that
    stay open for the life of the process, and on Windows the teardown
    of a TemporaryDirectory would fail with "file is being used by
    another process". pytest cleans its own tmp dirs at session end,
    outside our teardown, so no lock fight.
    """
    root = tmp_path / "astor"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ASTOR_DIR", str(root))
    yield root


def test_own_private_scope_binds_and_restores(monkeypatch):
    """own_private_scope binds the owner, then restores the previous identity."""
    from astor_memory._internal.acl import (
        astor_current_acl,
        astor_init_acl,
        own_private_scope,
    )

    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")
    before = astor_current_acl()

    with own_private_scope("someone"):
        inside = astor_current_acl()
        assert inside.actor == "user:someone"
        assert inside.role == "user"
        assert inside.user_id == "someone"

    after = astor_current_acl()
    assert after.actor == before.actor
    assert after.role == before.role
    assert after.user_id == before.user_id


def test_own_private_scope_restores_on_exception(monkeypatch):
    """A raise inside the scope still restores the previous identity."""
    from astor_memory._internal.acl import (
        astor_current_acl,
        astor_init_acl,
        own_private_scope,
    )

    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")
    before = astor_current_acl()

    with pytest.raises(RuntimeError):
        with own_private_scope("someone"):
            raise RuntimeError("boom")

    after = astor_current_acl()
    assert after.actor == before.actor
    assert after.user_id == before.user_id


def test_private_write_is_recallable(astor_root, monkeypatch):
    """A fact written to a private scope must be recallable afterwards.

    This is the whole bug: the write succeeded, the fact was in the bus,
    and recall still returned nothing because the embedding was refused.

    The sequence below is the one the CLI actually runs -- open the
    private bus as the owner (open_own_private binds the owner identity
    ONLY for the open, then restores it), write, then promote. The
    promote is where the embedding is computed, and it runs AFTER the
    owner binding is gone. The test deliberately does NOT wrap the
    promote in own_private_scope: the fix has to live inside
    promote_candidate, because that is the code path every caller uses.
    """
    from astor_memory._internal.acl import astor_init_acl
    from astor_memory.bus.store import astor_bus

    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")

    user = "someone"
    text = "R-class: private facts must be recallable, not just writable."

    # Exactly what the CLI does: open_own_private binds the owner
    # identity only for the bus open and restores it on return.
    from astor_memory._internal.acl import open_own_private

    bus = open_own_private(user)
    event_id = bus.append_event(
        namespace=user,
        agent_id="test",
        source="test.write",
        action="write",
        content=text,
    )
    cand_id = bus.insert_candidate(
        event_id=event_id,
        namespace=user,
        content=text,
        kind="rule",
    )
    canon_id = bus.promote_candidate(
        cand_id, promoted_by="test", user_id=user, tier="private",
    )

    # The fact is in the bus ...
    assert canon_id in _canonical_ids(astor_root, user)

    # ... AND it has an embedding. Before v1.17.36 this set was empty:
    # the embed was refused and swallowed into cascade_state.
    assert canon_id in _embedded_ids(astor_root, user), (
        f"fact {canon_id} was written but never embedded -- it is "
        f"unrecallable. This is the v1.17.36 regression."
    )


def test_no_failed_embed_recorded(astor_root, monkeypatch):
    """A successful private write must not leave an embedding_failed audit row."""
    from astor_memory._internal.acl import astor_init_acl, open_own_private
    from astor_memory.bus.store import astor_bus

    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")

    user = "someone"
    text = "R-class: private facts must be recallable, not just writable."

    bus = open_own_private(user)
    event_id = bus.append_event(
        namespace=user, agent_id="test", source="test.write",
        action="write", content=text,
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace=user, content=text,
        kind="rule",
    )
    bus.promote_candidate(
        cand_id, promoted_by="test", user_id=user, tier="private",
    )

    bus_path = _bus_db(astor_root, user)
    conn = sqlite3.connect(str(bus_path))
    try:
        rows = conn.execute(
            "SELECT metadata FROM audit_log WHERE event = 'embedding_failed'"
        ).fetchall()
    finally:
        conn.close()
    assert not rows, f"embedding_failed recorded: {rows}"
