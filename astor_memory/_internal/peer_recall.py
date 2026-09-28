"""v1.15.24 (2026-09-28) — Ship H: Reusable PPS fan-out helper.

Extracted from the original /v1/peer/recall endpoint so /v1/read can
trigger the same peer fan-out when body.peer_fanout=true is set.

Single source of truth: both /v1/peer/recall (the explicit PPS endpoint)
and /v1/read (with the body flag) use this helper. No behavioral
divergence between the two paths.

The helper assumes local recall was already attempted and returned
empty. The calling endpoint is responsible for that gate.
"""
from __future__ import annotations

from typing import Optional


def dispatch_peer_fanout(
    *,
    query: str,
    limit: int = 5,
    topic: Optional[str] = None,
    actor_peer_id: Optional[str] = None,
) -> dict:
    """Sign a PPS request and dispatch to all eligible friends.

    Returns a dict with:
        peer_results: list[dict] — ranked by relevance desc, capped at `limit`
        per_peer: list[dict] — per-friend status (peer_id, alias, count, error, truncated)
        peer_count: int — total before cap
        local_error: str | None — none, this is fan-out only
        mode: 'peer_fanout' (constant)

    Anti-hostile: only fires when caller (the request handler) decides
    to invoke this helper. Default /v1/read does NOT call it.
    Per-peer rate limit (R12593) is enforced inside the dispatch_search
    path on each friend's /v1/peer/public_search endpoint (1000/24h).
    """
    from .peer_identity import init_identity
    from .peer_relationships import list_peers
    from .peer_search import (
        build_search_request, select_search_targets, dispatch_search_to_peers,
    )

    if not actor_peer_id:
        try:
            me = init_identity()
            actor_peer_id = me.get("peer_id")
        except Exception:
            actor_peer_id = None

    peers = list_peers()
    targets = select_search_targets(peers)
    if not targets:
        return {"peer_results": [], "per_peer": [],
                "peer_count": 0, "local_error": None,
                "mode": "peer_fanout",
                "hint": "no eligible friends (trust>=50 + endpoint + allow_search)"}

    if not actor_peer_id:
        return {"peer_results": [], "per_peer": [],
                "peer_count": 0, "local_error": "no_local_identity",
                "mode": "peer_fanout"}

    try:
        me_full = init_identity()
        if not me_full or "private_key" not in me_full:
            return {"peer_results": [], "per_peer": [],
                    "peer_count": 0, "local_error": "no_local_key",
                    "mode": "peer_fanout"}
        req = build_search_request(
            query=query,
            requestor_peer_id=actor_peer_id,
            requestor_pubkey=me_full["public_key"],
            requestor_private_key=me_full["private_key"],
            topic=topic, limit=limit,
        )
        responses = dispatch_search_to_peers(req, targets)
    except Exception as e:
        return {"peer_results": [], "per_peer": [],
                "peer_count": 0, "local_error": f"peer_dispatch_failed: {e}",
                "mode": "peer_fanout"}

    peer_results = []
    per_peer = []
    for tgt, resp in zip(targets, responses):
        per_peer.append({
            "peer_id": tgt["peer_id"],
            "alias": tgt.get("alias") or "",
            "error": resp.error,
            "count": len(resp.results),
            "truncated": resp.truncated,
        })
        for r in resp.results:
            d = r.to_dict()
            d["source"] = "peer"
            d["peer_id"] = r.source_peer_id
            peer_results.append(d)
    peer_results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    peer_results = peer_results[:limit]
    return {"peer_results": peer_results, "per_peer": per_peer,
            "peer_count": len(peer_results), "local_error": None,
            "mode": "peer_fanout"}
