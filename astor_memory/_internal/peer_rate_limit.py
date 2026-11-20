"""v1.15.22 (2026-09-28) — Per-peer rate limit for PPS path.

R12593 (locked 2026-09-16): PPS server-to-server sync is NOT subject to
the per-actor 24h rate limit (which exists for single-agent clients). But
left unbounded, a runaway peer could DoS the local server. So PPS uses
a SEPARATE per-peer_id budget: 1000 requests per peer per 24h by default,
tunable via env ASTOR_PEER_RATE_LIMIT_PER_24H.

Design choices:

  - The counter lives in SQLite (peer_state.db), not in this process's
    memory. An in-memory dict gave every worker its own budget, so the
    peer's real allowance was N x 1000 when the node ran N workers --
    a replayed signed URL then burned one budget per worker.
  - The same file holds the one-time nonce ledger, because a nonce that
    only one worker knows about is not a nonce.
  - Sliding window of timestamps, so a peer that keeps a steady rate
    still gets its full budget every 24h.
  - ``_BUCKET`` stays as a dict-shaped view over the shared table: older
    code (and the test suite) pokes at it as a dict, and the storage
    underneath is now shared by every worker.
  - Admin escape hatch: ``reset(peer_id)`` clears one peer's bucket.
  - ``rebuild_from_audit`` lets the server rebuild buckets after a restart
    by replaying the latest 24h of audit rows with ``action='peer_search'``
    and actor starting with ``peer:``. This is the durable fallback.

Functions:
    check_and_consume(peer_id) -> (allowed: bool, count: int, retry_after_s: int)
        Atomic: if allowed, appends this timestamp; if not, returns retry-after.
        Used by ``/v1/peer/public_search`` before doing any work.
    consume_nonce(peer_id, nonce) -> bool
        Atomic: True the first time this (peer, nonce) is seen, False on
        every replay. Used to stop a captured signed URL from being
        replayed into the peer's budget.
    snapshot(peer_id) -> {count, window_hours, cap, last_request_iso, oldest}
        Read-only — used by ``/v1/peer/rate-limit`` (admin peek).
    reset(peer_id) -> int (rows cleared)
        Admin escape hatch. Called via REST or CLI.
    rebuild_from_audit(astor_dir=None) -> int (rows replayed)
        Called by server start hooks and CLI ``am peer rl rebuild``.
        Reads from audit_logger.conn for last 24h of ``peer_search`` rows
        and rebuilds the buckets.

Constants:
    DEFAULT_LIMIT_PER_24H = 1000
    WINDOW_SECONDS = 24 * 3600
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from .acl_layout import get_astor_dir
from .peer_search import TIMESTAMP_AGE_SECONDS

DEFAULT_LIMIT_PER_24H = 1000
WINDOW_SECONDS = 24 * 3600

# 一次性 nonce 留多久 = 签名请求本身还能被接受多久(复用 peer_search 的
# 7 天新鲜度窗口)。更老的请求本来就过不了时间戳校验, 留着只是占地方。
NONCE_TTL_SECONDS = TIMESTAMP_AGE_SECONDS

_DB_NAME = "peer_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_budget (
    peer_id TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_peer_budget_peer ON peer_budget(peer_id, ts);

CREATE TABLE IF NOT EXISTS peer_nonce (
    peer_id TEXT NOT NULL,
    nkey TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (peer_id, nkey)
);
CREATE INDEX IF NOT EXISTS ix_peer_nonce_ts ON peer_nonce(ts);
"""


def _now() -> float:
    return time.time()


def _cutoff() -> float:
    """窗口边界: 比它早的请求已经不在 24h 额度里了。"""
    return _now() - WINDOW_SECONDS


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


def _db_path() -> Path:
    """Where the shared counter lives.

    WHY: the budget is a property of the peer, not of this process.
    Every worker of the node has to see the same table, so it sits next
    to the other per-peer state (relationships.db) rather than in memory.
    """
    p = get_astor_dir() / "identity" / _DB_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()), timeout=30.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(_SCHEMA)
    return con


# In-process lock on top of SQLite's own locking: keeps the read-modify-
# write of the budget inside one transaction instead of racing on it.
_LOCK = threading.RLock()


