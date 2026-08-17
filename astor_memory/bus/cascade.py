"""
Cascade write queue for astor-memory v1.2.0 (2026-08-16 ship).

Pattern: EverOS md_change_state (simplified for astor's SQLite-only stack).

When nest.store() fails during promote_candidate (e.g. embedding model OOM,
fastembed import error, LanceDB unavailable), the fact_id + content + tier
+ user_id are queued in the cascade_state table for retry. A separate
replay pass processes pending rows and re-attempts the embed.

Failure modes that route here:
  - Embedding model not loaded (lazy load failed first time)
  - OOM during batched embed
  - SQLite disk full / I/O error
  - fastembed / numpy version mismatch

What does NOT route here (write fails loud instead):
  - ACL permission denied → caller sees 403, no cascade
  - Schema corruption → write fails fast, audit row written
  - Schema version mismatch → write fails fast

Replay paths:
  - `POST /v1/cascade/replay` (first_admin only) — manual trigger
  - `am cascade replay [--limit=N]` — CLI equivalent
  - Cron: `am cascade replay --limit=50` daily 03:30 MDT (drains backlog)

Per-row state machine:
  pending → succeeded   (embed succeeded on retry)
  pending → failed      (embed still failing after retries; row kept for
                          post-mortem; cleared by `am cascade purge`)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .store import AstorBus


# Status enum
STATUS_PENDING = 'pending'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'


def enqueue(
    bus,
    fact_id: int,
    operation: str,
    tier: str,
    user_id: str | None,
    payload: dict[str, Any],
    error: str,
) -> int:
    """Queue a failed write for later replay.

    Called from promote_candidate when nest.store() raises. Writes a row to
    cascade_state with status='pending' so the next replay pass picks it up.

    Returns the cascade_state.id (the queue row id, NOT fact_id).
    """
    if operation not in ('embed_insert', 'lex_index', 'provenance_link'):
        raise ValueError(f"unknown cascade operation: {operation!r}")
    # Use a fresh connection since this is called outside promote_candidate's
    # transaction (the original transaction has already committed).
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    cur = bus.conn.execute(
        """
        INSERT INTO cascade_state (fact_id, operation, tier, user_id, payload,
                                    last_attempt_at, last_error, status)
        VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?)
        """,
        (fact_id, operation, tier, user_id, payload_json, error, STATUS_PENDING),
    )
    bus.conn.commit()
    return cur.lastrowid or 0


def list_pending(bus, limit: int = 100) -> list[dict]:
    """List pending cascade rows (FIFO by enqueued_at)."""
    rows = bus.conn.execute(
        """
        SELECT id, fact_id, operation, tier, user_id, payload,
               enqueued_at, attempt_count, last_error
        FROM cascade_state
        WHERE status = ?
        ORDER BY enqueued_at ASC
        LIMIT ?
        """,
        (STATUS_PENDING, int(limit)),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r[5])
        except Exception:
            payload = {'_raw': r[5]}
        out.append({
            'id': r[0],
            'fact_id': r[1],
            'operation': r[2],
            'tier': r[3],
            'user_id': r[4],
            'payload': payload,
            'enqueued_at': r[6],
            'attempt_count': r[7],
            'last_error': r[8],
            'status': STATUS_PENDING,
        })
    return out


def stats(bus) -> dict:
    """Aggregate stats: pending / succeeded / failed counts + last_attempt."""
    rows = bus.conn.execute(
        """
        SELECT status, COUNT(*), MAX(last_attempt_at)
        FROM cascade_state
        GROUP BY status
        """,
    ).fetchall()
    out = {
        'pending': 0,
        'succeeded': 0,
        'failed': 0,
        'last_attempt_at': None,
    }
    for status, count, last_at in rows:
        if status in out:
            out[status] = count
        if last_at and (out['last_attempt_at'] is None or last_at > out['last_attempt_at']):
            out['last_attempt_at'] = last_at
    return out


def replay_one(bus, row_id: int, max_attempts: int = 5) -> dict:
    """Replay a single cascade row. Returns {ok, error, attempt_count, status}.

    Idempotent: on success the row's status flips to 'succeeded'; on failure
    attempt_count increments and last_error updates. If attempt_count >
    max_attempts the row goes to 'failed' (kept for post-mortem).
    """
    # Lazy import to avoid circular: bus/cascade.py is imported by bus/store.py
    # which is the package's main entry. nest imports happen at replay time.
    from ..nest import astor_nest

    row = bus.conn.execute(
        "SELECT id, fact_id, operation, tier, user_id, payload, attempt_count "
        "FROM cascade_state WHERE id = ? AND status = ?",
        (int(row_id), STATUS_PENDING),
    ).fetchone()
    if row is None:
        return {'ok': False, 'error': 'row_not_pending', 'row_id': int(row_id)}

    queue_id, fact_id, operation, tier, user_id, payload_json, attempts = row
    attempts = int(attempts or 0)
    payload = {}
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except Exception:
        payload = {}

    try:
        if operation == 'embed_insert':
            content = payload.get('content', '')
            if not content:
                raise ValueError('cascade payload missing content for embed_insert')
            nest = astor_nest(tier=tier, user_id=user_id)
            nest.store(int(fact_id), content)
        elif operation == 'lex_index':
            from ..nest.lex_index import astor_lex
            content = payload.get('content', '')
            if not content:
                raise ValueError('cascade payload missing content for lex_index')
            lex = astor_lex(tier=tier, user_id=user_id)
            lex.index_fact(int(fact_id), content)
        elif operation == 'provenance_link':
            # Reserved for future use — when promote_candidate auto-link fails.
            raise NotImplementedError('provenance_link cascade not yet wired')
        else:
            raise ValueError(f"unknown cascade operation {operation!r}")

        # Success: mark succeeded, keep row for post-mortem visibility.
        bus.conn.execute(
            "UPDATE cascade_state SET status = ?, last_attempt_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), attempt_count = attempt_count + 1, "
            "last_error = NULL WHERE id = ?",
            (STATUS_SUCCEEDED, int(queue_id)),
        )
        bus.conn.commit()
        return {'ok': True, 'row_id': int(queue_id), 'fact_id': int(fact_id),
                'operation': operation, 'status': STATUS_SUCCEEDED,
                'attempt_count': attempts + 1}

    except Exception as e:
        attempts += 1
        new_status = STATUS_FAILED if attempts >= max_attempts else STATUS_PENDING
        bus.conn.execute(
            "UPDATE cascade_state SET status = ?, last_attempt_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), attempt_count = ?, last_error = ? "
            "WHERE id = ?",
            (new_status, attempts, str(e)[:500], int(queue_id)),
        )
        bus.conn.commit()
        return {'ok': False, 'row_id': int(queue_id), 'fact_id': int(fact_id),
                'operation': operation, 'status': new_status,
                'attempt_count': attempts, 'error': str(e)[:500]}


def replay_pending(bus, limit: int = 100, max_attempts: int = 5) -> dict:
    """Replay up to `limit` pending rows. Returns summary counts."""
    rows = list_pending(bus, limit=limit)
    succeeded = 0
    failed = 0
    still_pending = 0
    results = []
    for r in rows:
        out = replay_one(bus, r['id'], max_attempts=max_attempts)
        results.append(out)
        if out['ok']:
            succeeded += 1
        elif out.get('status') == STATUS_FAILED:
            failed += 1
        else:
            still_pending += 1
    return {
        'processed': len(rows),
        'succeeded': succeeded,
        'failed': failed,
        'still_pending': still_pending,
        'results': results,
    }


def purge(bus, status: str = 'succeeded', older_than_days: int = 7) -> int:
    """Delete cascade rows with given status older than N days. Returns deleted count."""
    cur = bus.conn.execute(
        "DELETE FROM cascade_state WHERE status = ? "
        "AND enqueued_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ? || ' days')",
        (status, f'-{int(older_than_days)}'),
    )
    bus.conn.commit()
    return cur.rowcount


__all__ = [
    'STATUS_PENDING', 'STATUS_SUCCEEDED', 'STATUS_FAILED',
    'enqueue', 'list_pending', 'stats', 'replay_one', 'replay_pending', 'purge',
]
