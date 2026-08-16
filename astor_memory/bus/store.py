"""
Bus: SQLite-based event log + canonical fact store.

Provides:
- Bus class with append_event / insert_candidate / promote_candidate
- Transaction context for atomic multi-insert (Plan § Crash recovery)
- Default connection with WAL + foreign keys + busy_timeout (Plan § Memory <-> concurrency)

v1.0 simple install: single file ~/.astor/astor.db
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import astor_init_schema, astor_verify_schema


@dataclass
class AstorEvent:
    """Lightweight event representation."""
    id: int
    ts: str
    namespace: str
    agent_id: str
    source: str
    action: str
    content: str
    metadata: dict[str, Any]


class AstorBus:
    """SQLite-backed bus for events + canonical facts."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._open()

    def _open(self):
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 5000')
        self._conn = conn
        astor_init_schema(conn)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def transaction(self):
        """Atomic transaction context. All writes within commit/rollback together."""
        with self._lock:
            c = self.conn.cursor()
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise

    def append_event(
        self,
        namespace: str,
        agent_id: str,
        source: str,
        action: str,
        content: str,
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> int:
        """Append an event to the bus. Returns event_id."""
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO events
                   (namespace, agent_id, source, action, content, metadata, request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    namespace,
                    agent_id,
                    source,
                    action,
                    content,
                    json.dumps(metadata or {}),
                    request_id,
                ),
            )
            event_id = cur.lastrowid
            assert event_id is not None
            return event_id

    def insert_candidate(
        self,
        event_id: int,
        namespace: str,
        content: str,
        kind: str = 'fact',
        confidence: float = 0.7,
        importance: float = 0.5,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        scene: str = 'casual',
    ) -> int:
        """Insert a candidate fact. Returns candidate_id."""
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO memory_candidates
                   (event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, scene)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    namespace,
                    content,
                    kind,
                    confidence,
                    importance,
                    json.dumps(tags or []),
                    json.dumps(metadata or {}),
                    scene,
                ),
            )
            candidate_id = cur.lastrowid
            assert candidate_id is not None
            return candidate_id

    def promote_candidate(
        self,
        candidate_id: int,
        promoted_by: str,
        user_id: str | None = None,
        tier: str = 'public',
        scope_type: str = 'long_term',
        verdict: str = 'settled',
        origin_session_id: str | None = None,
        stable_id: str | None = None,
    ) -> int:
        """Promote a candidate to canonical. Returns canonical_id.

        After the INSERT, computes embedding via nest and stores it on the
        canonical row so recall() works (Plan § Write-time dedup).
        """
        # P0-fix 2026-08-15: dedup check BEFORE INSERT. If candidate_id was
        # already promoted (e.g. retried write), return existing canonical_id
        # instead of crashing with UNIQUE constraint failure.
        existing_canonical_id = None
        with self.transaction() as c:
            existing = c.execute(
                "SELECT id FROM memory_canonical WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                existing_canonical_id = existing[0]
        if existing_canonical_id is not None:
            # Already promoted — idempotent return. Audit must happen OUTSIDE
            # the now-closed transaction (write_audit opens its own).
            self.write_audit(
                event='promote_idempotent_replay',
                actor=promoted_by or 'system',
                target_type='candidate',
                target_id=candidate_id,
                metadata={'canonical_id': existing_canonical_id},
            )
            return existing_canonical_id

        with self.transaction() as c:
            c.execute(
                "SELECT event_id, namespace, content, kind, confidence, importance, tags, metadata, scene FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            )
            row = c.fetchone()
            if row is None:
                raise ValueError(f"Candidate {candidate_id} not found")
            event_id, namespace, content, kind, confidence, importance, tags, metadata, scene = row
            cur = c.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict,
                    origin_session_id, stable_id, embedding_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict,
                    origin_session_id, stable_id, 1,
                ),
            )
            canonical_id = cur.lastrowid
            assert canonical_id is not None

        # Compute + persist embedding (outside the bus transaction so a slow
        # embedding model doesn't hold the WAL lock).
        # P0-fix 2026-08-15: pass tier + user_id so embedding lands in the correct
        # per-tier nest DB. Previously called astor_nest() with no args which
        # raised ValueError (tier required) and silently swallowed embedding.
        try:
            from ..nest import astor_nest
            nest = astor_nest(tier=tier, user_id=user_id)
            nest.store(canonical_id, content)
        except Exception as e:
            # Embedding failure should not block fact storage; log to audit.
            self.write_audit(
                event='embedding_failed',
                actor=promoted_by or 'system',
                target_type='canonical',
                target_id=canonical_id,
                metadata={'error': str(e)},
            )

        with self.transaction() as c:
            c.execute(
                "UPDATE memory_candidates SET review_state='promoted', promoted_at=CURRENT_TIMESTAMP, promoted_to=? WHERE id=?",
                (canonical_id, candidate_id),
            )
        return canonical_id

    def write_audit(
        self,
        event: str,
        actor: str,
        target_type: str | None = None,
        target_id: str | None = None,
        reason: str | None = None,
        metadata: dict | None = None,
        severity: str = 'info',
    ) -> int:
        """Write an audit log entry. Returns audit_id."""
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO audit_log
                   (event, actor, target_type, target_id, reason, metadata, severity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event, actor, target_type, target_id, reason, json.dumps(metadata or {}), severity),
            )
            audit_id = cur.lastrowid
            assert audit_id is not None
            return audit_id

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# Module-level singleton (lazy init)
_astor_bus_singleton: AstorBus | None = None
_astor_bus_lock = threading.Lock()


