"""v1.14.68 (2026-09-17) — Peer relationships (friend + trust + blacklist).

Stores the social graph: who this astor install trusts, who it ignores,
and what topic weights it associates with each peer. Separate from bus DBs
(relationships are config, not memory content).

Schema:
- peer_id (PK): astor:<32-hex>
- alias (text, nullable): human-readable short name
- kind (text): 'friend' | 'blacklist' | 'whitelist' | 'pending'
- trust (int): 0-100 (0=blacklisted, 100=fully trusted)
- added_at (iso8601): when relationship was created
- updated_at (iso8601): last update (trust change, alias, etc.)
- rekey_chain (text, JSON list): chain of historical peer_ids if rekeyed
- public_key (text, base64): last known ed25519 public key for verification
- topic_weights (text, JSON dict): trust per topic (e.g. {"poker":0.9})
- last_sync (iso8601, nullable): last successful sync with this peer
- last_topic_seen (text, nullable): most recent topic we synced from them
- metadata (text, JSON): free-form extension data

Trust semantics (locked 2026-09-16, fact 12610/12611):
- 0: blacklisted (auto-reject all incoming)
- 1-29: very low (quarantine all incoming)
- 30: default for new peers (quarantine)
- 31-49: low (manual accept only)
- 50-69: medium (auto-accept with caution)
- 70-89: high (auto-accept, KEEP trust on rekey)
- 90-100: very high (auto-accept, KEEP trust on rekey, broadcast back)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_RELATIONSHIPS_LOCK = threading.Lock()
_RELATIONSHIPS_CONN: dict[str, sqlite3.Connection] = {}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS peer_relationships (
    peer_id        TEXT PRIMARY KEY,
    alias          TEXT,
    kind           TEXT NOT NULL DEFAULT 'friend',
    trust          INTEGER NOT NULL DEFAULT 30,
    public_key     TEXT,
    added_at       TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    rekey_chain    TEXT,
    topic_weights  TEXT,
    last_sync      TEXT,
    last_topic_seen TEXT,
    metadata       TEXT
);

CREATE TABLE IF NOT EXISTS rekey_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    old_peer_id  TEXT NOT NULL,
    new_peer_id  TEXT NOT NULL,
    signature    TEXT NOT NULL,
    applied_at   TEXT NOT NULL,
    sender_pubkey TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_rekey_log_new ON rekey_log(new_peer_id);
CREATE INDEX IF NOT EXISTS idx_rekey_log_status ON rekey_log(status);
CREATE INDEX IF NOT EXISTS idx_rel_kind ON peer_relationships(kind);
CREATE INDEX IF NOT EXISTS idx_rel_trust ON peer_relationships(trust);

CREATE TABLE IF NOT EXISTS topic_index (
    topic           TEXT NOT NULL,
    peer_id         TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0,
    fact_count      INTEGER NOT NULL DEFAULT 0,
    last_seen_at    TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (topic, peer_id)
);

CREATE INDEX IF NOT EXISTS idx_topic_index_topic ON topic_index(topic);
CREATE INDEX IF NOT EXISTS idx_topic_index_peer ON topic_index(peer_id);
CREATE INDEX IF NOT EXISTS idx_topic_index_weight ON topic_index(weight);
"""


def _get_db_path(astor_dir: str | None = None) -> str:
    if astor_dir is None:
        astor_dir = os.environ.get(
            "ASTOR_DIR",
            str(Path.home() / ".astor"),
        )
    identity_dir = Path(astor_dir) / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    return str(identity_dir / "relationships.db")


def _get_conn(astor_dir: str | None = None) -> sqlite3.Connection:
    """Per-thread/per-astor-dir singleton connection.

    SQLite + threading: we use check_same_thread=False but serialize via
    _RELATIONSHIPS_LOCK for writes. Same pattern as memory bus.

    Test isolation: when astor_dir is given, callers should call
    close_all_connections() in tearDown to release file locks on Windows.
    """
    db_path = _get_db_path(astor_dir)
    key = db_path
    if key not in _RELATIONSHIPS_CONN:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with _RELATIONSHIPS_LOCK:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        _RELATIONSHIPS_CONN[key] = conn
    return _RELATIONSHIPS_CONN[key]