def check_and_consume(peer_id: str) -> tuple[bool, int, int]:
    """Atomic check-then-record. Returns (allowed, current_count, retry_after_seconds).

    On allowed: also records the new timestamp in this peer's bucket.
    On denied: does NOT record; caller should drop / 429.
    """
    if not peer_id:
        # Unlabeled request — fail closed (counted as a hostile request).
        return False, 0, WINDOW_SECONDS
    cap = _limit()
    now = _now()
    cutoff = now - WINDOW_SECONDS
    con = _connect()
    try:
        with _LOCK:
            con.execute("BEGIN IMMEDIATE")
            try:
                # 24h 窗口滑出去的行顺手删掉, 表才不会一直长
                con.execute("DELETE FROM peer_budget WHERE peer_id=? AND ts<?",
                            (peer_id, cutoff))
                count = con.execute(
                    "SELECT COUNT(*) FROM peer_budget WHERE peer_id=?",
                    (peer_id,)).fetchone()[0]
                if count >= cap:
                    oldest = con.execute(
                        "SELECT MIN(ts) FROM peer_budget WHERE peer_id=?",
                        (peer_id,)).fetchone()[0]
                    con.execute("COMMIT")
                    retry_s = (max(1, int((oldest + WINDOW_SECONDS) - now))
                               if oldest else WINDOW_SECONDS)
                    return False, count, retry_s
                con.execute(
                    "INSERT INTO peer_budget (peer_id, ts) VALUES (?, ?)",
                    (peer_id, now))
                con.execute("COMMIT")
                return True, count + 1, 0
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()


def clear_nonce_ledger() -> int:
    """Forget every spent nonce. Returns how many rows went.

    The nonce ledger lives in SQLite so it survives across processes --
    that is the point. But it also means a test run leaves spent nonces
    behind in its ASTOR_DIR, and the next test that replays the same
    signed request would be rejected as a replay instead of running the
    path it meant to test.
    """
    con = _connect()
    try:
        cur = con.execute("DELETE FROM peer_nonce")
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def consume_nonce(peer_id: str, nonce: str) -> bool:
    """Record a one-time token. False means "already used" -> replay.

    WHY: the peer's budget is the thing an attacker wants to burn, and a
    captured signed URL can be replayed without the requestor's key. One
    row per (peer, nonce) means the second use of the same request is
    refused — and the row has to be in the shared table, or a second
    worker would happily accept the replay.
    """
    if not peer_id or not nonce:
        return False
    # 只存摘要: nonce 是不受信任的输入, 而且长度没有上限
    key = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    now = _now()
    con = _connect()
    try:
        with _LOCK:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM peer_nonce WHERE ts < ?",
                            (now - NONCE_TTL_SECONDS,))
                cur = con.execute(
                    "INSERT OR IGNORE INTO peer_nonce (peer_id, nkey, ts) "
                    "VALUES (?, ?, ?)", (peer_id, key, now))
                con.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()


def snapshot(peer_id: str) -> dict:
    """Read-only view of one peer's bucket."""
    cap = _limit()
    if not peer_id:
        return {"peer_id": "", "count": 0, "cap": cap,
                "window_hours": WINDOW_SECONDS // 3600,
                "last_request_iso": None, "oldest_iso": None,
                "retry_after_seconds": None}
    con = _connect()
    try:
        row = con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM peer_budget "
            "WHERE peer_id=? AND ts>=?", (peer_id, _cutoff())).fetchone()
    finally:
        con.close()
    count = int(row[0] or 0)
    if not count:
        return {"peer_id": peer_id, "count": 0, "cap": cap,
                "window_hours": WINDOW_SECONDS // 3600,
                "last_request_iso": None, "oldest_iso": None,
                "retry_after_seconds": None}
    oldest, last = float(row[1]), float(row[2])
    retry_s = max(0, int((oldest + WINDOW_SECONDS) - _now()))
    return {"peer_id": peer_id, "count": count, "cap": cap,
            "window_hours": WINDOW_SECONDS // 3600,
            "last_request_iso": _iso(last),
            "oldest_iso": _iso(oldest),
            "retry_after_seconds": retry_s}


def reset(peer_id: str) -> int:
    """Admin escape hatch. Returns count cleared."""
    con = _connect()
    try:
        with _LOCK:
            cur = con.execute("DELETE FROM peer_budget WHERE peer_id=?",
                               (peer_id,))
            return cur.rowcount
    finally:
        con.close()