def astor_bus(
    db_path: Path | None = None,
    tier: str | None = None,
    user_id: str | None = None,
) -> AstorBus:
    """
    Get or create a bus handle.

    2026-08-15 ship: backward-compat path REMOVED. The legacy single-file
    fallback (auto-creating ASTOR_DIR/astor_bus.db) was removed because it
    silently regenerated a root db that bypasses 3-tier × 3-store ACL.
    Callers MUST now explicitly pass `tier='public'|'source'|'private'`
    (and `user_id=<id>` for private).

    Args:
        db_path:  override the sqlite file path (testing only)
        tier:     'public' / 'source' / 'private' — REQUIRED
        user_id:  required when tier='private'

    Returns:
        AstorBus connected to the resolved db file.

    Raises:
        ValueError if tier is None.
    """
    from .._internal.acl_layout import get_db_path as _gdp
    from .._internal.acl import astor_check_write

    if tier is None:
        raise ValueError(
            "astor_bus() requires tier='public'|'source'|'private'. "
            "The legacy single-file fallback was removed 2026-08-15; "
            "see migrate_root_legacy_to_3tier.py for migration context."
        )
    astor_check_write(tier, user_id)
    if tier == "private" and user_id is None:
        raise ValueError("astor_bus(tier='private') requires user_id")
    target = db_path if db_path is not None else _gdp(tier, "bus", user_id)
    return AstorBus(target)


def astor_bus_for(tier: str, user_id: str | None = None) -> AstorBus:
    """
    Explicit 9-db layout accessor. Always uses the layout-derived path;
    raises PermissionError_ if ACL denies access.

    Examples:
        astor_bus_for('public')
        astor_bus_for('source')           # first_admin only
        astor_bus_for('private', 'user_e') # user_e's own db (or first_admin)
    """
    return astor_bus(db_path=None, tier=tier, user_id=user_id)


def _get_or_create_singleton(path: Path) -> AstorBus:
    """Backward-compat singleton for legacy astor_bus() callers."""
    global _astor_bus_singleton
    with _astor_bus_lock:
        if _astor_bus_singleton is None:
            _astor_bus_singleton = AstorBus(Path(path))
        return _astor_bus_singleton


def astor_reset_bus() -> None:
    """Reset the singleton (for testing)."""
    global _astor_bus_singleton
    with _astor_bus_lock:
        if _astor_bus_singleton is not None:
            _astor_bus_singleton.close()
        _astor_bus_singleton = None


__all__ = ["AstorBus", "AstorEvent", "astor_bus", "astor_bus_for", "astor_reset_bus"]
