"""v1.15.26 (2026-09-28) — Ship J: per-peer health aggregator.

Returns a status dict for one peer (or summary for all known peers).
Powers GET /v1/peer/health and `am peer health <pid>`.

Status fields (all best-effort, no critical side effects):
  - peer_id: astor:<32-hex>
  - alias: optional user-given short name
  - trust: current trust score (0..100)
  - kind: friend | blacklist | whitelist | pending
  - endpoint: URL string or None
  - has_pubkey: bool — public_key registered
  - allow_search: bool — receiver-side opt-in
  - rate_limit: {count, cap, oldest_iso, retry_after_seconds} or None
  - last_seen_iso: most recent audit row ts or None
  - last_action: most recent audit action or None
  - error_count: consecutive error count (for trust-decay)
  - online: heuristic — endpoint reachable + has_pubkey + trust>=50
  - health: 'healthy' | 'degraded' | 'unreachable' | 'unknown'
"""
from __future__ import annotations

from typing import Optional

from .peer_relationships import get_peer, list_peers
from .audit_logger import astor_query_peer_audit
from .peer_rate_limit import snapshot as _rl_snapshot


def _classify(online: bool, has_endpoint: bool, trust: int,
                allow_search: bool, kind: str = "friend") -> str:
    """Map health signals to a human-readable status.

    v1.15.28 Ship L: quarantined peers get a dedicated status so ops
    dashboards can show "this peer is intentionally isolated".
    """
    if kind == "quarantine":
        return "quarantined"
    if not has_endpoint:
        return "unknown"  # not configured yet
    if not online:
        return "unreachable"
    if trust < 30:
        return "degraded"
    if not allow_search:
        return "degraded"
    return "healthy"


def peer_health(peer_id: str) -> dict:
    """Aggregate health status for a single peer.

    Returns: dict with status fields (see module docstring). Always
    includes the peer_id even when peer is unknown (so the caller
    can distinguish 'unknown peer' from 'known but unhealthy').
    """
    p = get_peer(peer_id)
    if not p:
        return {
            "peer_id": peer_id,
            "found": False,
            "health": "unknown",
            "reason": "peer not in relationships table",
        }
    # Most recent audit row
    try:
        rows = astor_query_peer_audit(peer_id, limit=1)
    except Exception:
        rows = []
    last_seen_iso = rows[0]["ts"] if rows else None
    last_action = rows[0]["action"] if rows else None
    # Rate limit snapshot
    try:
        rl = _rl_snapshot(peer_id)
        rl_summary = {
            "count": rl.get("count", 0),
            "cap": rl.get("cap", 0),
            "oldest_iso": rl.get("oldest_iso"),
            "retry_after_seconds": rl.get("retry_after_seconds", 0),
        }
    except Exception:
        rl_summary = None
    # Error count from metadata
    meta = p.get("metadata") or {}
    err_count = 0
    if isinstance(meta, dict):
        err_count = int(meta.get("consecutive_error_count") or 0)
    # Online heuristic
    has_endpoint = bool(p.get("endpoint"))
    trust = int(p.get("trust") or 0)
    allow_search = bool(meta.get("allow_search"))
    has_pubkey = bool(p.get("public_key"))
    online = has_endpoint and has_pubkey  # don't actually ping (avoids blocking on slow peers)
    kind = p.get("kind") or "friend"
    return {
        "peer_id": peer_id,
        "alias": p.get("alias") or None,
        "kind": kind,
        "trust": trust,
        "endpoint": p.get("endpoint") or None,
        "has_pubkey": has_pubkey,
        "allow_search": allow_search,
        "rate_limit": rl_summary,
        "last_seen_iso": last_seen_iso,
        "last_action": last_action,
        "error_count": err_count,
        "online": online,
        "found": True,
        "health": _classify(online, has_endpoint, trust, allow_search, kind),
    }


def all_peer_health() -> list[dict]:
    """Health status for every known peer. Convenience wrapper."""
    return [peer_health(p["peer_id"]) for p in list_peers()]
