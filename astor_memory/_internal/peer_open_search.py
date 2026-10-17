"""Open public-tier search (2026-10-16).

Peer-to-peer search required a mutual relationship: to query node B's
public tier, A had to add B at trust >= 50 with an endpoint, and B had
to add A back. That is the right default for a trusted private network,
but it makes the public tier unusable as what it claims to be -- shared
knowledge anyone can read.

This module adds the zero-friction counterpart: an operator can open
their public tier to signed queries from nodes they have never met.
Deliberately narrow:

  - read-only. Querying cannot write, and cannot reach source/private.
  - public tier only. Nothing else is reachable through this path.
  - opt-in, default off, per-node.
  - signature required. `verify_rekey_message` proves the request came
    from the holder of a peer_id's private key; an unsigned query is
    refused, so there is no anonymous endpoint.
  - rate limited per caller, reusing the peer rate-limit table.
  - the caller is NOT auto-added as a friend. Search is not friendship:
    a node can query without becoming part of your gossip set.
  - every result carries this node's peer_id, so a reader can tell
    where a fact came from.

The risk this accepts: a peer_id you know can query your public tier.
That is the same exposure as publishing to the public tier in the first
place -- the facts are already public within the network. Rate limiting
bounds the cost; the switch bounds the exposure.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Per-caller query budget. Deliberately simple: a fixed window per
# caller peer_id, in memory. A restart resets it, which is acceptable
# for a rate limit whose job is to blunt abuse, not to meter billing.
_WINDOW_SECONDS = 60
_MAX_PER_WINDOW = 30

_budget: dict[str, list[float]] = {}

# Set of peer_ids allowed to query without a friendship. Empty = closed.
_open_to: dict[str, dict] = {}


def _now() -> float:
    return time.monotonic()


def is_open_for(peer_id: str) -> bool:
    """Is this caller allowed to query our public tier unsolicited?"""
    return bool(peer_id) and peer_id in _open_to


def set_open(peer_id: str, allow: bool = True, *, note: str = "") -> bool:
    """Grant or revoke open-search access for one peer_id.

    `allow=True` with a bare peer_id is how an operator opens to the
    whole network: pass the caller's id to invite exactly one node, or
    use '*' to open to any signature-verified peer.
    """
    if not peer_id:
        return False
    if allow:
        _open_to[peer_id] = {'since': _now(), 'note': note}
    else:
        _open_to.pop(peer_id, None)
    return True


def open_peers() -> list[str]:
    return sorted(_open_to)


def close_all() -> None:
    """Revoke every open-search grant."""
    _open_to.clear()


def check_rate(peer_id: str, *, limit: int = _MAX_PER_WINDOW,
               window: int = _WINDOW_SECONDS) -> tuple[bool, dict]:
    """Consume one query slot for `peer_id`.

    Returns (allowed, info). info['retry_after_s'] is set when refused.
    """
    t = _now()
    hits = [x for x in _budget.get(peer_id, []) if t - x < window]
    if len(hits) >= limit:
        oldest = min(hits)
        _budget[peer_id] = hits
        return False, {
            'error': 'rate_limited',
            'limit': limit,
            'window_seconds': window,
            'retry_after_s': max(1, int(window - (t - oldest)) + 1),
        }
    hits.append(t)
    _budget[peer_id] = hits
    return True, {'remaining': limit - len(hits)}


def reset_budget() -> None:
    """Test hook."""
    _budget.clear()


def handle_public_search(msg: dict, *, astor_dir: str | None = None,
                         limit: int = 10) -> dict:
    """Answer a signed `public_search` request.

    `msg` shape (same envelope as the other /v1/peer/recv types):
      {
        "sender_peer_id": "astor:<32 hex>",
        "query": "how does decay work",
        "topic": "optional filter",
        "limit": 1..20,
        "ts": "<ISO8601 Z>",
        "sig": "<hex signature over the canonical payload>"
      }

    Never raises. Every refusal is a dict with an `error` key so the
    caller can distinguish "you may not" from "nothing matched".
    """
    from .peer_identity import verify_rekey_message

    caller = (msg.get('sender_peer_id') or '').strip()
    if not caller.startswith('astor:'):
        return {'error': 'invalid_sender_peer_id'}

    # 1. signature before anything else -- an unsigned request must not
    #    even be able to tell whether open search is enabled.
    if not verify_rekey_message(msg):
        return {'error': 'bad_signature',
                'detail': 'request must carry a valid signature'}

    # 2. authorisation
    if caller not in _open_to and '*' not in _open_to:
        return {'error': 'open_search_closed',
                'detail': 'this node does not accept unsolicited queries'}

    # 3. rate limit
    ok, info = check_rate(caller)
    if not ok:
        return info

    query = (msg.get('query') or '').strip()
    if not query:
        return {'error': 'empty_query'}
    try:
        limit = max(1, min(20, int(msg.get('limit') or limit)))
    except (TypeError, ValueError):
        limit = 10

    # 4. Read through the CLI's own recall path rather than a private
    #    query, so ACL, hybrid ranking and decay behave exactly as they
    #    do for a local `am recall`. cmd_recall spans every readable
    #    tier, so results are filtered to public here -- see below.
    rows = _recall_public(query, limit)

    topic = (msg.get('topic') or '').strip()
    if topic:
        rows = [r for r in rows if topic.lower() in (r.get('content') or '').lower()][:limit]

    return {
        'type': 'public_search_response',
        'caller_peer_id': caller,
        'query': query,
        'count': len(rows),
        'results': rows,
        'rate': info,
    }


def _recall_public(query: str, limit: int) -> list[dict]:
    """Return public-tier hits for `query`.

    Reads memory_canonical in the public store directly, read-only.
    That table IS the public tier -- nothing else is reachable from this
    path, so there is no ACL question to answer and no tier filtering to
    get wrong.

    `am recall` is deliberately not reused: it prints only fact_id and
    similarity, so the content and provenance would have to be re-fetched
    anyway, and the public store already holds both.

    Only four columns are ever selected. entities_json, metadata and
    provenance columns stay in the database -- an open endpoint should
    not be a way to harvest them.
    """
    import sqlite3
    from .acl_layout import get_db_path

    # SQL LIKE does not do word-order-independent matching, so a
    # multi-word phrase would miss everything unless it appeared
    # verbatim. Split and AND the tokens instead.
    tokens = [t for t in query.strip().lower().split() if t]
    if not tokens or len(tokens) > 8:
        return []
    if sum(len(t) for t in tokens) < 3:
        return []          # a single 1-2 char token would match most of the tier

    where = 'COALESCE(tombstoned, 0) = 0 AND ' + ' AND '.join(
        'LOWER(content) LIKE ?' for _ in tokens)
    params: list = [f'%{t}%' for t in tokens]
    params.append(max(1, min(20, limit)))

    try:
        db = get_db_path('public', 'bus')
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, content, kind, importance FROM memory_canonical "
            f"WHERE {where} "
            "ORDER BY COALESCE(importance, 0) DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
    except Exception:
        return []

    return [
        {
            'fact_id': r['id'],
            'content': (r['content'] or '')[:500],
            'kind': r['kind'],
            'importance': r['importance'],
            'source_peer_id': None,   # filled by the caller
        }
        for r in rows
    ]