def close_all_connections() -> None:
    """Close all cached connections (for tests + graceful shutdown)."""
    with _RELATIONSHIPS_LOCK:
        for conn in _RELATIONSHIPS_CONN.values():
            try:
                conn.close()
            except Exception:
                pass
        _RELATIONSHIPS_CONN.clear()


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"))


def add_peer(
    peer_id: str,
    *,
    kind: str = "friend",
    trust: int = 30,
    alias: str | None = None,
    public_key: str | None = None,
    topic_weights: dict | None = None,
    metadata: dict | None = None,
    astor_dir: str | None = None,
) -> dict:
    """Add or update a peer relationship. Returns the row."""
    if not peer_id.startswith("astor:"):
        raise ValueError(f"peer_id must start with 'astor:'; got {peer_id!r}")
    if not 0 <= trust <= 100:
        raise ValueError(f"trust must be 0-100; got {trust}")
    if kind not in ("friend", "blacklist", "whitelist", "pending"):
        raise ValueError(f"invalid kind: {kind!r}")

    now = _now_iso()
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        existing = con.execute(
            "SELECT * FROM peer_relationships WHERE peer_id = ?",
            (peer_id,)
        ).fetchone()
        if existing:
            # Update — preserve added_at, refresh updated_at
            con.execute("""
                UPDATE peer_relationships
                SET alias = COALESCE(?, alias),
                    kind = ?,
                    trust = ?,
                    public_key = COALESCE(?, public_key),
                    topic_weights = COALESCE(?, topic_weights),
                    metadata = COALESCE(?, metadata),
                    updated_at = ?
                WHERE peer_id = ?
            """, (
                alias,
                kind,
                trust,
                public_key,
                json.dumps(topic_weights) if topic_weights else None,
                json.dumps(metadata) if metadata else None,
                now,
                peer_id,
            ))
        else:
            con.execute("""
                INSERT INTO peer_relationships
                (peer_id, alias, kind, trust, public_key, added_at, updated_at,
                 topic_weights, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                peer_id,
                alias,
                kind,
                trust,
                public_key,
                now,
                now,
                json.dumps(topic_weights) if topic_weights else None,
                json.dumps(metadata) if metadata else None,
            ))
        con.commit()
    return get_peer(peer_id, astor_dir=astor_dir) or {}


def remove_peer(peer_id: str, astor_dir: str | None = None) -> bool:
    """Remove a peer relationship. Returns True if removed, False if not found."""
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        cur = con.execute(
            "DELETE FROM peer_relationships WHERE peer_id = ?",
            (peer_id,)
        )
        con.commit()
    return cur.rowcount > 0


def get_peer(peer_id: str, astor_dir: str | None = None) -> dict | None:
    """Get a single peer relationship by ID."""
    con = _get_conn(astor_dir)
    row = con.execute(
        "SELECT * FROM peer_relationships WHERE peer_id = ?",
        (peer_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_peers(
    *,
    kind: str | None = None,
    min_trust: int | None = None,
    astor_dir: str | None = None,
) -> list[dict]:
    """List peer relationships, optionally filtered."""
    con = _get_conn(astor_dir)
    sql = "SELECT * FROM peer_relationships WHERE 1=1"
    args = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if min_trust is not None:
        sql += " AND trust >= ?"
        args.append(min_trust)
    sql += " ORDER BY trust DESC, alias ASC, peer_id ASC"
    rows = con.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_trust(peer_id: str, trust: int, astor_dir: str | None = None) -> dict | None:
    """Update trust for an existing peer. Returns updated row."""
    if not 0 <= trust <= 100:
        raise ValueError(f"trust must be 0-100; got {trust}")
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        con.execute("""
            UPDATE peer_relationships
            SET trust = ?, updated_at = ?
            WHERE peer_id = ?
        """, (trust, _now_iso(), peer_id))
        con.commit()
    return get_peer(peer_id, astor_dir=astor_dir)


def record_rekey(
    old_peer_id: str,
    new_peer_id: str,
    signature: str,
    sender_pubkey: str,
    *,
    status: str = "pending",
    note: str | None = None,
    astor_dir: str | None = None,
) -> int:
    """Record a rekey event in the log. Returns the rekey_log id."""
    if status not in ("pending", "auto_accepted", "manual_pending", "rejected"):
        raise ValueError(f"invalid status: {status!r}")
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        cur = con.execute("""
            INSERT INTO rekey_log
            (old_peer_id, new_peer_id, signature, applied_at, sender_pubkey,
             status, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            old_peer_id, new_peer_id, signature, _now_iso(),
            sender_pubkey, status, note,
        ))
        con.commit()
    return cur.lastrowid or 0


