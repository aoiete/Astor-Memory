"""episodes.py — v1.16.12 (2026-09-30)

M-flow-inspired L0 Episode layer for astor memory.

Source: wechat article "受生物启发的认知记忆引擎 M-flow"
(https://mp.weixin.qq.com/s/gYqseaZH1MlJA3b6CFwxFQ).

M-flow uses a 4-layer cone: Episode → Facet → FacetPoint → Entity.
astor already has L1 (memory_canonical facts), L2 (memory_experience),
L3 (mental_model). Missing: L0 — raw conversation chunks.

Why L0 matters: when a fact is recalled, the user often needs the
EVIDENCE (the original turn where it was said). Without L0 we can
only return the derived fact, not the source. This module:

  - Stores raw conversation chunks (turns) in the `episodes` table.
  - Each episode can be linked to derived_fact_ids (the L1 facts
    it gave rise to) via JSON array.
  - `/v1/episode` endpoint to write/read/list episodes.
  - `/v1/write` opt-in (`record_episode: True`): after a successful
    fact write, create an episode pointing to the new fact_id.

The episode layer is OPT-IN. Default OFF. Reason: most callers don't
need raw conversation stored — just the derived fact. Episode
storage adds DB writes per turn (storage cost) for evidence retrieval
(useful for debugging + "where did I say that?").

Design notes:
  - Episodes use the SAME 3-tier isolation as memory_canonical
    (namespace, user_id, tier). Cross-tier leakage impossible.
  - derived_fact_ids stored as JSON array (text column). On read,
    caller resolves via /v1/bitemporal/lifecycle/<fact_id>.
  - entities_json is optional — populated by LLM-extracted entities
    if available; empty otherwise. (We don't force extraction here.)
  - Embedding column is BLOB for future vector recall. Not populated
    in v1.16.12 (caller can populate later via apply_embedding_cron).

Cost: ~1-2ms per episode write (single INSERT). ~5ms per read.

Migration: v14 → v15 adds the `episodes` table. See schema.py.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable


def write_episode(
    conn: sqlite3.Connection,
    raw_text: str,
    namespace: str = "public",
    user_id: str = "",
    tier: str = "public",
    session_id: str = "",
    derived_fact_ids: list[int] | None = None,
    entities: list[dict] | None = None,
) -> int:
    """Write a raw conversation chunk as an episode.

    Args:
        conn: SQLite connection (must be bus public/private db).
        raw_text: the actual conversation text.
        namespace / user_id / tier: 3-tier isolation (must match caller's scope).
        session_id: optional session correlation id.
        derived_fact_ids: list of fact_ids derived from this chunk.
        entities: optional list of {'type': ..., 'value': ...} dicts.

    Returns the new episode id.
    """
    cur = conn.execute(
        """
        INSERT INTO episodes (
            namespace, user_id, tier, session_id, raw_text,
            derived_fact_ids, entities_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            namespace,
            user_id,
            tier,
            session_id,
            raw_text,
            json.dumps(derived_fact_ids or [], ensure_ascii=False),
            json.dumps(entities or [], ensure_ascii=False),
        ),
    )
    return cur.lastrowid


def read_episode(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    """Fetch a single episode by id."""
    row = conn.execute(
        "SELECT id, namespace, user_id, tier, session_id, raw_text, "
        "derived_fact_ids, entities_json, created_at "
        "FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "namespace": row[1],
        "user_id": row[2],
        "tier": row[3],
        "session_id": row[4],
        "raw_text": row[5],
        "derived_fact_ids": json.loads(row[6] or "[]"),
        "entities_json": json.loads(row[7] or "[]"),
        "created_at": row[8],
    }


def list_episodes(
    conn: sqlite3.Connection,
    namespace: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """List episodes filtered by namespace/user/session."""
    where = []
    params: list = []
    if namespace:
        where.append("namespace = ?")
        params.append(namespace)
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id, namespace, user_id, tier, session_id, raw_text, "
        f"derived_fact_ids, entities_json, created_at "
        f"FROM episodes{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [
        {
            "id": r[0],
            "namespace": r[1],
            "user_id": r[2],
            "tier": r[3],
            "session_id": r[4],
            "raw_text": r[5],
            "derived_fact_ids": json.loads(r[6] or "[]"),
            "entities_json": json.loads(r[7] or "[]"),
            "created_at": r[8],
        }
        for r in rows
    ]


def link_fact_to_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    fact_id: int,
) -> None:
    """Append a fact_id to an episode's derived_fact_ids list.

    Used by /v1/write's record_episode hook after a fact is successfully
    inserted. Idempotent (no duplicate fact_ids).
    """
    row = conn.execute(
        "SELECT derived_fact_ids FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if not row:
        return
    ids = json.loads(row[0] or "[]")
    if fact_id in ids:
        return
    ids.append(fact_id)
    conn.execute(
        "UPDATE episodes SET derived_fact_ids = ? WHERE id = ?",
        (json.dumps(ids, ensure_ascii=False), episode_id),
    )


def find_episodes_by_fact(
    conn: sqlite3.Connection,
    fact_id: int,
) -> list[dict]:
    """Find all episodes that derive to a given fact_id."""
    rows = conn.execute(
        "SELECT id, namespace, user_id, tier, session_id, raw_text, "
        "derived_fact_ids, entities_json, created_at "
        "FROM episodes WHERE instr(derived_fact_ids, ?) > 0 "
        "ORDER BY created_at DESC",
        (str(fact_id),),
    ).fetchall()
    return [
        {
            "id": r[0],
            "namespace": r[1],
            "user_id": r[2],
            "tier": r[3],
            "session_id": r[4],
            "raw_text": r[5],
            "derived_fact_ids": json.loads(r[6] or "[]"),
            "entities_json": json.loads(r[7] or "[]"),
            "created_at": r[8],
        }
        for r in rows
    ]


__all__ = [
    "write_episode",
    "read_episode",
    "list_episodes",
    "link_fact_to_episode",
    "find_episodes_by_fact",
]