def all_snapshots() -> list[dict]:
    """For the dashboard / CLI ``am peer rl list`` view."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT peer_id, COUNT(*), MIN(ts), MAX(ts) FROM peer_budget "
            "WHERE ts>=? GROUP BY peer_id", (_cutoff(),)).fetchall()
    finally:
        con.close()
    out = []
    for pid, count, oldest, last in rows:
        out.append({"peer_id": pid, "count": int(count),
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
    cutoff_iso = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=WINDOW_SECONDS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = []
    try:
        con = sqlite3.connect(str(path), check_same_thread=False)
        cur = con.execute(
            "SELECT actor, ts FROM audit "
            "WHERE action = 'peer_search' AND ts >= ?",
            (cutoff_iso,),
        )
        rows = cur.fetchall()
        con.close()
    except Exception:
        return 0
    entries = []
    for actor, ts in rows:
        peer_id = actor[len("peer:"):].strip() if actor.startswith("peer:") else ""
        if not peer_id:
            continue
        # parse ISO back to epoch
        try:
            t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        entries.append((peer_id, t))
    con = _connect()
    try:
        with _LOCK:
            con.execute("BEGIN IMMEDIATE")
            try:
                # Wipe and repopulate (each restart gets a clean window).
                con.execute("DELETE FROM peer_budget")
                con.executemany(
                    "INSERT INTO peer_budget (peer_id, ts) VALUES (?, ?)",
                    entries)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()
    return len(entries)


def status_summary() -> dict:
    """Admin dashboard summary: total peers, total requests in window, total cap."""
    con = _connect()
    try:
        peers = con.execute(
            "SELECT COUNT(DISTINCT peer_id) FROM peer_budget WHERE ts>=?",
            (_cutoff(),)).fetchone()[0]
        total = con.execute(
            "SELECT COUNT(*) FROM peer_budget WHERE ts>=?",
            (_cutoff(),)).fetchone()[0]
    finally:
        con.close()
    return {
        "peers_tracked": int(peers),
        "requests_in_window": int(total),
        "cap_per_peer_per_24h": _limit(),
        "window_hours": WINDOW_SECONDS // 3600,
    }


class _BudgetView:
    """dict 形状的兼容层。

    WHY: 旧的调用方(CLI 清理钩子、测试)把 _BUCKET 当 dict 用, 而额度
    现在必须活在所有 worker 共享的表里 —— 所以这里只保留 dict 的外形,
    真正的存储是共享的 SQLite 表。
    """

    def __getitem__(self, peer_id: str) -> list:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts FROM peer_budget WHERE peer_id=? ORDER BY ts",
                (peer_id,)).fetchall()
        finally:
            con.close()
        return [float(r[0]) for r in rows]

    def __setitem__(self, peer_id: str, timestamps) -> None:
        con = _connect()
        try:
            with _LOCK:
                con.execute("BEGIN IMMEDIATE")
                try:
                    con.execute("DELETE FROM peer_budget WHERE peer_id=?",
                                (peer_id,))
                    con.executemany(
                        "INSERT INTO peer_budget (peer_id, ts) VALUES (?, ?)",
                        [(peer_id, float(t)) for t in timestamps])
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        finally:
            con.close()

    def __delitem__(self, peer_id: str) -> None:
        con = _connect()
        try:
            con.execute("DELETE FROM peer_budget WHERE peer_id=?", (peer_id,))
        finally:
            con.close()

    def get(self, peer_id: str, default=None):
        rows = self[peer_id]
        return rows if rows else default

    def pop(self, peer_id: str, default=None):
        rows = self[peer_id]
        if not rows:
            return default
        del self[peer_id]
        return rows

    def clear(self) -> None:
        con = _connect()
        try:
            con.execute("DELETE FROM peer_budget")
        finally:
            con.close()

    def keys(self) -> list:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT peer_id FROM peer_budget").fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def items(self) -> list:
        return [(pid, self[pid]) for pid in self.keys()]

    def __contains__(self, peer_id: str) -> bool:
        return peer_id in self.keys()

    def __len__(self) -> int:
        return len(self.keys())

    def __iter__(self):
        return iter(self.keys())


_BUCKET = _BudgetView()
