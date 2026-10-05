"""
v1.16.68 Ship B #2 — chat_chunk cluster summaries (MemFit §摘要层).

TextTiling-style: group adjacent chat_chunk events into clusters when
the time-gap between them exceeds GAP_MINUTES (default 30). For each
cluster, optionally call an LLM to produce a 2-sentence / 200-token
summary. Stores provenance (member_event_ids) so recall can navigate
from cluster → original chunks.

Why this exists:
- MemFit 2026-10 paper § 摘要层: "摘要的角色是导航" — without
  summaries, multi-hop questions ("what did the user say about X
  across all of last week?") can't be answered in one recall.
- mem0-style summary-on-write keeps the chunk-count manageable.
- Provenance link (cluster.member_event_ids) means the original raw
  evidence is never lost.

Pipeline:
  1. Build clusters (group by time-gap)  — sync text overlap
  2. For clusters > 1 chunk + > 50 chars of prefix total: ask LLM
     for summary
  3. Embed the summary alongside raw chunks
  4. Recall: dense over both `chat_chunk_embeddings` AND
     `chat_chunk_clusters` (or just one table with cluster rows tagged)
"""
from __future__ import annotations

import json
from typing import Optional


GAP_MINUTES = 30  # TextTiling-equivalent: gap > 30 min = new cluster
SUMMARY_MAX_TOKENS = 200


def _gap_seconds_between(ts_a: str, ts_b: str) -> float:
    """Return (ts_b - ts_a) in seconds. Accepts ISO-8601 strings."""
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in ts_a else "%Y-%m-%dT%H:%M:%SZ"
    fmt2 = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in ts_b else "%Y-%m-%dT%H:%M:%SZ"
    try:
        from datetime import datetime
        a = datetime.strptime(ts_a, fmt)
        b = datetime.strptime(ts_b, fmt2)
        return (b - a).total_seconds()
    except Exception:
        return 0.0


def build_clusters(
    nest,
    gap_seconds: int = GAP_MINUTES * 60,
    user_id: Optional[str] = None,
    namespace: Optional[str] = None,
    since_iso: Optional[str] = None,
) -> list:
    """Group adjacent chat_chunk_embeddings rows into clusters.

    Returns list of cluster dicts:
      { member_event_ids: [int, ...],
        start_ts, end_ts: str,
        prefixes_concat: str,
        member_count: int }

    Does NOT write to disk — caller decides whether to commit via
    save_cluster_summary(). Pure read-path; safe to call repeatedly.
    """
    if user_id is None:
        user_id = nest.user_id
    if namespace is None:
        namespace = nest.tier
    sql = (
        "SELECT event_id, prefix, turn_count, ts FROM chat_chunk_embeddings "
        "WHERE user_id = ? AND namespace = ?"
    )
    params = [user_id, namespace]
    if since_iso:
        sql += " AND ts >= ?"
        params.append(since_iso)
    sql += " ORDER BY ts ASC"
    rows = nest._conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        return []
    clusters = []
    cur = {
        'member_event_ids': [rows[0][0]],
        'prefixes': [rows[0][1] or ''],
        'turn_counts': [rows[0][2]],
        'start_ts': rows[0][3],
        'end_ts': rows[0][3],
    }
    for ev_id, prefix, turn_count, ts in rows[1:]:
        gap = _gap_seconds_between(cur['end_ts'], ts)
        if gap > gap_seconds:
            clusters.append(cur)
            cur = {
                'member_event_ids': [ev_id],
                'prefixes': [prefix or ''],
                'turn_counts': [turn_count],
                'start_ts': ts,
                'end_ts': ts,
            }
        else:
            cur['member_event_ids'].append(ev_id)
            cur['prefixes'].append(prefix or '')
            cur['turn_counts'].append(turn_count)
            cur['end_ts'] = ts
    clusters.append(cur)
    for c in clusters:
        c['member_count'] = len(c['member_event_ids'])
        c['prefixes_concat'] = ' | '.join(c['prefixes'])
    return clusters


def _llm_summarize(prefixes_concat: str, max_tokens: int = SUMMARY_MAX_TOKENS) -> str:
    """Cheap LLM call for cluster summary. Lazy-imports nest LLM helper."""
    try:
        from .llm_rerank import _call_cheap_llm
        return _call_cheap_llm(
            "为以下对话 chunk prefixes 生成一段简洁摘要 "
            "(<=%d tokens, 2 句话):\n\n%s" % (max_tokens, prefixes_concat)
        )
    except Exception:
        # Fallback when no LLM: use a heuristic concatenation (mem0 /
        # MemFit paper fallback). "摘要 = prefix joined + truncated".
        # Better than nothing.
        return prefixes_concat[:max_tokens * 4]


def save_cluster_summary(
    nest,
    cluster: dict,
    summary: str,
    user_id: Optional[str] = None,
    namespace: Optional[str] = None,
    embedding_model: str = '',
) -> int:
    """Insert a cluster row. Returns cluster_id."""
    if user_id is None:
        user_id = nest.user_id
    if namespace is None:
        namespace = nest.tier
    member_ids_json = json.dumps(cluster['member_event_ids'])
    cur = nest._conn.execute(
        """INSERT INTO chat_chunk_clusters
           (summary, member_event_ids, member_count, start_ts, end_ts,
            user_id, namespace, model_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            summary,
            member_ids_json,
            cluster['member_count'],
            cluster['start_ts'],
            cluster['end_ts'],
            user_id,
            namespace,
            embedding_model,
        ),
    )
    return cur.lastrowid


def cluster_summary_search(
    nest,
    query_embedding,
    limit: int = 5,
    user_id: Optional[str] = None,
    namespace: Optional[str] = None,
    max_age_days: Optional[float] = None,
) -> list:
    """Dense search over chat_chunk_clusters using cosine.

    Returns [{cluster_id, similarity, summary, member_event_ids,
              member_count, start_ts, end_ts}].
    """
    import numpy as np
    if user_id is None:
        user_id = nest.user_id
    if namespace is None:
        namespace = nest.tier
    where = ["user_id = ?", "namespace = ?"]
    params = [user_id, namespace]
    if max_age_days is not None and max_age_days > 0:
        where.append(
            "start_ts >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)"
        )
        params.append('-%d days' % int(max_age_days))
    sql = (
        "SELECT cluster_id, summary, member_event_ids, member_count, "
        "start_ts, end_ts FROM chat_chunk_clusters WHERE "
        + ' AND '.join(where)
    )
    rows = nest._conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        return []
    from .embeddings import astor_get_embedding_model
    model = astor_get_embedding_model()
    q_norm = float(np.linalg.norm(query_embedding)) or 1.0
    results = []
    for cid, summary, members, mcnt, s_ts, e_ts in rows:
        if not summary:
            continue
        try:
            emb = np.array(list(model.embed([summary]))[0], dtype=np.float32)
            e_norm = float(np.linalg.norm(emb)) or 1.0
            sim = float((emb @ query_embedding) / (q_norm * e_norm))
        except Exception:
            continue
        results.append({
            'cluster_id': cid,
            'similarity': round(sim, 4),
            'summary': summary[:500],
            'member_event_ids': json.loads(members),
            'member_count': mcnt,
            'start_ts': s_ts,
            'end_ts': e_ts,
        })
    results.sort(key=lambda x: -x['similarity'])
    return results[:limit]


__all__ = [
    'GAP_MINUTES', 'SUMMARY_MAX_TOKENS',
    'build_clusters', 'save_cluster_summary',
    'cluster_summary_search',
]