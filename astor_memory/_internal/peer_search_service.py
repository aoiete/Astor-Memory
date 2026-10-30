"""The receiver half of a peer search.

The HTTP endpoint and the relay both answer the same question: "this
peer asks for your public tier -- may they, and what do they get?"
Those rules are the security-relevant part, so they live here once and
both callers go through them. A relay must not get a laxer answer than
a direct request.

The rules, in order:

  - the request carries a signature made with the requestor's identity
    key, and it is checked against the key in the request
  - the requestor is a known peer, not blacklisted, with trust >= 50,
    and has allow_search set
  - the requestor is charged against the per-peer search budget
  - the answer holds only `commons` rows of the public tier: never a
    personal fact, never a tombstoned one

v1.17.21. The body of this function used to live inside the Flask
endpoint, which meant the relay path would have had to grow its own
copy -- and a copy of an authorization check is a copy that can drift.
"""
from __future__ import annotations

import json
from typing import Any


def answer_search_request(d: dict) -> tuple[int, dict]:
    """Answer a decoded peer-search request.

    Returns (status, body). The body is what the caller should send
    back; on success it is the peer search response document.
    """
    from .. import bus as _bus_pkg
    from .. import nest as _nest_pkg
    from ..nest.embeddings import astor_get_embedding_model
    from ..nest.lex_index import astor_lex as _pp_lex
    from ..nest.lex_index import hybrid_merge as _pp_merge
    from .peer_identity import get_identity as _pp_get_identity
    from .peer_identity import verify as _peer_verify
    from .peer_relationships import get_peer as _pp_get_peer
    from .peer_search import (
        MAX_RESULTS_PER_PEER,
        PeerSearchRequest,
        PeerSearchResponse,
        PeerSearchResult,
    )

    # The server wraps the bus and nest factories so every one created
    # inside a request is tracked for cleanup. It patches the package
    # attribute, so the lookup has to happen here, at call time -- an
    # import-time binding would keep the unwrapped original and the
    # per-request cleanup would miss it.
    astor_bus = _bus_pkg.astor_bus
    astor_nest = _nest_pkg.astor_nest

    # Construct-time validation — raises on malformed/stale input.
    try:
        req = PeerSearchRequest(
            requestor_peer_id=d['requestor_peer_id'],
            requestor_pubkey=d['requestor_pubkey'],
            query=d['query'],
            topic=d.get('topic') or None,
            limit=int(d.get('limit', 10)),
            timestamp=d['timestamp'],
            signature=d['signature'],
        )
    except KeyError as e:
        return 400, {'error': 'missing_field', 'field': str(e)}
    except ValueError as e:
        return 400, {'error': 'invalid_request', 'detail': str(e)}
    except Exception as e:
        return 400, {'error': 'invalid_request', 'detail': str(e)}

    # Self-search guard: a peer searching itself via this endpoint is a
    # topology error, not an attack — reject cleanly.
    try:
        from .peer_identity import get_identity as _pp_get_identity
        _me = _pp_get_identity()
        if _me and req.requestor_peer_id == _me.get('peer_id'):
            return 400, {'error': 'self_search_not_allowed'}
    except Exception:
        pass

    # Signature verification (never raise to caller; hostile sig = 403).
    if not req.verify_signature():
        try:
            from .audit_logger import astor_audit as _pp_audit
            _pp_audit(
                actor=f'peer:{req.requestor_peer_id}',
                tier='public',
                action='peer_search_sig_reject',
                user_id=None,
                detail='ed25519 verification failed',
            )
        except Exception:
            pass
        return 403, {'error': 'bad_signature'}

    # Receiver-side opt-in chain: known peer, not blacklisted,
    # trust >= 50, allow_search=True.
    peer_row = _pp_get_peer(req.requestor_peer_id)
    if not peer_row:
        return 403, {'error': 'unknown_peer',
                        'detail': 'friend must add you first (am peer add)'}
    if (peer_row.get('kind') or 'friend') == 'blacklist':
        return 403, {'error': 'peer_blacklisted'}
    if int(peer_row.get('trust') or 0) < 50:
        return 403, {'error': 'trust_below_threshold',
                        'detail': 'trust must be >= 50'}
    _meta = peer_row.get('metadata') or {}
    if isinstance(_meta, str):
        try:
            _meta = json.loads(_meta)
        except Exception:
            _meta = {}
    if not isinstance(_meta, dict) or not _meta.get('allow_search'):
        return 403, {'error': 'search_not_allowed',
                        'detail': 'friend has not opted in '
                                  '(am peer allow-search <requestor_peer_id>)'}
    # v1.15.22 Ship E: per-peer rate limit (R12593 lock — PPS path is
    # its own budget, separate from per-actor limits). Enforce BEFORE
    # doing any expensive search work so a runaway peer doesn't trigger
    # embedding model loads.
    from .peer_rate_limit import (
        check_and_consume as _prl_check,
        DEFAULT_LIMIT_PER_24H as _PRL_DEFAULT,
    )
    _allowed, _rl_count, _rl_retry = _prl_check(req.requestor_peer_id)
    if not _allowed:
        return 429, {
            'error': 'peer_rate_limit_exceeded',
            'detail': (f'this peer has exceeded the '
                       f'{_PRL_DEFAULT} req/24h PPS budget'),
            'count_in_window': _rl_count,
            'retry_after_seconds': _rl_retry,
        }

    # Clamp limit (already validated 1..20 at construction, but be safe).
    limit = min(int(req.limit), MAX_RESULTS_PER_PEER)

    # Search PUBLIC tier only. tombstoned facts are excluded.
    # Vector + BM25 merge, keep top `limit` by combined score.
    tier = 'public'
    try:
        nest = astor_nest(tier=tier, user_id=None)
        bus = astor_bus(tier=tier, user_id=None)
        from ..nest.embeddings import astor_get_embedding_model
        model = astor_get_embedding_model()
        query_emb = list(model.embed([req.query]))[0]

        from ..nest.lex_index import astor_lex as _pp_lex, hybrid_merge as _pp_merge
        lex = _pp_lex(tier=tier, user_id=None)
        vector_hits = nest.search(query_emb, limit=limit * 2)
        bm25_hits = lex.bm25_search(req.query, limit=limit * 2)
        merged = _pp_merge(
            bm25_hits, vector_hits,
            bm25_weight=0.6, vec_weight=0.4,
            limit=limit,
        )
    except Exception as e:
        return 500, {'error': 'search_backend_failed', 'detail': str(e)}

    # Enrich from bus, drop tombstoned, cap bytes defensively.
    me_row = _me or {}
    my_peer_id = me_row.get('peer_id', 'astor:unknown')
    results = []
    truncated = False
    for fact_id, sim in merged:
        if len(results) >= limit:
            truncated = True
            break
        row = bus.conn.execute(
            "SELECT id, content, kind, tags, created_at, tombstoned, "
            "visibility FROM memory_canonical WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if row is None or int(row[5] or 0) != 0:
            continue
        # v1.17.15: only `commons` rows are shareable. The public
        # store holds personal rows too -- `visibility` is what
        # separates them -- and this endpoint answers a remote node.
        if (row[6] or 'commons') != 'commons':
            continue
        try:
            import json as _j_pps
            tags = tuple(_j_pps.loads(row[3])) if row[3] else ()
        except Exception:
            tags = ()
        try:
            results.append(PeerSearchResult(
                source_peer_id=my_peer_id,
                source_trust=100,
                fact_id=int(row[0]),
                content=str(row[1] or '')[:2048],
                kind=str(row[2] or 'fact'),
                tags=tags,
                created_at=str(row[4] or ''),
                relevance=round(min(max(float(sim), 0.0), 1.0), 4),
            ))
        except (ValueError, TypeError):
            continue

    # v1.17.22: a connection test is a real search, so it is charged
    # against the peer's search budget like any other -- the prefix
    # must not buy a free way to hammer a peer. But it does not leave
    # a trace in the audit log: a test is not a query anyone made.
    from .peer_test import TEST_QUERY_PREFIX as _TEST_PREFIX
    _is_test = req.query.startswith(_TEST_PREFIX)
    try:
        from .audit_logger import astor_audit as _pp_audit2
        if not _is_test:
            _pp_audit2(
                actor=f'peer:{req.requestor_peer_id}',
                tier='public',
                action='peer_search',
                user_id=None,
                peer_id=req.requestor_peer_id,
                metadata={'query': req.query[:256],
                          'result_count': len(results)},
            )
    except Exception:
        pass

    resp = PeerSearchResponse(
        requestor_peer_id=req.requestor_peer_id,
        results=tuple(results),
        truncated=truncated,
    )
    return 200, resp.to_dict()
