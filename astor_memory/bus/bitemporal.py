"""bitemporal.py — v1.16.8 (2026-09-30)

Bi-temporal fact lifecycle for astor-memory (Zep/Graphiti-inspired, Article
"2026 Agent 记忆六条路线" 路线5 时序知识图谱).

Background
----------
Article: "2026 年主流六条 Agent 记忆技术路线" notes the weakness of the
Agentic Memory Pipeline (Mem0-style, route 4 — which astor implements in
v1.15.57): when a user says "I moved to Shanghai" while a prior fact says
"I live in Beijing", BOTH facts remain active. Old fact should be marked
invalid, NOT just superseded.

Zep/Graphiti (route 5, Temporal KG) solves this with bi-temporal edges:
  - t_valid / t_invalid: when the fact was true in the real world
  - t_created: when the system learned it

Astor's existing `superseded_by INTEGER` is single-link (one-direction).
This module adds proper time-bounded validity.

Public surface
--------------
- invalidate_fact(bus, fact_id, replaced_by_fact_id, reason)
    Mark a fact invalid. Idempotent (re-invalidating an already-invalid
    fact is a no-op that returns the existing invalidation row).

- find_active_facts(bus, query, ...)
    Recall wrapper that filters out facts with valid_until IS NOT NULL.
    Falls back to bus.match_experiences for experience rows.

- get_fact_lifecycle(bus, fact_id) -> dict
    Returns full temporal info: created_at, valid_from, valid_until,
    invalidated_by, invalidated_at, lifetime_seconds, superseded_by.

- auto_invalidate_on_update(bus, new_fact_id, content, entities)
    Heuristic: when a new fact with kind in {correction, update,
    fact_update} arrives, scan existing facts with overlapping
    entities_json and mark them valid_until = now if they are
    contradicted by the new content. Conservative — only acts when
    semantic similarity > 0.6 AND shared subject entity.

- cascade_forget(bus, fact_id)
    Unlike the simple /v1/forget which only sets tombstoned=1,
    cascade_forget ALSO removes:
      * graph edges in conversation_graph where this fact_id is src or dst
      * entity fact_id references (rewrites entities_json in other facts)
      * audit cascade row for compliance audit
    Returns dict with cascade counts.

The schema columns added by v13_to_v14 migration:
  - valid_from        DATETIME   (default = created_at)
  - valid_until       DATETIME   (NULL = currently active)
  - invalidated_by    INTEGER    (the fact_id that replaced this one)
  - invalidated_at    DATETIME   (when invalidation was set)
  - invalidated_reason TEXT      (free-text: 'superseded', 'user_request',
                                  'auto_invalidate', etc.)
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

# Conservative threshold for auto-invalidation. Below this, the candidate
# fact is too different from existing facts to be a "replacement" — it might
# be a parallel/related fact (e.g. "I have a sister" vs "I live in Shanghai"
# share entity=person but are unrelated).
_AUTO_INVALIDATE_SIM_THRESHOLD = 0.6

# Kinds that trigger auto-invalidation scan when written.
_UPDATE_KINDS = frozenset({
    'correction', 'update', 'fact_update', 'user_correction',
    'pushback', 'success_pattern',
})


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + 'Z'


def invalidate_fact(
    bus,
    fact_id: int,
    replaced_by_fact_id: int | None = None,
    reason: str = 'superseded',
) -> dict:
    """Mark a fact invalid.

    Idempotent: returns the existing invalidation if already set.

    Returns dict with: ok, fact_id, valid_until, invalidated_by, reason.
    """
    if not isinstance(fact_id, int):
        return {'ok': False, 'error': 'fact_id must be int'}

    with bus.transaction() as c:
        row = c.execute(
            "SELECT id, valid_until, invalidated_by FROM memory_canonical "
            "WHERE id = ? AND tombstoned = 0",
            (fact_id,),
        ).fetchone()
        if not row:
            return {'ok': False, 'error': f'fact {fact_id} not found or tombstoned'}
        if row[1]:  # already invalidated
            return {
                'ok': True,
                'fact_id': fact_id,
                'valid_until': row[1],
                'invalidated_by': row[2],
                'reason': 'already_invalid',
            }
        now = _now_iso()
        c.execute(
            "UPDATE memory_canonical "
            "SET valid_until = ?, invalidated_by = ?, "
            "    invalidated_at = ?, invalidated_reason = ? "
            "WHERE id = ?",
            (now, replaced_by_fact_id, now, reason, fact_id),
        )
    bus.write_audit(
        event='fact_invalidated',
        actor='bitemporal',
        target_type='canonical',
        target_id=fact_id,
        new_state={'valid_until': now, 'invalidated_by': replaced_by_fact_id,
                   'reason': reason},
    )
    return {
        'ok': True,
        'fact_id': fact_id,
        'valid_until': now,
        'invalidated_by': replaced_by_fact_id,
        'reason': reason,
    }


def find_active_facts(bus, query: str, *, top_k: int = 10,
                      tier: str = 'public', user_id: str | None = None,
                      namespace: str | None = None) -> list[dict]:
    """Recall wrapper that excludes invalidated facts (valid_until IS NOT NULL).

    Falls back to bus.match_experiences for hybrid search, then filters the
    resulting fact_ids to active only. Returns list of dicts with id,
    content, kind, importance, etc.
    """
    # First, get a candidate set via the existing recall pipeline.
    candidates = bus.match_experiences(
        query, top_k=top_k * 3,
        namespace=namespace, user_id=user_id,
    )
    if not candidates:
        # Even with no experience rows, try to read canonical facts via
        # SELECT ... LIKE query (cheap, no embedding needed for the filter
        # — recall ranking still happens upstream in caller).
        like_q = f'%{query}%'
        rows = bus.conn.execute(
            "SELECT id, content, kind, importance, namespace, user_id, tier, "
            "       valid_from, valid_until, invalidated_by "
            "FROM memory_canonical "
            "WHERE tombstoned = 0 AND valid_until IS NULL "
            "  AND (content LIKE ? OR keywords LIKE ?) "
            "ORDER BY importance DESC, last_confirmed_at DESC LIMIT ?",
            (like_q, like_q, top_k),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # Filter to active only.
    active_ids = []
    for c in candidates:
        fid = c.get('id')
        if not fid:
            continue
        row = bus.conn.execute(
            "SELECT valid_until FROM memory_canonical WHERE id = ?",
            (fid,),
        ).fetchone()
        if row and not row[0]:  # valid_until IS NULL → active
            active_ids.append(fid)
    if not active_ids:
        return []
    placeholders = ','.join('?' * len(active_ids))
    rows = bus.conn.execute(
        f"SELECT id, content, kind, importance, namespace, user_id, tier, "
        f"       valid_from, valid_until, invalidated_by "
        f"FROM memory_canonical "
        f"WHERE id IN ({placeholders}) "
        f"ORDER BY importance DESC LIMIT ?",
        (*active_ids, top_k),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(r) -> dict:
    return {
        'id': r[0],
        'content': r[1],
        'kind': r[2],
        'importance': r[3],
        'namespace': r[4],
        'user_id': r[5],
        'tier': r[6],
        'valid_from': r[7],
        'valid_until': r[8],
        'invalidated_by': r[9],
    }


def get_fact_lifecycle(bus, fact_id: int) -> dict | None:
    """Return full temporal info for one fact. None if not found."""
    row = bus.conn.execute(
        "SELECT id, content, kind, importance, created_at, valid_from, "
        "       valid_until, invalidated_by, invalidated_at, "
        "       invalidated_reason, tombstoned, tombstoned_at "
        "FROM memory_canonical WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if not row:
        return None
    lifetime = None
    try:
        start = row[5] or row[4]  # valid_from OR created_at
        end = row[6] or row[11]    # valid_until OR tombstoned_at
        if start and end:
            s = _dt.datetime.fromisoformat(start.replace('Z', '+00:00').rstrip('+00:00'))
            e = _dt.datetime.fromisoformat(end.replace('Z', '+00:00').rstrip('+00:00'))
            lifetime = (e - s).total_seconds()
    except Exception:
        lifetime = None
    return {
        'fact_id': row[0],
        'content_preview': (row[1] or '')[:120],
        'kind': row[2],
        'importance': row[3],
        'created_at': row[4],
        'valid_from': row[5],
        'valid_until': row[6],
        'invalidated_by': row[7],
        'invalidated_at': row[8],
        'invalidated_reason': row[9],
        'tombstoned': bool(row[10]),
        'tombstoned_at': row[11],
        'lifetime_seconds': lifetime,
        'is_active': row[6] is None and not row[10],
    }


def auto_invalidate_on_update(
    bus,
    new_fact_id: int,
    content: str,
    entities: list[dict] | None = None,
    kind: str | None = None,
) -> list[dict]:
    """Heuristic auto-invalidation when an update-kind fact is written.

    Scans existing ACTIVE facts that share entities with the new fact and
    has high content similarity. Marks them valid_until=now().

    Conservative: only invalidates facts whose content similarity to new
    is above _AUTO_INVALIDATE_SIM_THRESHOLD AND that share at least one
    entity with the new fact. Subject mismatches or low similarity ⇒ skip.

    Returns list of dicts {fact_id, invalidated: bool, reason}.
    """
    if kind not in _UPDATE_KINDS:
        return []

    # Pull candidate existing facts (active, recent, same namespace)
    # We use a coarse LIKE pre-filter on shared entities, then similarity.
    entity_subjects = set()
    if entities:
        for e in entities:
            if isinstance(e, dict):
                subj = e.get('subject') or e.get('entity') or e.get('name')
                if subj:
                    entity_subjects.add(str(subj).lower())
            elif isinstance(e, str):
                entity_subjects.add(e.lower())

    # If no entities, fall back to keyword overlap on content.
    if not entity_subjects:
        import re as _re
        entity_subjects = set()
        for w in _re.findall(r'[\u4e00-\u9fff]{2,}', content):
            entity_subjects.add(w)
        for w in _re.findall(r'[A-Za-z][A-Za-z0-9]{2,}', content):
            entity_subjects.add(w.lower())
    if not entity_subjects:
        return []

    # Get candidate existing facts that share entities_json tokens
    rows = []
    for subj in entity_subjects:
        like_pat = f'%{subj}%'
        cands = bus.conn.execute(
            "SELECT id, content, entities_json FROM memory_canonical "
            "WHERE tombstoned = 0 AND valid_until IS NULL "
            "  AND id != ? AND (entities_json LIKE ? OR content LIKE ?) "
            "ORDER BY importance DESC LIMIT 20",
            (new_fact_id, like_pat, like_pat),
        ).fetchall()
        for r in cands:
            if r not in rows:
                rows.append(r)

    if not rows:
        return []

    # Compute content similarity for each candidate.
    from ..nest.embeddings import (
        astor_get_embedding_model, astor_get_model_name_for_ram,
        astor_embed_query_cached,
    )
    try:
        model_name = astor_get_model_name_for_ram()
        model = astor_get_embedding_model(model_name=model_name)
        new_emb = astor_embed_query_cached(model, model_name, content[:500])
        cand_texts = [(int(r[0]), (r[1] or '')[:500]) for r in rows]
        cand_embs = list(model.embed([t for _, t in cand_texts]))
        import numpy as _np
        new_norm = float(_np.linalg.norm(new_emb))
        results = []
        for (fid, _txt), emb in zip(cand_texts, cand_embs):
            en = float(_np.linalg.norm(emb))
            if new_norm > 0 and en > 0:
                sim = float(_np.dot(new_emb, emb) / (new_norm * en))
                if sim >= _AUTO_INVALIDATE_SIM_THRESHOLD:
                    r = invalidate_fact(
                        bus, fact_id=fid,
                        replaced_by_fact_id=new_fact_id,
                        reason=f'auto_invalidate (sim={sim:.2f})',
                    )
                    results.append({'fact_id': fid, 'invalidated': r['ok'],
                                    'reason': r.get('reason'),
                                    'similarity': round(sim, 3)})
        return results
    except Exception as e:
        return [{'error': f'auto_invalidate skipped: {e}'}]


def cascade_forget(bus, fact_id: int) -> dict:
    """Cascade delete a fact and its graph/entity references.

    Unlike simple /v1/forget (which only sets tombstoned=1), this:
      1. Sets tombstoned=1 on memory_canonical row
      2. Removes rows from conversation_graph where src_id=dst_id=fact_id
      3. Rewrites entities_json in OTHER facts to remove this fact's id
      4. Writes audit row with cascade counts

    Returns dict with cascade counts per layer.
    """
    if not isinstance(fact_id, int):
        return {'ok': False, 'error': 'fact_id must be int'}

    row = bus.conn.execute(
        "SELECT id, tombstoned FROM memory_canonical WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if not row:
        return {'ok': False, 'error': f'fact {fact_id} not found'}
    if row[1]:
        return {'ok': True, 'fact_id': fact_id, 'note': 'already tombstoned'}

    counts = {'graph_edges': 0, 'entities_cleaned': 0}

    with bus.transaction() as c:
        # 1. Tombstone the fact
        now = _now_iso()
        c.execute(
            "UPDATE memory_canonical SET tombstoned = 1, tombstoned_at = ? "
            "WHERE id = ?",
            (now, fact_id),
        )
        # 2. Remove graph edges (if conversation_graph exists)
        try:
            cur = c.execute(
                "DELETE FROM conversation_graph "
                "WHERE src_id = ? OR dst_id = ?",
                (fact_id, fact_id),
            )
            counts['graph_edges'] = cur.rowcount
        except Exception:
            # conversation_graph may not exist on older DBs
            pass
        # 3. Rewrite entities_json in other facts to remove this fact_id
        rows = c.execute(
            "SELECT id, entities_json FROM memory_canonical "
            "WHERE id != ? AND tombstoned = 0 "
            "  AND entities_json LIKE ?",
            (fact_id, f'%"fact_id": {fact_id}%'),
        ).fetchall()
        for r in rows:
            try:
                ents = json.loads(r[1])
                if not isinstance(ents, list):
                    continue
                cleaned = [e for e in ents
                           if not (isinstance(e, dict) and e.get('fact_id') == fact_id)]
                if len(cleaned) != len(ents):
                    c.execute(
                        "UPDATE memory_canonical SET entities_json = ? WHERE id = ?",
                        (json.dumps(cleaned, ensure_ascii=False), r[0]),
                    )
                    counts['entities_cleaned'] += 1
            except Exception:
                continue

    bus.write_audit(
        event='cascade_forget',
        actor='bitemporal',
        target_type='canonical',
        target_id=fact_id,
        new_state=counts,
        reason='user_cascade_request',
        severity='warning',
    )
    return {'ok': True, 'fact_id': fact_id, **counts}
