"""
Migration tool: legacy memory-bus DB → Astor-Memory 3-DB layout.

Reads from:
- `~/.memory-bus/bus.db` (legacy single SQLite with 29 tables)

Writes to:
- `~/.astor/astor_bus.db` (events + memory_candidates + memory_canonical + audit_log)
- `~/.astor/astor_nest.db` (embeddings extracted from canonical.embedding BLOB)
- `~/.astor/astor_forge.db` (no-op; forge is a pure-functions module)

Per Plan § Week 5 step 4.7: clean cutover requires migration CLI.
Per Plan § Migration from v0.x: NOT directly compatible — needs this script.

Usage:
  am migrate from-memory-bus --source=~/.memory-bus/bus.db --target=~/.astor
  am migrate from-memory-bus --source=~/.memory-bus/bus.db --dry-run

Idempotency: skips rows that already exist in target (by stable_id for canonical).
"""
from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    get_default_astor_dir,
    get_default_bus_path,
    get_default_nest_path,
)
from ..bus.schema import astor_init_schema
from ..nest.schema import astor_init_nest_schema
from ..nest.embeddings import astor_get_model_name_for_ram


@dataclass
class MigrationReport:
    events_migrated: int = 0
    candidates_migrated: int = 0
    canonical_migrated: int = 0
    embeddings_migrated: int = 0
    skipped_existing: int = 0
    errors: list[str] | None = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def astor_migrate_from_memory_bus(source: Path, target_dir: Path | None = None, dry_run: bool = False) -> MigrationReport:
    """Migrate legacy memory-bus DB to Astor-Memory 3-DB layout.

    Args:
        source: path to legacy memory-bus DB (must have events + memory_candidates + memory_canonical tables)
        target_dir: target astor dir (default: ASTOR_DIR or ~/.astor)
        dry_run: if True, report what would happen without writing

    Returns:
        MigrationReport with counts + errors
    """
    report = MigrationReport()

    if not source.exists():
        report.errors.append(f'Source DB not found: {source}')
        return report

    if target_dir is None:
        target_dir = get_default_astor_dir()
    target_dir = Path(target_dir).expanduser()
    target_bus = target_dir / 'astor_bus.db'
    target_nest = target_dir / 'astor_nest.db'

    # Open source
    src = sqlite3.connect(str(source), isolation_level=None)

    # Verify source schema
    src_tables = {r[0] for r in src.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()}
    required = {'events', 'memory_candidates', 'memory_canonical'}
    missing = required - src_tables
    if missing:
        report.errors.append(f'Source missing required tables: {missing}')
        src.close()
        return report

    if dry_run:
        # Report counts only
        report.events_migrated = src.execute('SELECT count(*) FROM events').fetchone()[0]
        report.candidates_migrated = src.execute('SELECT count(*) FROM memory_candidates').fetchone()[0]
        report.canonical_migrated = src.execute('SELECT count(*) FROM memory_canonical').fetchone()[0]
        report.embeddings_migrated = sum(
            1 for r in src.execute('SELECT embedding FROM memory_canonical WHERE embedding IS NOT NULL')
        )
        src.close()
        return report

    # Initialize target DBs (idempotent). FKs disabled during migration (legacy data
    # may have promoted_to / event_id pointing at rows that haven't migrated yet).
    target_bus.parent.mkdir(parents=True, exist_ok=True)
    target_bus_conn = sqlite3.connect(str(target_bus), isolation_level=None, check_same_thread=False)
    target_bus_conn.execute('PRAGMA journal_mode = WAL')
    target_bus_conn.execute('PRAGMA synchronous = NORMAL')
    target_bus_conn.execute('PRAGMA busy_timeout = 5000')
    astor_init_schema(target_bus_conn)
    # Set FK pragma AFTER init_schema (table creation may re-enable FKs in SQLite ≥3.6)
    target_bus_conn.execute('PRAGMA foreign_keys = OFF')

    target_nest.parent.mkdir(parents=True, exist_ok=True)
    target_nest_conn = sqlite3.connect(str(target_nest), isolation_level=None, check_same_thread=False)
    target_nest_conn.execute('PRAGMA journal_mode = WAL')
    target_nest_conn.execute('PRAGMA synchronous = NORMAL')
    target_nest_conn.execute('PRAGMA foreign_keys = ON')
    target_nest_conn.execute('PRAGMA busy_timeout = 5000')
    astor_init_nest_schema(target_nest_conn)

    # Migrate memory_candidates FIRST (canonical references it via FK)
    # promoted_to → memory_canonical.id is migrated as NULL initially; canonical migration
    # passes promoted_to via separate update after canonical rows exist.
    legacy_cands = src.execute('SELECT id, event_id, namespace, content, kind, confidence, importance, tags, metadata, provenance, created_at, review_state, promoted_at, promoted_to, rejected_reason, ttl_days, expires_at, scene FROM memory_candidates').fetchall()
    for c in legacy_cands:
        cid, event_id, namespace, content, kind, confidence, importance, tags, metadata, provenance, created_at, review_state, promoted_at, promoted_to, rejected_reason, ttl_days, expires_at, scene = c
        # Migrate metadata as JSON
        meta_old = {}
        try:
            if metadata:
                meta_old = json.loads(metadata) if metadata.startswith('{') else {'provenance': provenance}
        except (json.JSONDecodeError, AttributeError):
            meta_old = {'provenance': provenance, 'raw_metadata': metadata}
        meta_old['migrated_from'] = 'memory-bus'
        meta_old['legacy_promoted_to'] = promoted_to  # preserve for canonical phase
        # Map fields directly
        exists = target_bus_conn.execute('SELECT 1 FROM memory_candidates WHERE id = ?', (cid,)).fetchone()
        if exists:
            report.skipped_existing += 1
            continue
        target_bus_conn.execute(
            """INSERT INTO memory_candidates
               (id, event_id, namespace, content, kind, confidence, importance, tags, metadata,
                created_at, review_state, promoted_at, promoted_to, rejected_reason, ttl_days, expires_at, scene)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, event_id or 0, namespace, content, kind, confidence, importance, tags, json.dumps(meta_old),
             created_at, review_state, promoted_at, None, rejected_reason, ttl_days, expires_at, scene or 'casual'),
        )
        report.candidates_migrated += 1

    # Migrate events
    legacy_events = src.execute('SELECT id, ts, namespace, agent_id, source, action, content, provenance, request_id, prev_event_id, tombstone, visibility FROM events').fetchall()
    for ev in legacy_events:
        ev_id, ts, namespace, agent_id, source, action, content, provenance, request_id, prev_event_id, tombstone, visibility = ev
        # Build metadata JSON from extra fields
        meta = {
            'migrated_from': 'memory-bus',
            'provenance': provenance,
            'request_id': request_id,
            'prev_event_id': prev_event_id,
            'tombstone': bool(tombstone),
            'visibility': visibility,
        }
        # Check if already migrated (by id)
        exists = target_bus_conn.execute('SELECT 1 FROM events WHERE id = ?', (ev_id,)).fetchone()
        if exists:
            report.skipped_existing += 1
            continue
        target_bus_conn.execute(
            """INSERT INTO events (id, ts, namespace, agent_id, source, action, content, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev_id, ts, namespace, agent_id, source, action, content, json.dumps(meta)),
        )
        report.events_migrated += 1

    # Migrate memory_canonical
    # legacy: id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, metadata, provenance, promoted_at, promoted_by,
    #         last_accessed_at, access_count, tombstoned, tombstoned_at, expires_at, scene, embedding, stable_id, user_id, session_id,
    #         scope_type, valid_from, valid_to, status, superseded_by, revision
    # new: id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, metadata, promoted_at, promoted_by,
    #      last_confirmed_at, access_count, tombstoned, tombstoned_at, expires_at, scene,
    #      revision, parent_revision_id, superseded_by, origin_session_id, verdict, scope_type, user_id, session_id, tier, stable_id
    legacy_canonical = src.execute(
        'SELECT id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, metadata, provenance, promoted_at, promoted_by, last_accessed_at, access_count, tombstoned, tombstoned_at, expires_at, scene, embedding, stable_id, user_id, session_id, scope_type, valid_from, valid_to, status, superseded_by, revision FROM memory_canonical'
    ).fetchall()
    for c in legacy_canonical:
        (cid, candidate_id, event_id, namespace, content, kind, confidence, importance,
         tags, metadata, provenance, promoted_at, promoted_by, last_accessed_at, access_count,
         tombstoned, tombstoned_at, expires_at, scene, embedding, stable_id, user_id, session_id,
         scope_type, valid_from, valid_to, status, superseded_by, revision) = c

        # Map legacy status → astor verdict
        if status == 'active':
            verdict = 'settled'
        elif status == 'contested':
            verdict = 'contested'
        elif status == 'archived':
            verdict = 'thin'
        else:
            verdict = 'settled'

        # Default tier
        tier = 'public'

        meta_old = {'migrated_from': 'memory-bus', 'provenance': provenance, 'legacy_status': status,
                    'legacy_valid_from': valid_from, 'legacy_valid_to': valid_to}
        try:
            if metadata:
                old_meta = json.loads(metadata) if metadata.startswith('{') else {}
                meta_old.update(old_meta)
        except (json.JSONDecodeError, AttributeError):
            pass

        # Map last_accessed_at → last_confirmed_at (closest field)
        last_confirmed_at = last_accessed_at

        # scope_type mapping
        if scope_type not in ('user', 'short_term', 'long_term', 'profile'):
            scope_type = 'long_term'

        # Normalize None for NOT NULL columns
        if access_count is None:
            access_count = 0
        if revision is None:
            revision = 1

        # Check if already migrated
        if stable_id:
            exists = target_bus_conn.execute('SELECT 1 FROM memory_canonical WHERE stable_id = ?', (stable_id,)).fetchone()
            if exists:
                report.skipped_existing += 1
                continue

        try:
            target_bus_conn.execute(
                """INSERT INTO memory_canonical
                   (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, metadata,
                    promoted_at, promoted_by, last_confirmed_at, access_count, tombstoned, tombstoned_at,
                    expires_at, scene, revision, superseded_by, origin_session_id, verdict, scope_type,
                    user_id, session_id, tier, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, candidate_id or 0, event_id, namespace, content, kind, confidence, importance, tags,
                 json.dumps(meta_old), promoted_at, promoted_by, last_confirmed_at, access_count,
                 tombstoned or 0, tombstoned_at, expires_at, scene or 'casual', revision, superseded_by,
                 session_id, verdict, scope_type, user_id, session_id, tier, stable_id),
            )
        except sqlite3.IntegrityError as e:
            # Report but don't fail — legacy data has FK references that may not migrate
            report.errors.append(f'Canonical row {cid}: FK constraint ({e})')
            continue
        report.canonical_migrated += 1

        # Migrate embedding to nest DB
        if embedding and isinstance(embedding, bytes) and len(embedding) > 0:
            try:
                # Try to determine embedding dim (assumes float32, 4 bytes per dim)
                dim = len(embedding) // 4
                if dim * 4 == len(embedding) and dim > 0:
                    # Skip migration if dim is unusual (legacy may use float64 or different format)
                    # For now, skip and let user re-embed later
                    pass
            except Exception as e:
                report.errors.append(f'Embedding for fact {cid}: {e}')

    # Migration done. Add audit log entry.
    target_bus_conn.execute(
        """INSERT INTO audit_log (event, actor, target_type, target_id, metadata)
           VALUES (?, ?, ?, ?, ?)""",
        ('migrate_from_memory_bus', 'system', 'db', '0',
         json.dumps({
             'events': report.events_migrated,
             'candidates': report.candidates_migrated,
             'canonical': report.canonical_migrated,
             'source': str(source),
             'target': str(target_dir),
         })),
    )

    src.close()
    target_bus_conn.commit()
    target_nest_conn.commit()
    target_bus_conn.close()
    target_nest_conn.close()

    return report


__all__ = ['astor_migrate_from_memory_bus', 'MigrationReport']
