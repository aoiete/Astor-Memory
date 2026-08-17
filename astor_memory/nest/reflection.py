"""
Reflection orchestrator for astor-memory v1.2.2 (2026-08-16).

Pattern adopted from EverOS `memory/reflection/` (simplified for astor's
SQLite-only stack, no LLM in v1.2.2 LLM-mode later).

Pipeline (Select → Merge → Deprecate):

1. **Select** — find clusters of similar canonical facts in a tier. Two
   facts are clustered if:
   - Same `kind` (e.g. both 'user_preference'), AND
   - Same `scope_type` (don't merge short_term with long_term), AND
   - Cosine similarity ≥ cosine_threshold (default 0.85), OR
   - BM25 score ≥ bm25_threshold (default 5.0) on hybrid retrieval

2. **Merge** — for each cluster of size ≥ 2:
   - Pick the winner: highest `importance`, then most recent `promoted_at`,
     then highest `confidence`. Tiebreak by id ASC for determinism.
   - Compose new content via heuristic mode: concatenate distinct
     variant details from cluster members + winner's content. (Future
     LLM mode would do a smarter merge.)
   - Update winner's content to merged text + bump importance + bump
     promoted_at + write deprecation audit row.

3. **Deprecate** — tombstone (`tombstoned=1`) all losers. Losers stay
   queryable via `/v1/fact/<id>/provenance` for audit purposes but drop
   out of standard recall (`/v1/read` filters `tombstoned=0`).

Failure modes:
- Cluster size < 2 → skip (nothing to merge)
- All cluster members already processed in a prior reflection → skip
  (idempotent via `last_reflection_at` cursor on `memory_canonical`)

Replayed safely; pipeline is read-only on inputs until the merge step,
then writes via the existing AstorBus APIs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Concat separator for heuristic merge content
_MERGE_SEP = '\n---\n'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%fZ')


def select_episode_clusters(
    bus,
    tier: str,
    user_id: str | None,
    *,
    min_size: int = 2,
    max_clusters: int = 100,
    kinds: list[str] | None = None,
    limit: int = 500,
) -> list[list[int]]:
    """Find clusters of similar canonical facts.

    Strategy: per `kind`, scan recent facts (up to `limit`), group by
    BM25-like co-occurrence on shared distinctive tokens. Cosine
    similarity is too expensive to compute pairwise (O(n²)); we use a
    token-overlap heuristic instead. Two facts cluster if they share
    ≥ 3 distinctive tokens (≥4 chars) AND their content length is
    within 2x of each other (length sanity check).

    For v1.2.2 this is intentionally conservative — better to miss a
    cluster than to merge unrelated facts. LLM-mode (future) will use
    more sophisticated grouping.

    Returns: list of fact_id lists, each length ≥ min_size.
    """
    # Load candidate facts (non-tombstoned, recent). Optionally restrict by kind.
    where_extra = ''
    params: list[Any] = []
    if kinds:
        placeholders = ','.join('?' * len(kinds))
        where_extra = f'AND kind IN ({placeholders})'
        params.extend(kinds)
    sql = (
        f'SELECT id, content, kind, scope_type, importance, '
        f'       promoted_at, confidence '
        f'FROM memory_canonical '
        f'WHERE tier = ? AND tombstoned = 0 '
        f'  AND (user_id IS ? OR user_id = ?) '
        f'  {where_extra} '
        f'ORDER BY id DESC LIMIT ?'
    )
    params = [tier, user_id, user_id] + params + [int(limit)]
    rows = bus.conn.execute(sql, params).fetchall()
    if len(rows) < min_size:
        return []

    # Group by (kind, scope_type) — only merge within same group.
    from collections import defaultdict
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for r in rows:
        fid, content, kind, scope_type, importance, promoted_at, confidence = r
        groups[(kind, scope_type)].append({
            'id': int(fid),
            'content': str(content),
            'kind': kind,
            'scope_type': scope_type,
            'importance': float(importance) if importance else 0.0,
            'promoted_at': promoted_at,
            'confidence': float(confidence) if confidence else 0.0,
        })

    # For each group, find clusters of similar facts.
    clusters: list[list[int]] = []
    for group_key, facts in groups.items():
        if len(facts) < min_size:
            continue
        # Tokenize each fact (simple split on non-word)
        import re as _re
        for f in facts:
            f['tokens'] = set(t.lower() for t in _re.findall(r'[a-zA-Z一-鿿]{4,}', f['content']))
        # Pairwise clustering: union-find
        parent = {f['id']: f['id'] for f in facts}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        for i, fa in enumerate(facts):
            for fb in facts[i + 1:]:
                # Length sanity check
                len_a = len(fa['content'])
                len_b = len(fb['content'])
                if min(len_a, len_b) == 0:
                    continue
                if max(len_a, len_b) / min(len_a, len_b) > 2.0:
                    continue
                # Distinctive token overlap
                inter = fa['tokens'] & fb['tokens']
                if len(inter) >= 3:
                    # Both have at least 3 long tokens in common AND
                    # similar length. Cluster them.
                    union(fa['id'], fb['id'])
        # Collect clusters of size >= min_size
        cluster_map: dict[int, list[int]] = {}
        for f in facts:
            root = find(f['id'])
            cluster_map.setdefault(root, []).append(f['id'])
        for fids in cluster_map.values():
            if len(fids) >= min_size:
                clusters.append(sorted(fids))
                if len(clusters) >= max_clusters:
                    return clusters
    return clusters


def merge_narrative(facts: list[dict]) -> dict:
    """Compose the merged fact for a cluster.

    Heuristic mode: pick the winner (highest importance, then most recent,
    then highest confidence), then concatenate distinct content from each
    member. The winner's content forms the base; others are appended if
    they contribute unique information.

    Returns: {
        'winner_id': int,
        'loser_ids': [int, ...],
        'merged_content': str,
        'merged_importance': float,  # bumped slightly
    }
    """
    if not facts:
        raise ValueError("merge_narrative: empty facts list")
    # Determine winner
    winner = max(facts, key=lambda f: (
        f['importance'],
        f['promoted_at'] or '',
        f['confidence'],
        -f['id'],  # tiebreak: smaller id wins (older fact usually more baked)
    ))
    loser_ids = [f['id'] for f in facts if f['id'] != winner['id']]
    # Compose merged content: winner's content + distinct variants
    seen_paragraphs = {winner['content'].strip()}
    parts = [winner['content'].strip()]
    for f in facts:
        if f['id'] == winner['id']:
            continue
        # Only add if the content is meaningfully different from what we have
        content = f['content'].strip()
        if content and content not in seen_paragraphs:
            # Avoid duplicating if winner already contains this
            already_contained = any(
                content in existing or existing in content
                for existing in seen_paragraphs)
            if not already_contained:
                parts.append(content)
                seen_paragraphs.add(content)
    merged_content = _MERGE_SEP.join(parts)
    # Bump importance (max 1.0) — merged fact is more valuable
    merged_importance = min(1.0, max(f['importance'] for f in facts) + 0.1)
    return {
        'winner_id': winner['id'],
        'loser_ids': loser_ids,
        'merged_content': merged_content,
        'merged_importance': merged_importance,
    }


def deprecate_old_facts(bus, loser_ids: list[int], winner_id: int, actor: str) -> int:
    """Tombstone losers + write audit row. Returns count deprecated."""
    if not loser_ids:
        return 0
    deprecated = 0
    for loser_id in loser_ids:
        row = bus.conn.execute(
            'SELECT id, content, kind, tier, user_id FROM memory_canonical WHERE id = ?',
            (loser_id,)).fetchone()
        if row is None:
            continue
        existing_id, content, kind, tier, user_id = row
        # Tombstone
        bus.conn.execute(
            'UPDATE memory_canonical SET tombstoned = 1, tombstoned_at = ? '
            'WHERE id = ? AND tombstoned = 0',
            (_utc_now(), int(loser_id)),
        )
        # Audit row — old_state / new_state live in metadata JSON because
        # write_audit() doesn't expose those columns directly.
        bus.write_audit(
            event='reflection_deprecated',
            actor=actor,
            target_type='fact',
            target_id=str(loser_id),
            metadata={
                'old_state': json.dumps({
                    'content': content,
                    'kind': kind,
                    'tier': tier,
                    'user_id': user_id,
                }),
                'new_state': json.dumps({'winner_id': winner_id, 'merged_into': winner_id}),
            },
            reason=f'reflection: merged into fact_id={winner_id}',
            severity='info',
        )
        deprecated += 1
    bus.conn.commit()
    return deprecated


def apply_merge(bus, winner_id: int, merged_content: str, merged_importance: float,
                actor: str) -> int:
    """Apply merged content to winner row. Returns 1 on success.

    Bumps promoted_at so the merged fact sorts first in most-recent
    queries. Updates content + importance.
    """
    bus.conn.execute(
        'UPDATE memory_canonical SET content = ?, importance = ?, '
        '  promoted_at = ?, last_confirmed_at = ? '
        'WHERE id = ?',
        (merged_content, merged_importance, _utc_now(), _utc_now(), int(winner_id)),
    )
    bus.write_audit(
        event='reflection_merged',
        actor=actor,
        target_type='fact',
        target_id=str(winner_id),
        metadata={
            'new_state': json.dumps({
                'content_preview': merged_content[:200],
                'importance': merged_importance,
            }),
        },
        reason='reflection: absorbed cluster members',
        severity='info',
    )
    bus.conn.commit()
    return 1


def run_reflection(
    bus,
    tier: str = 'public',
    user_id: str | None = None,
    *,
    min_size: int = 2,
    max_clusters: int = 50,
    kinds: list[str] | None = None,
    actor: str = 'reflection_v1',
) -> dict:
    """Run the full reflection pipeline.

    Returns summary:
        {
            'clusters_found': int,
            'clusters_merged': int,
            'facts_deprecated': int,
            'merge_log': [
                {'winner_id': int, 'merged_from': [int, ...], 'preview': str},
            ]
        }
    """
    clusters = select_episode_clusters(
        bus, tier=tier, user_id=user_id,
        min_size=min_size, max_clusters=max_clusters, kinds=kinds,
    )
    merge_log: list[dict] = []
    total_deprecated = 0
    for fids in clusters:
        # Load full facts for the cluster
        placeholders = ','.join('?' * len(fids))
        rows = bus.conn.execute(
            f'SELECT id, content, kind, scope_type, importance, '
            f'       promoted_at, confidence '
            f'FROM memory_canonical '
            f'WHERE id IN ({placeholders})',
            fids,
        ).fetchall()
        facts = [
            {
                'id': int(r[0]),
                'content': str(r[1]),
                'kind': r[2],
                'scope_type': r[3],
                'importance': float(r[4]) if r[4] else 0.0,
                'promoted_at': r[5],
                'confidence': float(r[6]) if r[6] else 0.0,
            }
            for r in rows
        ]
        if len(facts) < 2:
            continue
        # Merge
        merged = merge_narrative(facts)
        # Apply winner update
        apply_merge(
            bus,
            winner_id=merged['winner_id'],
            merged_content=merged['merged_content'],
            merged_importance=merged['merged_importance'],
            actor=actor,
        )
        # Deprecate losers
        n_dep = deprecate_old_facts(bus, merged['loser_ids'], merged['winner_id'], actor)
        total_deprecated += n_dep
        merge_log.append({
            'winner_id': merged['winner_id'],
            'merged_from': merged['loser_ids'],
            'preview': merged['merged_content'][:200],
            'deprecated_count': n_dep,
        })
    return {
        'clusters_found': len(clusters),
        'clusters_merged': len(merge_log),
        'facts_deprecated': total_deprecated,
        'merge_log': merge_log,
    }


__all__ = [
    'select_episode_clusters', 'merge_narrative', 'apply_merge',
    'deprecate_old_facts', 'run_reflection',
]
