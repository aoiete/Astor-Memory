"""test_reflection_skip_list.py — v1.15.44 (Ship P3.1g)

Reflection must NEVER tombstone mental_model + knowledge_page rows.
Two layers of defense:
  1. SQL filter: select_episode_clusters() excludes MM/KP from candidates
  2. Tombstone loop: deprecate_old_facts skips them defensively
"""
import sqlite3
import sys
import os
import gc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'astor_memory'))
from bus.schema import astor_init_schema
from nest.reflection import select_episode_clusters, deprecate_old_facts


class _FakeBus:
    """Minimal AstorBus-like wrapper for reflection tests.
    Exposes .conn (sqlite3.Connection) + .write_audit (no-op stub).
    """
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = OFF")
        astor_init_schema(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        # audit log
        self.audit_calls = []

    def write_audit(self, **kwargs):
        self.audit_calls.append(kwargs)


def _make_bus() -> _FakeBus:
    """In-memory SQLite wrapped in _FakeBus."""
    return _FakeBus()


def _insert_fact(bus, fid, kind, content, importance=0.5, confidence=0.5,
                 scope_type="long_term", tier="public"):
    bus.conn.execute("""
        INSERT INTO memory_canonical
        (candidate_id, event_id, namespace, content, kind, scope_type,
         confidence, importance, tier, user_id, promoted_at, promoted_by,
         provenance_kind, provenance_agent, source_ref, source_hash,
         embedding_version, memory_class, tags, metadata, keywords, context,
         access_count, tombstoned)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'admin:admin',
                'test', 'test', 'test', 'test',
                1, 'world_fact', '[]', '{}', '[]', '',
                0, 0)
    """, (fid * 10, f"ns:{fid}", content, kind, scope_type,
          confidence, importance, tier,
          "2026-09-29T00:00:00+00:00"))
    bus.conn.commit()


def test_kind_excludes_mental_model_from_candidates():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "mental_model", "[MM] question: timezone\nanswer: MDT")
        _insert_fact(bus, 2, "mental_model", "[MM] question: timezone2\nanswer: UTC")
        _insert_fact(bus, 3, "fact", "timezone is MDT for admin user")
        clusters = select_episode_clusters(bus, tier="public", user_id=None, min_size=2)
        # Only fact kind forms a cluster (2 mental_model not enough — but they're filtered out)
        # The 1 fact row alone is below min_size=2 → no clusters
        assert len(clusters) == 0, f"expected 0 clusters (MM filtered out), got {clusters}"
        print("  ✓ mental_model rows excluded from candidate set")
    finally:
        bus.conn.close()
        gc.collect()


def test_kind_excludes_knowledge_page_from_candidates():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "knowledge_page", "[KP] slug: arch\ntitle: Architecture")
        _insert_fact(bus, 2, "knowledge_page", "[KP] slug: arch2\ntitle: Architecture v2")
        _insert_fact(bus, 3, "fact", "Architecture documentation knowledge page system")
        _insert_fact(bus, 4, "fact", "Architecture documentation knowledge page version")
        clusters = select_episode_clusters(bus, tier="public", user_id=None, min_size=2)
        # The 2 fact rows should cluster (no KP included)
        assert len(clusters) >= 1, f"expected ≥1 cluster from fact rows, got {clusters}"
        # Verify cluster contains NO knowledge_page rows
        for cluster in clusters:
            for fid in cluster:
                row = bus.conn.execute("SELECT kind FROM memory_canonical WHERE id=?", (fid,)).fetchone()
                assert row["kind"] != "knowledge_page", f"cluster contains KP fid={fid}"
        print("  ✓ knowledge_page rows excluded from candidate set")
    finally:
        bus.conn.close()
        gc.collect()


def test_tombstone_loop_skips_mental_model():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "mental_model", "[MM] question: X\nanswer: Y")
        deprecate_old_facts(bus, loser_ids=[1], winner_id=999, actor="admin:admin")
        row = bus.conn.execute("SELECT tombstoned FROM memory_canonical WHERE id=1").fetchone()
        assert row["tombstoned"] == 0, "mental_model was tombstoned — defensive guard failed"
        print("  ✓ tombstone loop skips mental_model")
    finally:
        bus.conn.close()
        gc.collect()


def test_tombstone_loop_skips_knowledge_page():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "knowledge_page", "[KP] slug: x\ntitle: x")
        deprecate_old_facts(bus, loser_ids=[1], winner_id=999, actor="admin:admin")
        row = bus.conn.execute("SELECT tombstoned FROM memory_canonical WHERE id=1").fetchone()
        assert row["tombstoned"] == 0, "knowledge_page was tombstoned — defensive guard failed"
        print("  ✓ tombstone loop skips knowledge_page")
    finally:
        bus.conn.close()
        gc.collect()


def test_tombstone_loop_still_tombstones_normal_facts():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "fact", "Some fact content")
        deprecate_old_facts(bus, loser_ids=[1], winner_id=999, actor="admin:admin")
        row = bus.conn.execute("SELECT tombstoned FROM memory_canonical WHERE id=1").fetchone()
        assert row["tombstoned"] == 1, "regular fact should still tombstone"
        print("  ✓ regular fact rows still tombstone (regression intact)")
    finally:
        bus.conn.close()
        gc.collect()


def test_explicit_kind_includes_mental_model_when_requested():
    bus = _make_bus()
    try:
        _insert_fact(bus, 1, "mental_model", "[MM] question: timezone preferred\nanswer: MDT")
        _insert_fact(bus, 2, "mental_model", "[MM] question: timezone preferred utc\nanswer: UTC")
        _insert_fact(bus, 3, "mental_model", "[MM] question: timezone preferred est\nanswer: EST")
        clusters = select_episode_clusters(bus, tier="public", user_id=None,
                                           min_size=2, kinds=["mental_model"])
        # 3 similar MMs → 1 cluster of size 3
        assert len(clusters) >= 1, f"expected cluster when kinds=['mental_model'], got {clusters}"
        print("  ✓ explicit kinds=['mental_model'] opt-in still works")
    finally:
        bus.conn.close()
        gc.collect()


if __name__ == "__main__":
    tests = [
        test_kind_excludes_mental_model_from_candidates,
        test_kind_excludes_knowledge_page_from_candidates,
        test_tombstone_loop_skips_mental_model,
        test_tombstone_loop_skips_knowledge_page,
        test_tombstone_loop_still_tombstones_normal_facts,
        test_explicit_kind_includes_mental_model_when_requested,
    ]
    failed = 0
    for t in tests:
        print(f"\n[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"Total: {len(tests)}, Passed: {len(tests)-failed}, Failed: {failed}")
    sys.exit(0 if failed == 0 else 1)
