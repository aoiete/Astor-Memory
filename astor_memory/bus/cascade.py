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
  pending → processing → succeeded   (embed succeeded on retry)
  pending → processing → failed      (embed still failing after retries;
                                      row kept for post-mortem; cleared by
                                      `am cascade purge`)
  processing → pending               (lease expired after 15 min without
                                      update; orphan row reclaimed by next
                                      replay pass)

v1.10.8 (2026-08-26):
  - Added `processing` state — formerly absent from the docstring even
    though the code used it. The README + audit needed catching up.
  - Bug fix: lease-recovery comparison was comparing ISO-format string
    against datetime() space-format string → same-day orphans never
    reclaimed. Fixed by wrapping last_attempt_at in datetime() before
    comparing. See list_pending() and replay_one() for the diff.
  - Added requeue() so failed rows can be resurrected after the
    underlying error recovers.
  - stats() now exposes `processing` count (was silently dropped).

Status string literals (NOT exported constants — module uses bare strings
rather than named constants; intentional to keep call sites terse):
  'pending' | 'processing' | 'succeeded' | 'failed'
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .store import AstorBus


# Status enum
STATUS_PENDING = 'pending'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'
STATUS_PROCESSING = 'processing'
# A crashed worker must not strand a row forever. Reclaim only rows whose
# claim has been quiet for this long; active embedding work remains protected.
PROCESSING_LEASE_MINUTES = 15


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
               enqueued_at, attempt_count, last_error, status
        FROM cascade_state
        WHERE status = ?
           OR (status = ? AND last_attempt_at <
               datetime('now', ?))
        ORDER BY enqueued_at ASC
        LIMIT ?
        """,
        (STATUS_PENDING, STATUS_PROCESSING,
         f'-{PROCESSING_LEASE_MINUTES} minutes', int(limit)),
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
            'status': r[9],
        })
    return out


def stats(bus) -> dict:
    """Aggregate stats: pending / processing / succeeded / failed + last_attempt.

    v1.10.8 (2026-08-26): include `processing` count. Previously the output
    dict only initialized pending/succeeded/failed, and the stats loop
    silently dropped `processing` rows via `if status in out` (the
    processing key wasn't in out). This meant any row currently being
    worked on by a replay process was invisible to the operational
    dashboard, hiding in-flight work.
    """
    rows = bus.conn.execute(
        """
        SELECT status, COUNT(*), MAX(last_attempt_at)
        FROM cascade_state
        GROUP BY status
        """,
    ).fetchall()
    out = {
        'pending': 0,
        'processing': 0,  # v1.10.8: was missing → silently dropped by `if status in out`
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

    # Claim the queue row atomically before doing slow embedding work. Without
    # this, two replay workers can both read the same pending row and duplicate
    # nest.store() before either one marks it succeeded.
    with bus.transaction() as c:
        # v1.10.8 (2026-08-26): same lease-recovery fix as in list_pending —
        # wrap last_attempt_at in datetime() to convert ISO format to SQLite
        # datetime before comparing (string 'T' > ' ' would otherwise block
        # same-day reclaim).
        claimed = c.execute(
            "UPDATE cascade_state SET status = ?, last_attempt_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ? AND (status = ? OR (status = ? AND datetime(last_attempt_at) < "
            "datetime('now', ?)))",
            (STATUS_PROCESSING, int(row_id), STATUS_PENDING, STATUS_PROCESSING,
             f'-{PROCESSING_LEASE_MINUTES} minutes'),
        ).rowcount
        if not claimed:
            return {'ok': False, 'error': 'row_not_pending', 'row_id': int(row_id)}

    row = bus.conn.execute(
        "SELECT id, fact_id, operation, tier, user_id, payload, attempt_count "
        "FROM cascade_state WHERE id = ? AND status = ?",
        (int(row_id), 'processing'),
    ).fetchone()
    if row is None:
        return {'ok': False, 'error': 'row_not_processing', 'row_id': int(row_id)}

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




def requeue(
    bus,
    row_id: int | None = None,
    *,
    all_failed: bool = False,
    max_attempts_threshold: int = 5,
) -> dict:
    """v1.10.8 (2026-08-26): reset `failed` rows back to `pending` so a recovered
    embedding service can retry them.

    Previously failed rows had no revival path: list_pending() only matched
    status='pending' OR (status='processing' AND lease expired), so a row
    that hit max_attempts stayed 'failed' forever even after the underlying
    error (e.g. embedding OOM, downstream service outage) was resolved.

    Args:
      bus: AstorBus handle.
      row_id: specific row to requeue. None + all_failed=True requeues every
        failed row whose attempt_count <= max_attempts_threshold.
      all_failed: requeue all eligible failed rows.
      max_attempts_threshold: only requeue rows whose previous attempt_count
        was at or below this number. Default 5 matches the existing replay
        retry budget.

    Returns:
      {'ok': bool, 'requeued': [int], 'reason': str}
    """
    if row_id is None and not all_failed:
        return {'ok': False, 'requeued': [], 'reason': 'specify row_id or all_failed=True'}
    requeued: list[int] = []
    try:
        if row_id is not None:
            row = bus.conn.execute(
                "SELECT id, status, attempt_count FROM cascade_state WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                return {'ok': False, 'requeued': [], 'reason': f'row {row_id} not found'}
            if row[1] != 'failed':
                return {'ok': False, 'requeued': [], 'reason': f'row {row_id} status={row[1]}; only failed rows can be requeued'}
            bus.conn.execute(
                "UPDATE cascade_state SET status = 'pending', attempt_count = 0, "
                "last_error = NULL, last_attempt_at = NULL "
                "WHERE id = ?",
                (int(row_id),),
            )
            requeued.append(int(row_id))
        else:
            # all_failed path
            rows = bus.conn.execute(
                "SELECT id FROM cascade_state WHERE status = 'failed' "
                "AND attempt_count <= ?",
                (int(max_attempts_threshold),),
            ).fetchall()
            for r in rows:
                bus.conn.execute(
                    "UPDATE cascade_state SET status = 'pending', attempt_count = 0, "
                    "last_error = NULL, last_attempt_at = NULL "
                    "WHERE id = ?",
                    (int(r[0]),),
                )
                requeued.append(int(r[0]))
        bus.conn.commit()
        return {'ok': True, 'requeued': requeued, 'reason': 'ok'}
    except Exception as e:
        return {'ok': False, 'requeued': requeued, 'reason': f'error: {e}'}


__all__ = [
    'STATUS_PENDING', 'STATUS_SUCCEEDED', 'STATUS_FAILED',
    'enqueue', 'list_pending', 'stats', 'replay_one', 'replay_pending', 'purge',
]
