"""v1.15.22 (2026-09-28) — Per-peer rate limit for PPS path.

R12593 (locked 2026-09-16): PPS server-to-server sync is NOT subject to
the per-actor 24h rate limit (which exists for single-agent clients). But
left unbounded, a runaway peer could DoS the local server. So PPS uses
a SEPARATE per-peer_id budget: 1000 requests per peer per 24h by default,
tunable via env ASTOR_PEER_RATE_LIMIT_PER_24H.

Design choices:

  - In-memory dict keyed by peer_id, values = sliding window of timestamps.
    No new DB table. Storage is trivial (≤ N peers × 1000 timestamps = a
    few MB even at scale), and rebuilds from the audit log on restart
    anyway (see ``rebuild_from_audit``).
  - Lazy eviction on insertion: prune entries > 24h before counting.
  - Thread-safe via a single lock (Python dict, won't be cross-process).
  - Admin escape hatch: ``reset(peer_id)`` clears one peer's bucket.
  - ``rebuild_from_audit`` lets the server rebuild buckets after a restart
    by replaying the latest 24h of audit rows with ``action='peer_search'``
    and actor starting with ``peer:``. This is the durable fallback.

Functions:
    check_and_consume(peer_id) -> (allowed: bool, count: int, retry_after_s: int)
        Atomic: if allowed, appends this timestamp; if not, returns retry-after.
        Used by ``/v1/peer/public_search`` before doing any work.
    snapshot(peer_id) -> {count, window_hours, cap, last_request_iso, oldest}
        Read-only — used by ``/v1/peer/rate-limit`` (admin peek).
    reset(peer_id) -> int (rows cleared)
        Admin escape hatch. Called via REST or CLI.
    rebuild_from_audit(astor_dir=None) -> int (rows replayed)
        Called by server start hooks and CLI ``am peer rl rebuild``.
        Reads from audit_logger.conn for last 24h of ``peer_search`` rows
        and rebuilds the in-memory buckets.

Constants:
    DEFAULT_LIMIT_PER_24H = 1000
    WINDOW_SECONDS = 24 * 3600
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from typing import Optional


DEFAULT_LIMIT_PER_24H = 1000
WINDOW_SECONDS = 24 * 3600


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _limit() -> int:
    """Read env override (cached not — env may change in long-lived processes)."""
    raw = os.environ.get("ASTOR_PEER_RATE_LIMIT_PER_24H")
    if not raw:
        return DEFAULT_LIMIT_PER_24H
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_LIMIT_PER_24H
    except ValueError:
        return DEFAULT_LIMIT_PER_24H


# Module-level state. Single-process lock is fine; PPS volume at current
# scale is < 1000 events / 24h even with the E2E test fleet.
_BUCKET: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _evict(peer_id: str, *, now: Optional[float] = None) -> None:
    """Drop entries older than WINDOW_SECONDS."""
    t = now if now is not None else _now()
    cutoff = t - WINDOW_SECONDS
    bucket = _BUCKET.get(peer_id)
    if not bucket:
        return
    # In-place prune (timestamps are mostly append-ordered)
    pruned = [ts for ts in bucket if ts >= cutoff]
    if pruned:
        _BUCKET[peer_id] = pruned
    else:
        _BUCKET.pop(peer_id, None)


def check_and_consume(peer_id: str) -> tuple[bool, int, int]:
    """Atomic check-then-record. Returns (allowed, current_count, retry_after_seconds).

    On allowed: also records the new timestamp in this peer's bucket.
    On denied: does NOT record; caller should drop / 429.
    """
    if not peer_id:
        # Unlabeled request — fail closed (counted as a hostile request).
        return False, 0, WINDOW_SECONDS
    cap = _limit()
    with _LOCK:
        _evict(peer_id)
        bucket = _BUCKET.setdefault(peer_id, [])
        count = len(bucket)
        if count >= cap:
            # Compute retry-after based on the bucket's oldest entry
            oldest = min(bucket) if bucket else _now()
            retry_s = max(1, int((oldest + WINDOW_SECONDS) - _now()))
            return False, count, retry_s
        bucket.append(_now())
        return True, count + 1, 0


def snapshot(peer_id: str) -> dict:
    """Read-only view of one peer's bucket."""
    cap = _limit()
    if not peer_id:
        return {"peer_id": "", "count": 0, "cap": cap,
                "window_hours": WINDOW_SECONDS // 3600,
                "last_request_iso": None, "oldest_iso": None,
                "retry_after_seconds": None}
    with _LOCK:
        _evict(peer_id)
        bucket = _BUCKET.get(peer_id, [])
        if not bucket:
            return {"peer_id": peer_id, "count": 0, "cap": cap,
                    "window_hours": WINDOW_SECONDS // 3600,
                    "last_request_iso": None, "oldest_iso": None,
                    "retry_after_seconds": None}
        oldest = min(bucket)
        last = max(bucket)
        retry_s = max(0, int((oldest + WINDOW_SECONDS) - _now()))
        return {"peer_id": peer_id, "count": len(bucket), "cap": cap,
                "window_hours": WINDOW_SECONDS // 3600,
                "last_request_iso": _iso(last),
                "oldest_iso": _iso(oldest),
                "retry_after_seconds": retry_s}


