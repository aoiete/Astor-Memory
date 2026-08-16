"""
Memory versioning — lightweight per-fact restore via audit_log (2026-08-16 opt6).

Strategy:
  Every mutating action (forget, merge, tombstone) is supposed to write
  the prior canonical-row state to audit_log.old_state (JSON). This
  module provides:

    restore_fact(fact_id, target_state='live')
        Re-insert a previously-deleted/tombstoned fact using its
        last audit_log.old_state snapshot. Idempotent: re-running
        restore on a fact that's already live is a no-op.

    list_versions(fact_id)
        Walk audit_log entries for a fact_id in time order. Each entry
        shows the prior state (old_state) that was captured before the
        mutation — so you can roll back step by step.

    daily_snapshot_stats(date_str)
        Count mutations on a given date (UTC) per (tier, event_type).

  These do NOT require schema changes — we lean on the existing
  audit_log table that we already populate.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .._internal.acl_layout import get_db_path, list_user_ids, Tier


def _bus_path(tier: str, user_id: str | None) -> Path:
    return get_db_path(tier, 'bus', user_id)


def list_versions(fact_id: int,
                  tier: str = 'public',
                  user_id: Optional[str] = None) -> list[dict]:
    """Walk audit_log entries for a given fact_id and reconstruct the
    state graph. Returns entries newest-first."""
    p = _bus_path(tier, user_id)
    if not p.exists():
        raise FileNotFoundError(f'bus db missing: {p}')
    conn = sqlite3.connect(
        f'file:{p}?mode=ro', uri=True,
        check_same_thread=False, timeout=5,
    )
    try:
        rows = conn.execute(
            "SELECT id, ts, event, actor, old_state, new_state, "
            "reason, metadata, severity "
            "FROM audit_log "
            "WHERE target_type = 'fact' AND target_id = ? "
            "ORDER BY id DESC",
            (str(fact_id),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        old_state = None
        new_state = None
        try:
            if r[4]:
                old_state = json.loads(r[4])
        except Exception:
            pass
        try:
            if r[5]:
                new_state = json.loads(r[5])
        except Exception:
            pass
        meta = None
        try:
            if r[7]:
                meta = json.loads(r[7])
        except Exception:
            pass
        out.append({
            'audit_id': r[0],
            'ts': r[1],
            'event': r[2],
            'actor': r[3],
            'old_state': old_state,
            'new_state': new_state,
            'reason': r[6],
            'metadata': meta,
            'severity': r[8],
        })
    return out


def restore_fact(
    fact_id: int,
    tier: str = 'public',
    user_id: Optional[str] = None,
    *,
    target_state: str = 'live',
    actor: str = 'restore_v1',
) -> dict:
    """Restore a fact from its most-recent audit_log.old_state.

    target_state:
      'live'    → re-insert into memory_canonical, remove tombstoned flag
      'preview' → return the row that would be restored, do NOT mutate
                  bus / nest / lex. Useful for safe inspection.
    """
    if target_state not in ('live', 'preview'):
        return {'error': f'invalid target_state {target_state!r}'}

    # 1) Find the most recent audit_log entry with old_state for this fact
    p = _bus_path(tier, user_id)
    if not p.exists():
        return {'error': f'bus db missing: {p}'}
    conn = sqlite3.connect(str(p), timeout=30)
    conn.execute('PRAGMA journal_mode = WAL')
    try:
        row = conn.execute(
            "SELECT id, ts, actor, old_state, reason FROM audit_log "
            "WHERE target_type = 'fact' AND target_id = ? "
            "AND old_state IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (str(fact_id),),
        ).fetchone()
        if row is None:
            return {'restored': [], 'reason': 'no audit_log.old_state found'}
        old_state_raw = row[3]
        try:
            old_state = json.loads(old_state_raw)
        except Exception as e:
            return {'error': f'old_state corrupt: {e}'}
        if not isinstance(old_state, dict) or 'columns' not in old_state:
            return {'error': 'old_state shape unexpected; need {columns: {...}}'}

        cols = old_state['columns']
        # 2) If we are in preview mode, return the row without mutating
        if target_state == 'preview':
            return {
                'preview': True,
                'fact_id': fact_id, 'tier': tier, 'user_id': user_id,
                'would_restore': cols,
                'source_audit_id': row[0],
                'source_ts': row[1],
                'source_actor': row[2],
                'source_reason': row[4],
            }
        # 3) Re-insert into memory_canonical
        # First check if the fact_id is already live (idempotent)
        existing = conn.execute(
            "SELECT id, tombstoned FROM memory_canonical WHERE id = ?",
            (int(fact_id),),
        ).fetchone()
        if existing is not None and existing[1] == 0:
            return {
                'restored': [],
                'reason': f'fact {fact_id} already live; no-op',
                'source_audit_id': row[0],
            }
        # Build the column list. Note: we don't preserve the original
        # AUTOINCREMENT id — if the row exists in another way, we may
        # want to update instead. For simplicity here: re-INSERT with the
        # original id when tombstoned, OR restore as a new row if hard-
        # deleted.
        try:
            fact_id_int = int(cols.get('id') or fact_id)
        except Exception:
            fact_id_int = int(fact_id)

        # Hard-deleted row case: row doesn't exist at all in memory_canonical.
        # We INSERT a new row using the captured state.
        if existing is None:
            # Restore row via INSERT — preserve original id
            try:
                # Make sure columns include all NOT NULL fields
                insert_cols = cols
                # Convert dict to ordered list for SQL
                col_names = list(insert_cols.keys())
                placeholders = ','.join(['?'] * len(col_names))
                values = [insert_cols[c] for c in col_names]
                # Override id to the new (or target) one
                if 'id' in col_names:
                    id_idx = col_names.index('id')
                    values[id_idx] = fact_id_int
                # Serialize tags/metadata back as JSON if string
                for i, c in enumerate(col_names):
                    if c in ('tags', 'metadata') and isinstance(values[i], str):
                        try:
                            json.loads(values[i])
                        except Exception:
                            values[i] = json.dumps(values[i])
                conn.execute(
                    f"INSERT OR REPLACE INTO memory_canonical ({','.join(col_names)}) VALUES ({placeholders})",
                    values,
                )
            except Exception as e:
                conn.rollback()
                return {'error': f're-INSERT failed: {e}'}
        else:
            # Tombstoned row case: clear tombstoned, restore everything else
            try:
                set_clauses = []
                set_values = []
                # Restore every column from old_state
                for c, v in cols.items():
                    if c == 'id':
                        continue
                    set_clauses.append(f'{c} = ?')
                    set_values.append(v)
                # Always clear tombstoned
                set_clauses.append('tombstoned = 0')
                set_values.append(fact_id_int)
                conn.execute(
                    f"UPDATE memory_canonical SET {','.join(set_clauses)} WHERE id = ?",
                    set_values,
                )
            except Exception as e:
                conn.rollback()
                return {'error': f'UPDATE failed: {e}'}

        # 4) Audit the restore
        try:
            conn.execute(
                "INSERT INTO audit_log(event, actor, target_type, "
                "target_id, reason, metadata, severity) "
                "VALUES (?,?,?,?,?,?,?)",
                ('restore', actor, 'fact', str(fact_id_int),
                 f'from audit_log audit_id={row[0]} (ts={row[1]})',
                 json.dumps({'restored_from_audit_id': int(row[0]),
                             'tier': tier, 'user_id': user_id},
                            ensure_ascii=False),
                 'info'),
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()

    # 5) Re-index into nest (best-effort) and lex (best-effort)
    nest_re = None
    lex_re = None
    try:
        from .. import astor_nest
        from .lex_index import astor_lex
        # Get the restored content
        conn = sqlite3.connect(str(p), timeout=5)
        try:
            r = conn.execute(
                "SELECT content FROM memory_canonical WHERE id=?",
                (fact_id_int,),
            ).fetchone()
        finally:
            conn.close()
        if r:
            content = r[0]
            try:
                nest = astor_nest(tier=tier, user_id=user_id)
                # Embed and store
                from .embeddings import astor_get_embedding_model
                model = astor_get_embedding_model()
                emb = list(model.embed([content]))[0]
                emb_blob = struct_pack_floats(emb)
                nest.conn.execute(
                    "INSERT OR REPLACE INTO embeddings(fact_id, embedding, model_name) VALUES (?,?,?)",
                    (fact_id_int, emb_blob,
                     astor_get_model_name_for_ram() if False else 'BAAI/bge-small-en-v1.5'),
                )
                nest.conn.commit()
                nest_re = 'ok'
            except Exception as e:
                nest_re = f'failed: {e}'
            try:
                lex = astor_lex(tier=tier, user_id=user_id)
                lex.index_fact(fact_id_int, content)
                lex_re = 'ok'
            except Exception as e:
                lex_re = f'failed: {e}'
    except Exception:
        pass

    return {
        'restored': [{
            'fact_id': fact_id_int, 'tier': tier, 'user_id': user_id,
            'content_preview': str(cols.get('content', ''))[:120],
        }],
        'source_audit_id': int(row[0]),
        'source_ts': row[1],
        'nest_reindex': nest_re,
        'lex_reindex': lex_re,
        'actor': actor,
    }


def daily_snapshot_stats(
    date_str: str,
    tier: str = 'public',
    user_id: Optional[str] = None,
) -> dict:
    """Count audit_log mutations on a given UTC date.

    date_str: 'YYYY-MM-DD' (UTC day)
    Returns counts per event_type + sample of mutations.
    """
    p = _bus_path(tier, user_id)
    if not p.exists():
        return {'error': f'bus db missing: {p}'}
    conn = sqlite3.connect(
        f'file:{p}?mode=ro', uri=True,
        check_same_thread=False, timeout=5,
    )
    try:
        rows = conn.execute(
            "SELECT event, severity, COUNT(*) FROM audit_log "
            "WHERE ts LIKE ? "
            "GROUP BY event, severity",
            (f'{date_str}%',),
        ).fetchall()
        sample = conn.execute(
            "SELECT id, ts, event, actor, target_id, severity "
            "FROM audit_log WHERE ts LIKE ? ORDER BY id DESC LIMIT 30",
            (f'{date_str}%',),
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, dict] = {}
    for ev, sev, n in rows:
        counts.setdefault(ev, {})[sev] = n
    return {
        'date': date_str, 'tier': tier, 'user_id': user_id,
        'counts_by_event_severity': counts,
        'sample': [
            {'audit_id': r[0], 'ts': r[1], 'event': r[2],
             'actor': r[3], 'target_id': r[4], 'severity': r[5]}
            for r in sample
        ],
    }


# Helpers
def struct_pack_floats(arr) -> bytes:
    import struct
    return struct.pack(f'{len(arr)}f', *arr)
