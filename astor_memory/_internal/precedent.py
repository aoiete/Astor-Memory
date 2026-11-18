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
) -> dict[str, Any]:
    """Search the outcome zones for prior art about ``query``.

    Returns a dict with ``found``, ``count`` and ``results``. Each result
    carries the fact id, its kind, its similarity and the fact text.

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

    return {
        'found': bool(results),
        'count': len(results),
        'results': results,
        'query': query,
        'kinds': list(wanted),
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