def reset(peer_id: str) -> int:
    """Admin escape hatch. Returns count cleared."""
    with _LOCK:
        bucket = _BUCKET.pop(peer_id, None)
        return len(bucket) if bucket else 0


def all_snapshots() -> list[dict]:
    """For the dashboard / CLI ``am peer rl list`` view."""
    with _LOCK:
        out = []
        # Snapshot a copy under lock
        for pid in list(_BUCKET.keys()):
            _evict(pid)
        for pid, bucket in _BUCKET.items():
            if not bucket:
                continue
            oldest = min(bucket)
            last = max(bucket)
            out.append({"peer_id": pid, "count": len(bucket),
                        "cap": _limit(),
                        "last_request_iso": _iso(last),
                        "oldest_iso": _iso(oldest),
                        "window_hours": WINDOW_SECONDS // 3600})
    return out


def rebuild_from_audit(astor_dir: str | None = None) -> int:
    """Re-derive buckets from the audit log (24h lookback).

    Called by server-start hook + ``am peer rl rebuild`` CLI.
    Reads only ``action='peer_search'`` rows with actor starting ``peer:``,
    drops rows older than WINDOW_SECONDS, and populates the bucket map.
    """
    try:
        from . import audit_logger as _al
        from . import acl_layout as _al_layout
    except Exception:
        return 0
    try:
        path = _al_layout.get_audit_path()
    except Exception:
        return 0
    if not path.exists():
        return 0
    import sqlite3 as _sqlite3
    cutoff_iso = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=WINDOW_SECONDS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = []
    try:
        con = _sqlite3.connect(str(path), check_same_thread=False)
        cur = con.execute(
            "SELECT actor, ts FROM audit "
            "WHERE action = 'peer_search' AND ts >= ?",
            (cutoff_iso,),
        )
        rows = cur.fetchall()
        con.close()
    except Exception:
        return 0
    n = 0
    with _LOCK:
        # Wipe and repopulate (each restart gets a clean window).
        _BUCKET.clear()
        for actor, ts in rows:
            peer_id = actor[len("peer:"):].strip() if actor.startswith("peer:") else ""
            if not peer_id:
                continue
            # parse ISO back to epoch
            try:
                t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            _BUCKET.setdefault(peer_id, []).append(t)
            n += 1
        # Drop empty entries after pruning
        for pid in list(_BUCKET.keys()):
            _evict(pid)
    return n


def status_summary() -> dict:
    """Admin dashboard summary: total peers, total requests in window, total cap."""
    with _LOCK:
        # Evict stale
        for pid in list(_BUCKET.keys()):
            _evict(pid)
        total = sum(len(b) for b in _BUCKET.values())
        cap = _limit()
        return {
            "peers_tracked": len(_BUCKET),
            "requests_in_window": total,
            "cap_per_peer_per_24h": cap,
            "window_hours": WINDOW_SECONDS // 3600,
        }
