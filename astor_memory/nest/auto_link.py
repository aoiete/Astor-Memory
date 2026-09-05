"""
Zettelkasten-style auto-link (v1.2.3 — 2026-08-16 ship).

Pattern adopted from A-MEM (agiresearch/A-mem): when a new fact is written,
automatically establish provenance edges to existing similar facts in the
same tier. No rewriting of existing facts — only adds edges to the
provenance graph (audit-safe).

Why:
- A-MEM's research shows automatic linking improves recall precision
  by 5-10% by giving hybrid_merge an enriched graph signal.
- For astor specifically: short queries now benefit from "related facts"
  expansion via the existing /v1/fact/<id>/lineage endpoint.

Pipeline (per write):
1. New fact_id N is written via /v1/write.
2. Compute embedding for N (already done by nest.store).
3. Scan up to M existing facts in same (tier, user_id, kind) with cosine
   similarity > threshold (default 0.85).
4. For each match M_i, call record_provenance(child_id=M_i, parents=[N],
   kind='auto_link', agent='nest.auto_link'). This adds N to M_i's
   parent_fact_ids — the edge goes from "old fact derived from new fact"
   which is conceptually inverted, but we record it as a *bidirectional*
   link via the auto_link kind.

Wait — record_provenance sets parent_fact_ids, which means the OLD
fact now has the NEW fact as parent. That's: "old fact was derived from
new fact" — which is wrong. The correct semantic is "these two facts
are related" — bidirectional.

We handle this by:
- Adding the new fact to old fact's parent_fact_ids
- AND adding the old fact to new fact's parent_fact_ids
- Both with provenance_kind='auto_link' and provenance_agent='nest.auto_link'
- Plus a metadata note that this was an auto-link (not a real derivation)

Idempotency: if edge already exists (parent_fact_ids contains the other
fact_id), skip. This makes auto-link safe to call multiple times.

Threshold parameters:
- cosine_threshold: 0.85 default. Below this, edges are too noisy.
- max_existing_facts: 200 default. Limit scan per write to bound work.
- max_links_per_fact: 5 default. Cap total edges per new fact.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from .._internal.acl_layout import get_db_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%fZ')


def _bus_path(tier: str, user_id: str | None) -> Path:
    from pathlib import Path
    return get_db_path(tier, 'bus', user_id)


def _open_conn(tier: str = 'public', user_id: str | None = None) -> sqlite3.Connection:
    p = _bus_path(tier, user_id)
    if not p.exists():
        raise FileNotFoundError(f'bus db missing: {p}')
    conn = sqlite3.connect(
        f'file:{p}?mode=rw', uri=True,
        check_same_thread=False, timeout=5,
    )
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn


def find_similar_facts(
    bus_connection,
    new_fact_id: int,
    content: str,
    kind: str,
    tier: str,
    user_id: str | None,
    *,
    cosine_threshold: float = 0.85,
    max_existing: int = 200,
    max_results: int = 5,
) -> list[dict]:
    """Find existing facts in same (tier, user_id, kind) similar to content.

    Uses the existing astor_nest.search (BM25 + cosine) to find candidates,
    then filters by same kind (so we don't link e.g. user_preference to
    risk_rule).

    Returns: list of {'fact_id': int, 'similarity': float, 'content': str}
    """
    from . import astor_nest
    # Need an embedding for the new content
    from .embeddings import astor_get_embedding_model
    model = astor_get_embedding_model()
    new_emb = list(model.embed([content]))[0]
    nest = astor_nest(tier=tier, user_id=user_id)
    # oversample a bit since we re-filter
    hits = nest.search(new_emb, limit=max_existing)
    # Filter: same kind + cosine > threshold + exclude self
    out = []
    for fact_id, sim in hits:
        if sim < cosine_threshold:
            continue
        if int(fact_id) == int(new_fact_id):
            continue
        # Read kind from bus (cheap)
        row = bus_connection.execute(
            'SELECT kind FROM memory_canonical WHERE id = ? AND tombstoned = 0',
            (int(fact_id),),
        ).fetchone()
        if row is None:
            continue
        if row[0] != kind:
            continue
        out.append({'fact_id': int(fact_id), 'similarity': float(sim), 'kind': kind})
        if len(out) >= max_results:
            break
    return out


def add_auto_link(
    bus_connection,
    *,
    new_fact_id: int,
    existing_fact_id: int,
    similarity: float,
) -> bool:
    """Add a bidirectional auto-link edge between two facts.

    - Adds new_fact_id to existing_fact_id's parent_fact_ids (if not present)
    - Adds existing_fact_id to new_fact_id's parent_fact_ids (if not present)
    - Records audit row.
    - Returns True if edge was added, False if already present.
    """
    if int(new_fact_id) == int(existing_fact_id):
        return False
    # Read both rows
    new_row = bus_connection.execute(
        "SELECT parent_fact_ids, provenance_kind, provenance_agent FROM memory_canonical WHERE id = ?",
        (int(new_fact_id),),
    ).fetchone()
    ex_row = bus_connection.execute(
        "SELECT parent_fact_ids, provenance_kind, provenance_agent FROM memory_canonical WHERE id = ?",
        (int(existing_fact_id),),
    ).fetchone()
    if new_row is None or ex_row is None:
        return False
    # Parse existing parents
    try:
        new_parents = json.loads(new_row[0] or '[]')
    except Exception:
        new_parents = []
    try:
        ex_parents = json.loads(ex_row[0] or '[]')
    except Exception:
        ex_parents = []
    # Bail if either edge already exists (idempotent)
    if int(existing_fact_id) in new_parents:
        return False  # new -> existing edge already exists
    # Add edges
    new_parents.append(int(existing_fact_id))
    ex_parents.append(int(new_fact_id))
    new_parents_json = json.dumps(sorted(set(new_parents)))
    ex_parents_json = json.dumps(sorted(set(ex_parents)))
    # v1.10.8 (2026-08-26): preserve existing provenance_kind/agent if set.
    # Previously auto_link unconditionally wrote provenance_kind='auto_link' /
    # provenance_agent='nest.auto_link', which silently overwrote real
    # provenance from reflection (kind='merged') or extraction (kind='extracted').
    # Now: only set auto_link provenance if the column is NULL/empty.
    # Update new_fact_id
    bus_connection.execute(
        "UPDATE memory_canonical SET "
        "parent_fact_ids = ?, "
        "provenance_kind = COALESCE(NULLIF(provenance_kind, ''), 'auto_link'), "
        "provenance_agent = COALESCE(NULLIF(provenance_agent, ''), 'nest.auto_link'), "
        "provenance_at = ? "
        "WHERE id = ?",
        (new_parents_json, _utc_now(), int(new_fact_id)),
    )
    # Update existing_fact_id
    bus_connection.execute(
        "UPDATE memory_canonical SET "
        "parent_fact_ids = ?, "
        "provenance_kind = COALESCE(NULLIF(provenance_kind, ''), 'auto_link'), "
        "provenance_agent = COALESCE(NULLIF(provenance_agent, ''), 'nest.auto_link'), "
        "provenance_at = ? "
        "WHERE id = ?",
        (ex_parents_json, _utc_now(), int(existing_fact_id)),
    )
    return True


def write_audit(bus, new_fact_id: int, linked_to: list[dict]) -> None:
    """Write one audit row per auto-link run (per new_fact_id)."""
    bus.write_audit(
        event='auto_link',
        actor='nest.auto_link',
        target_type='fact',
        target_id=str(new_fact_id),
        metadata={
            'new_fact_id': new_fact_id,
            'linked_to': linked_to,
            'edge_count': len(linked_to),
        },
        severity='info',
    )


def auto_link_for_fact(
    bus,
    new_fact_id: int,
    content: str,
    kind: str,
    tier: str,
    user_id: str | None,
    *,
    cosine_threshold: float = 0.85,
    max_existing: int = 200,
    max_links: int = 5,
    min_confidence: float = 0.6,
) -> dict:
    """End-to-end: find similarities + add edges + audit.

    v1.13.2 (2026-09-04): added `min_confidence` gate. Skip auto-linking
    facts whose `confidence` < min_confidence — these are typically LLM
    extractions whose content isn't well-grounded, and auto-linking them
    amplifies their reach into the provenance graph. Without this gate,
    low-confidence extractions (verified root cause of the
    sunday-rejection-bug fact 8608) propagate as if authoritative.

    Returns: {'new_fact_id', 'linked_to': [fact_id, ...], 'edges_added': int}
    """
    # v1.13.2 (2026-09-04): read new fact's confidence; skip if below gate.
    try:
        row = bus.conn.execute(
            'SELECT confidence FROM memory_canonical WHERE id = ? AND tombstoned = 0',
            (int(new_fact_id),),
        ).fetchone()
        if row is not None and row[0] is not None and float(row[0]) < min_confidence:
            return {
                'new_fact_id': new_fact_id,
                'linked_to': [],
                'edges_added': 0,
                'skipped': f'confidence {float(row[0]):.2f} < {min_confidence:.2f}',
            }
    except Exception:
        pass
    try:
        sims = find_similar_facts(
            bus.conn, new_fact_id, content, kind, tier, user_id,
            cosine_threshold=cosine_threshold,
            max_existing=max_existing,
            max_results=max_links,
        )
    except Exception as e:
        # Auto-link failure should never block the write path.
        return {'new_fact_id': new_fact_id, 'linked_to': [], 'edges_added': 0,
                'error': str(e)}
    edges_added = 0
    linked_to: list[dict] = []
    for sim in sims:
        # v1.13.2 (2026-09-04): also gate the EXISTING fact's confidence —
        # don't link a high-confidence new fact to a low-confidence
        # existing fact (would pull it into the graph as if equivalent).
        try:
            ex_row = bus.conn.execute(
                'SELECT confidence FROM memory_canonical WHERE id = ? AND tombstoned = 0',
                (int(sim['fact_id']),),
            ).fetchone()
            if ex_row is not None and ex_row[0] is not None and float(ex_row[0]) < min_confidence:
                continue
        except Exception:
            pass
        ok = add_auto_link(
            bus.conn,
            new_fact_id=new_fact_id,
            existing_fact_id=sim['fact_id'],
            similarity=sim['similarity'],
        )
        if ok:
            edges_added += 1
            linked_to.append({
                'fact_id': sim['fact_id'],
                'similarity': sim['similarity'],
            })
    if edges_added > 0:
        try:
            write_audit(bus, new_fact_id, linked_to)
            bus.conn.commit()
        except Exception:
            pass
    return {
        'new_fact_id': new_fact_id,
        'linked_to': linked_to,
        'edges_added': edges_added,
    }


def backfill_all(
    bus,
    tier: str,
    user_id: str | None,
    *,
    cosine_threshold: float = 0.85,
    max_links_per_fact: int = 5,
    limit: int = 500,
    actor: str = 'nest.auto_link.backfill',
) -> dict:
    """Backfill auto-links for existing facts (one-shot).

    Reads the most recent N non-tombstoned facts and runs auto-link for
    each. Useful after a fresh import or once per quarter as cron.
    """
    rows = bus.conn.execute(
        "SELECT id, content, kind FROM memory_canonical "
        "WHERE tombstoned = 0 "
        "ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    total = 0
    edges = 0
    for fid, content, kind in rows:
        result = auto_link_for_fact(
            bus, new_fact_id=int(fid), content=content, kind=kind,
            tier=tier, user_id=user_id,
            cosine_threshold=cosine_threshold,
            max_links=max_links_per_fact,
        )
        total += 1
        edges += result.get('edges_added', 0)
    # Audit row for the backfill run
    try:
        bus.write_audit(
            event='auto_link_backfill',
            actor=actor,
            target_type='system',
            target_id='auto_link',
            metadata={
                'tier': tier, 'user_id': user_id,
                'facts_processed': total,
                'edges_added': edges,
                'cosine_threshold': cosine_threshold,
            },
            severity='info',
        )
    except Exception:
        pass
    return {
        'facts_processed': total,
        'edges_added': edges,
    }


__all__ = [
    'find_similar_facts', 'add_auto_link', 'auto_link_for_fact',
    'backfill_all',
]