def update_rekey_status(
    rekey_id: int, status: str, note: str | None = None,
    astor_dir: str | None = None,
) -> None:
    """Update a rekey log entry's status (e.g. after auto-accept)."""
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        if note is not None:
            con.execute("""
                UPDATE rekey_log SET status = ?, note = ?
                WHERE id = ?
            """, (status, note, rekey_id))
        else:
            con.execute("""
                UPDATE rekey_log SET status = ?
                WHERE id = ?
            """, (status, rekey_id))
        con.commit()


def get_rekey_log(
    *, status: str | None = None, astor_dir: str | None = None,
) -> list[dict]:
    """List rekey log entries, optionally filtered by status."""
    con = _get_conn(astor_dir)
    sql = "SELECT * FROM rekey_log"
    args = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC"
    rows = con.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def apply_rekey(
    rekey_id: int, astor_dir: str | None = None,
) -> dict | None:
    """Apply a rekey: update peer_relationships to point to new peer_id.

    Strategy (per fact 3249 / 3250):
    - Find all relationships pointing to old_peer_id
    - Append old_peer_id to rekey_chain
    - Rename peer_id column to new_peer_id
    - Preserve trust, alias, kind, public_key, topic_weights
    """
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        entry = con.execute(
            "SELECT * FROM rekey_log WHERE id = ?",
            (rekey_id,)
        ).fetchone()
        if not entry:
            return None
        old_pid = entry["old_peer_id"]
        new_pid = entry["new_peer_id"]
        # Find existing relationship with old peer_id
        old_rel = con.execute(
            "SELECT * FROM peer_relationships WHERE peer_id = ?",
            (old_pid,)
        ).fetchone()
        if not old_rel:
            # No existing relationship — nothing to rename
            return {"rekey_id": rekey_id, "applied": False,
                    "reason": "no existing relationship"}
        # Build new rekey_chain: existing chain + old peer_id
        existing_chain = []
        if old_rel["rekey_chain"]:
            try:
                existing_chain = json.loads(old_rel["rekey_chain"])
                if not isinstance(existing_chain, list):
                    existing_chain = []
            except Exception:
                existing_chain = []
        if old_pid not in existing_chain:
            existing_chain.append(old_pid)
        # Insert new row, delete old row, in transaction
        con.execute("""
            INSERT OR REPLACE INTO peer_relationships
            (peer_id, alias, kind, trust, public_key, added_at, updated_at,
             rekey_chain, topic_weights, last_sync, last_topic_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            new_pid,
            old_rel["alias"],
            old_rel["kind"],
            old_rel["trust"],
            entry["sender_pubkey"],  # use the new public key from rekey msg
            old_rel["added_at"],
            _now_iso(),
            json.dumps(existing_chain),
            old_rel["topic_weights"],
            old_rel["last_sync"],
            old_rel["last_topic_seen"],
            old_rel["metadata"],
        ))
        con.execute(
            "DELETE FROM peer_relationships WHERE peer_id = ?",
            (old_pid,)
        )
        con.commit()
    return {
        "rekey_id": rekey_id,
        "applied": True,
        "old_peer_id": old_pid,
        "new_peer_id": new_pid,
        "trust_preserved": old_rel["trust"] if old_rel else None,
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert sqlite3.Row to dict, parsing JSON columns."""
    d = dict(row)
    for k in ("topic_weights", "metadata", "rekey_chain"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


# ---------------------------------------------------------------------------
# v1.14.70 (2026-09-17) — Topic-aware routing (S1)
# ---------------------------------------------------------------------------


def set_topic(
    topic: str,
    peer_id: str,
    weight: float = 1.0,
    *,
    source: str = "manual",
    astor_dir: str | None = None,
) -> dict:
    """Set or update a (topic, peer_id) entry in topic_index.

    weight 0.0-1.0 controls routing preference. Higher = more relevant.
    Use 1.0 for fully trusted topic, 0.5 for partially trusted, 0.0 to disable.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must be 0.0-1.0; got {weight}")
    con = _get_conn(astor_dir)
    now = _now_iso()
    with _RELATIONSHIPS_LOCK:
        con.execute("""
            INSERT INTO topic_index (topic, peer_id, weight, last_seen_at, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (topic, peer_id) DO UPDATE SET
                weight = excluded.weight,
                last_seen_at = excluded.last_seen_at,
                source = excluded.source
        """, (topic, peer_id, weight, now, source))
        con.commit()
    return get_topic(topic, peer_id, astor_dir=astor_dir) or {}


def get_topic(
    topic: str,
    peer_id: str,
    astor_dir: str | None = None,
) -> dict | None:
    """Get a single (topic, peer_id) entry."""
    con = _get_conn(astor_dir)
    row = con.execute("""
        SELECT * FROM topic_index WHERE topic = ? AND peer_id = ?
    """, (topic, peer_id)).fetchone()
    return dict(row) if row else None


def list_topics_for_peer(
    peer_id: str,
    *,
    min_weight: float | None = None,
    astor_dir: str | None = None,
) -> list[dict]:
    """List all topics for a peer, optionally filtered by min weight."""
    con = _get_conn(astor_dir)
    sql = "SELECT * FROM topic_index WHERE peer_id = ?"
    args = [peer_id]
    if min_weight is not None:
        sql += " AND weight >= ?"
        args.append(min_weight)
    sql += " ORDER BY weight DESC, last_seen_at DESC"
    rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_peers_for_topic(
    topic: str,
    *,
    min_weight: float | None = None,
    astor_dir: str | None = None,
) -> list[dict]:
    """List all peers that have this topic, optionally filtered by min weight.

    This is the core 'topic-aware routing' query: given a topic, which peers
    are relevant and how strongly?
    """
    con = _get_conn(astor_dir)
    sql = "SELECT * FROM topic_index WHERE topic = ?"
    args = [topic]
    if min_weight is not None:
        sql += " AND weight >= ?"
        args.append(min_weight)
    sql += " ORDER BY weight DESC, fact_count DESC"
    rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_all_topics(
    *,
    min_weight: float | None = None,
    astor_dir: str | None = None,
) -> list[dict]:
    """List all topics across all peers (for /v1/read topic discovery)."""
    con = _get_conn(astor_dir)
    sql = """
        SELECT topic,
               COUNT(DISTINCT peer_id) AS peer_count,
               AVG(weight) AS avg_weight,
               MAX(weight) AS max_weight,
               MAX(last_seen_at) AS most_recent
        FROM topic_index
        WHERE 1=1
    """
    args = []
    if min_weight is not None:
        sql += " AND weight >= ?"
        args.append(min_weight)
    sql += " GROUP BY topic ORDER BY avg_weight DESC, peer_count DESC"
    rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def bump_topic_seen(
    topic: str,
    peer_id: str,
    astor_dir: str | None = None,
) -> None:
    """Bump last_seen_at and fact_count for a (topic, peer_id) entry.

    Called when a fact for this topic arrives from this peer. Idempotent
    if the entry doesn't exist (no-op, since manual seeding is required).
    """
    con = _get_conn(astor_dir)
    now = _now_iso()
    with _RELATIONSHIPS_LOCK:
        con.execute("""
            UPDATE topic_index
            SET last_seen_at = ?, fact_count = fact_count + 1
            WHERE topic = ? AND peer_id = ?
        """, (now, topic, peer_id))
        con.commit()


def remove_topic(
    topic: str,
    peer_id: str,
    astor_dir: str | None = None,
) -> bool:
    """Remove a (topic, peer_id) entry. Returns True if removed."""
    con = _get_conn(astor_dir)
    with _RELATIONSHIPS_LOCK:
        cur = con.execute("""
            DELETE FROM topic_index WHERE topic = ? AND peer_id = ?
        """, (topic, peer_id))
        con.commit()
    return cur.rowcount > 0

