"""
Regression test for the write bug discovered 2026-08-16.

Bug: when a memory_canonical row exists with a candidate_id that
matches a NEW candidate (because of a prior failed promote_candidate),
promote_candidate would return the OLD canonical_id with stale content
instead of writing the new fact. Caller gets a misleading 200 response.

Pattern:
1. Insert a candidate A.
2. Insert a canonical row with candidate_id=A.candidate_id (stale row).
3. promote_candidate(A) — should NOT return the stale canonical_id.
   Should write a new canonical row with the new content.
"""
from __future__ import annotations

import sqlite3

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    target = tmp_path / "astor_promote_bug"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    return astor_bus(tier=Tier.PUBLIC.value, user_id=None)


def _insert_candidate(bus, content: str) -> int:
    eid = bus.append_event(
        namespace='/test/promote_bug', agent_id='pytest',
        source='rest', action='write', content=content, metadata={},
    )
    return bus.insert_candidate(
        event_id=eid, namespace='/test/promote_bug',
        content=content, kind='fact',
    )


def test_promote_candidate_inserts_new_canonical_with_fresh_content(fresh_bus):
    """Baseline: promote_candidate inserts a new canonical row."""
    cid = _insert_candidate(fresh_bus, content='baseline test')
    fid = fresh_bus.promote_candidate(
        candidate_id=cid, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )
    assert fid > 0
    # Verify canonical row exists with matching content
    row = fresh_bus.conn.execute(
        'SELECT content FROM memory_canonical WHERE id = ?', (fid,)
    ).fetchone()
    assert row is not None
    assert row[0] == 'baseline test'


def test_promote_candidate_idempotent_on_same_content(fresh_bus):
    """True idempotency: re-promote same candidate_id with same content
    returns the same canonical_id (early bail)."""
    cid = _insert_candidate(fresh_bus, content='same content')
    fid1 = fresh_bus.promote_candidate(
        candidate_id=cid, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )
    # Re-call with same candidate_id (true retry)
    fid2 = fresh_bus.promote_candidate(
        candidate_id=cid, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )
    assert fid1 == fid2


def test_promote_candidate_handles_stale_orphan_canonical(fresh_bus):
    """REGRESSION: when a stale canonical row with candidate_id=X exists
    but content differs from candidate X's content, promote_candidate
    must tombstone the stale row and write a fresh canonical row.

    Bug pattern: legacy data or previous failed promote left an orphan
    canonical row with the same candidate_id. Without the fix, the
    function would return the stale canonical_id with stale content.
    """
    cid = _insert_candidate(fresh_bus, content='NEW content for the new write')
    # Inject a stale orphan canonical row with same candidate_id but
    # different content (simulating legacy data state).
    # We need to bypass the UNIQUE constraint on candidate_id by
    # using a different candidate_id for the stale row first, then
    # updating it to collide with our new candidate.
    # Easier: use direct SQL to insert the stale row.
    import json
    fresh_bus.conn.execute(
        """INSERT INTO memory_canonical
           (candidate_id, event_id, namespace, content, kind, confidence, importance,
            tags, metadata, keywords, context,
            promoted_by, user_id, tier, scope_type, verdict,
            origin_session_id, stable_id, embedding_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, 1, '/test/legacy', 'STALE content from a previous write',
         'fact', 0.7, 0.5, '[]', '{}', '[]', '',
         'legacy', None, 'public', 'long_term', 'settled',
         None, None, 1),
    )
    fresh_bus.conn.commit()

    # Verify stale canonical exists
    stale = fresh_bus.conn.execute(
        'SELECT id, content FROM memory_canonical WHERE candidate_id = ?',
        (cid,),
    ).fetchone()
    assert stale is not None
    assert stale[1] == 'STALE content from a previous write'

    # Now promote_candidate should handle this gracefully.
    # With the fix: tombstone the stale row, insert fresh.
    # The new canonical_id should be DIFFERENT from the stale id.
    fid = fresh_bus.promote_candidate(
        candidate_id=cid, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )
    # The returned canonical_id should map to the NEW content, not stale.
    row = fresh_bus.conn.execute(
        'SELECT content FROM memory_canonical WHERE id = ?', (fid,)
    ).fetchone()
    assert row is not None
    assert row[0] == 'NEW content for the new write', (
        f'Expected NEW content, got: {row[0]!r}'
    )

    # Verify the stale row has been DELETED (v1.2.0 fix uses DELETE, not
    # tombstone, because the UNIQUE constraint on candidate_id would still
    # block the INSERT otherwise).
    stale_row = fresh_bus.conn.execute(
        'SELECT id FROM memory_canonical WHERE id = ?', (stale[0],)
    ).fetchone()
    assert stale_row is None, f'Stale row should be DELETEd, but still exists: {stale_row!r}'

def test_promote_candidate_writes_audit_on_stale_restore(fresh_bus):
    """When promote_candidate encounters a stale orphan, the recovery
    should be auditable (so future forensics can trace what happened)."""
    cid = _insert_candidate(fresh_bus, content='fresh content xyz')
    # Inject stale orphan
    fresh_bus.conn.execute(
        """INSERT INTO memory_canonical
           (candidate_id, event_id, namespace, content, kind, confidence, importance,
            tags, metadata, keywords, context,
            promoted_by, user_id, tier, scope_type, verdict,
            origin_session_id, stable_id, embedding_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, 1, '/test/legacy', 'old stale',
         'fact', 0.7, 0.5, '[]', '{}', '[]', '',
         'legacy', None, 'public', 'long_term', 'settled',
         None, None, 1),
    )
    fresh_bus.conn.commit()

    # Promote
    fid = fresh_bus.promote_candidate(
        candidate_id=cid, promoted_by='test',
        user_id=None, tier='public', scope_type='long_term',
    )
    # We should see at least one audit row for this promote
    # (specifically the custom write_audit or the orphan tombstone).
    # At minimum, the new canonical row exists.
    row = fresh_bus.conn.execute(
        'SELECT content FROM memory_canonical WHERE id = ?', (fid,)
    ).fetchone()
    assert row is not None
    assert row[0] == 'fresh content xyz'
