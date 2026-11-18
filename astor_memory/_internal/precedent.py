"""Look up prior art for an operation that just failed.

Why this exists (2026-11-17): astor keeps three outcome zones -- what
worked (``success_pattern``), what did not (``failure_pattern``) and what
was learned the hard way (``postmortem`` / ``lesson``). Nothing ever read
them at the moment they mattered. An agent that had never done a thing
before, tried it, and failed had no way to learn that astor already knew
the answer: the recall tool's own description names user preferences and
recurring bugs, so "how do I read a wechat article" never triggered it.

This module is the retrieval half of the fix, and it is deliberately
platform neutral -- any agent that can talk HTTP can use it. The trigger
(the instant an operation fails) belongs to the host agent; the knowledge
of *which* zone to look in and *how* to phrase the query belongs here, so
that every host gets it right without re-deriving it.

Two design points worth keeping:

* The operation being attempted is a better query than the error message.
  "curl mp.weixin.qq.com/s/x" matches a stored recipe; "SSL connect
  error" matches nothing. The error is still appended, because a
  distinctive error string sometimes is the only thing that matches.
* "No precedent found" is a real answer, not a failure. The caller can
  tell the user that nothing is known, which is different from silently
  retrying.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

# The outcome zones worth consulting after a failure. 'procedure' and
# 'experience' are included because a procedure is exactly "how this was
# done successfully" and an experience is its untyped sibling.
EXPERIENCE_KINDS: tuple[str, ...] = (
    'failure_pattern',
    'success_pattern',
    'lesson',
    'postmortem',
    'procedure',
    'experience',
)

# Kinds that are never prior art even if a caller passes them in.
_NOISE_KINDS = frozenset({
    'event', 'session', 'snapshot', 'observation', 'raw',
})


def build_query(operation: str = '', error: str = '', tool: str = '') -> str:
    """Compose the search query for a failed operation.

    The operation comes first and carries the most weight: it describes
    what the agent was trying to do, which is what the stored recipe is
    about. The error is appended for the case where a distinctive error
    string is the only thing that will match.
    """
    parts: list[str] = []
    for chunk in (tool, operation, error):
        chunk = (chunk or '').strip()
        if chunk:
            parts.append(chunk)
    return ' '.join(parts)


def _kind_filter(kinds: Iterable[str]) -> tuple[str, ...]:
    wanted = {str(k).strip() for k in kinds if str(k).strip()}
    wanted -= _NOISE_KINDS
    return tuple(sorted(wanted))


def find_precedent(
    query: str,
    user_id: str | None = None,
    limit: int = 5,
    kinds: Sequence[str] | None = None,
    min_similarity: float = 0.87,
    level: str = 'L1',
    peer_fanout: bool | None = None,
) -> dict[str, Any]:
    """Search the outcome zones for prior art about ``query``.

    Returns a dict with ``found``, ``count`` and ``results``. Each result
    carries the fact id, its kind, its similarity and the fact text.

    When the local node has no match at all, the peer network is asked
    (its public tier is where the shared outcome zones live) and any hit
    is tagged with its source.

    ``min_similarity`` drops weak matches: an empty result then means
    "nothing close enough is known", which the caller can act on, rather
    than handing back a lesson that merely shares vocabulary.

    The public tier carries the success and failure zones; the lesson and
    postmortem kinds live in the caller's own private tier. Both are
    searched, because a lesson recorded as private is exactly the one
    worth surfacing to the agent that just made the same mistake.
    """
    query = (query or '').strip()
    if not query:
        return {'found': False, 'count': 0, 'results': [], 'query': ''}

    wanted = _kind_filter(kinds if kinds is not None else EXPERIENCE_KINDS)
    if not wanted:
        return {'found': False, 'count': 0, 'results': [], 'query': query}

    # A kind filter is a post-filter, so oversample: if most of the
    # semantic neighbours are the wrong kind, the wanted ones would
    # otherwise fall outside the top_k window.
    search_limit = min(max(int(limit), 1) * 4, 100)

    from .. import astor_nest
    from ..nest.embeddings import astor_get_embedding_model

    try:
        model = astor_get_embedding_model()
        query_emb = list(model.embed([query]))[0]
    except Exception:
        return {'found': False, 'count': 0, 'results': [], 'query': query,
                'error': 'embedding_failed'}

    scored: dict[int, float] = {}

    try:
        hits = astor_nest().search(query_emb, limit=search_limit)
        for fid, sim in hits or []:
            scored[int(fid)] = max(scored.get(int(fid), 0.0), float(sim))
    except Exception:
        pass

    if user_id:
        try:
            # own_private_scope, not open_own_private: the latter binds the
            # owner identity only while it opens the bus and then restores
            # the previous actor, so the search that follows would run as
            # the original actor and be refused.
            from .acl import own_private_scope
            with own_private_scope(user_id):
                priv = astor_nest(tier='private', user_id=user_id)
                for fid, sim in priv.search(query_emb, limit=search_limit) or []:
                    scored[int(fid)] = max(scored.get(int(fid), 0.0), float(sim))
        except Exception:
            pass

    if not scored:
        return {'found': False, 'count': 0, 'results': [], 'query': query}

    ordered = sorted(scored.items(), key=lambda kv: -kv[1])[:search_limit]

    rows = _load_facts([fid for fid, _ in ordered], user_id)
    results = []
    for fid, sim in ordered:
        row = rows.get(fid)
        if not row:
            continue
        if row['kind'] not in wanted:
            continue
        # A weak semantic match is worse than no match: the caller reads
        # "here is how this was solved last time" and gets a lesson about
        # something else that merely shares vocabulary. 0.87 was measured
        # on this host: a real recipe sat at 0.886 while the machine
        # generated tick lessons clustered at 0.847-0.865.
        if float(sim) < float(min_similarity):
            continue
        results.append({
            'fact_id': fid,
            'kind': row['kind'],
            'similarity': round(float(sim), 4),
            'tier': row['tier'],
            'content': row['content'],
        })
        if len(results) >= limit:
            break

    peer_info: dict[str, Any] = {}
    if not results:
        # Nothing local. The peer network holds other nodes' public
        # outcome zones, and a recipe that exists there is still a
        # recipe. Fan out only on a miss, and only when the node-level
        # switch allows it, so a hit never pays a network round trip.
        peer_info = _peer_precedent(query, limit, peer_fanout, min_similarity)
        for row in peer_info.get('results', []):
            results.append(row)
            if len(results) >= limit:
                break

    return {
        'found': bool(results),
        'count': len(results),
        'results': results,
        'query': query,
        'kinds': list(wanted),
        'peer': peer_info,
    }


def _rescore(rows: list[dict], query: str, min_similarity: float) -> list[dict]:
    """Recompute similarity for remote rows with the local embedding model.

    A peer's relevance score is not comparable with the local similarity,
    so the local model re-ranks the remote text against the same query.
    Rows that do not clear the threshold are dropped: a remote answer
    that merely shares vocabulary is worse than no answer.
    """
    if not rows:
        return rows
    try:
        import numpy as np
        from ..nest.embeddings import astor_get_embedding_model
        model = astor_get_embedding_model()
        q = np.asarray(list(model.embed([query]))[0], dtype='float64')
        docs = np.asarray(list(model.embed([r['content'] for r in rows])), dtype='float64')
        qn = np.linalg.norm(q) or 1.0
        dn = np.linalg.norm(docs, axis=1)
        dn[dn == 0] = 1.0
        sims = (docs @ q) / (dn * qn)
    except Exception:
        # Without a scoring pass, keep the peer's own ordering rather
        # than dropping everything.
        return rows

    kept = []
    for row, sim in zip(rows, sims.tolist()):
        row['similarity'] = round(float(sim), 4)
        if float(sim) >= float(min_similarity):
            kept.append(row)
    kept.sort(key=lambda r: -r['similarity'])
    return kept


def _peer_precedent(query: str, limit: int, peer_fanout: bool | None,
                    min_similarity: float = 0.87) -> dict[str, Any]:
    """Ask the peer network for prior art when the local node has none.

    Peers expose their public tier, which is where the success and
    failure zones live. A recipe another node recorded is still a
    recipe, and a node that has never solved this problem should not
    have to solve it alone.

    Honours the node-level peer switch (``peer_fanout.is_enabled``) so a
    node that does not want to answer remote searches stays quiet. Every
    returned row is tagged with its source so the caller can tell a
    local fact from a remote one.
    """
    if peer_fanout is False:
        return {'enabled': False, 'results': [], 'per_peer': [],
                'peer_count': 0, 'reason': 'disabled_by_caller'}
    try:
        from . import peer_fanout as _pf
        if peer_fanout is None and not _pf.is_enabled():
            return {'enabled': False, 'results': [], 'per_peer': [],
                    'peer_count': 0, 'reason': 'node_switch_off'}
    except Exception:
        pass

    try:
        from .peer_recall import dispatch_peer_fanout
    except Exception as exc:
        return {'enabled': True, 'results': [], 'per_peer': [],
                'peer_count': 0, 'reason': f'no_peer_module:{exc}'}

    try:
        fanout = dispatch_peer_fanout(query=query, limit=max(int(limit), 1) * 2)
    except Exception as exc:
        return {'enabled': True, 'results': [], 'per_peer': [],
                'peer_count': 0, 'reason': f'dispatch_failed:{exc}'}

    rows = []
    for row in (fanout.get('peer_results') or []):
        content = str(row.get('content') or '').strip()
        if not content:
            continue
        peer_id = row.get('peer_id') or row.get('source_peer') or 'peer'
        rows.append({
            'fact_id': row.get('fact_id') or row.get('id'),
            'kind': row.get('kind') or 'peer_fact',
            'peer_relevance': row.get('relevance'),
            'tier': 'peer_public',
            'content': content,
            'source': 'peer:%s' % peer_id,
        })

    # The peer scores its hits with its own relevance measure, which is not
    # the same scale as the local similarity, so it cannot be compared
    # against min_similarity. Recompute with the local model instead: same
    # query, same embedding space, same threshold. Without this a peer
    # answers every question with whatever its public tier happens to
    # rank highest.
    rows = _rescore(rows, query, min_similarity)

    return {
        'enabled': True,
        'results': rows[:limit],
        'per_peer': fanout.get('per_peer') or [],
        'peer_count': fanout.get('peer_count') or 0,
        'local_error': fanout.get('local_error'),
    }


def _load_facts(fact_ids: list[int], user_id: str | None) -> dict[int, dict[str, Any]]:
    """Load kind, tier and content for the candidate ids."""
    if not fact_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    marks = ','.join('?' for _ in fact_ids)

    def _read(bus, tier: str) -> None:
        try:
            rows = bus.conn.execute(
                f"SELECT id, kind, content FROM memory_canonical "
                f"WHERE id IN ({marks}) AND tombstoned = 0",
                fact_ids,
            ).fetchall()
        except Exception:
            return
        for fid, kind, content in rows:
            fid = int(fid)
            if fid in out:
                continue
            out[fid] = {'kind': str(kind), 'tier': tier, 'content': content or ''}

    def _ensure_acl() -> None:
        # The public bus refuses to open without a bound ACL context, and
        # a caller that only knows its own identity has no reason to have
        # initialised one. Bind an admin actor once so the public tier is
        # always readable; the private tier below binds the caller's own.
        try:
            from .acl import astor_init_acl
            astor_init_acl(actor='admin:admin', role='admin', tier='public')
        except Exception:
            pass

    try:
        from ..bus import astor_bus
        _read(astor_bus(tier='public'), 'public')
    except Exception:
        _ensure_acl()
        try:
            from ..bus import astor_bus
            _read(astor_bus(tier='public'), 'public')
        except Exception:
            pass

    if user_id:
        try:
            from .acl import own_private_scope
            from ..bus import astor_bus
            with own_private_scope(user_id):
                _read(astor_bus(tier='private', user_id=user_id), 'private')
        except Exception:
            pass

    return out
