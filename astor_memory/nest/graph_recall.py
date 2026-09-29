"""graph_recall.py — Hindsight-style graph recall path (Ship P1.2).

v1.15.37: query-time retrieval using structured entities_json column.
Hindsight's 4-path recall (vector + BM25 + graph + temporal) — astor had
vector+BM25 only. Graph recall = "find all facts mentioning entity E"
via json_each index on memory_canonical.entities_json.

vs. stage_recall_rerank (content-based regex, v1.10.9):
- Uses structured entities_json (auto-extracted by forge.extractor)
- Pure SQL (json_each + json_extract) → <2ms for 540+ facts
- Returns ENTITY EDGES not just ranked facts — caller can use them
  to expand the recall set (e.g. user asks "about Hermes" → find all
  facts mentioning Hermes entity → return as evidence).

Endpoint:
  GET /v1/graph_recall?entity=Hermes&tier=public&top_k=20
    → {"entity": "Hermes", "edges": [...], "facts": [...]}

  edges: list of (source_fid, entity_value, entity_type, target_fid)
  facts: top-K facts mentioning this entity (joined back to memory_canonical)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def graph_recall_by_entity(
    bus_conn: sqlite3.Connection,
    entity: str,
    tier: str = "public",
    user_id: str | None = None,
    top_k: int = 20,
    entity_types: list[str] | None = None,
) -> dict[str, Any]:
    """Return facts + edges mentioning `entity` (case-insensitive).

    v1.15.37: pure SQL via json_each on entities_json. Returns the joined
    memory_canonical rows so the caller can show the fact content + the
    list of edges (source_fid, entity_value, target_fid).

    Args:
      bus_conn: astor bus sqlite connection (memory_canonical table).
      entity: entity value (e.g. "Hermes", "moomoo"). Case-insensitive
        substring match on json_extract(entity, '$.value').
      tier: public / private_<user> / source. (private_<user> bus_conn
        is opened by caller.)
      user_id: optional user filter (None = any user).
      top_k: max facts returned.
      entity_types: filter to entity types (e.g. ['person', 'org']).
        None = all types.

    Returns:
      {"entity": str, "count": int, "edges": [...], "facts": [...]}
    """
    entity_pattern = f"%{entity}%"
    # Build entity_types filter — when None, match all (LIKE not applied).
    # json_each unrolls the JSON array; json_extract pulls .value and .type
    # directly in the WHERE clause (no FROM alias — SQLite JSON1 doesn't
    # accept function names as table aliases).
    base_sql = """
        SELECT
            c.id AS fact_id,
            c.content,
            c.tier,
            c.user_id,
            c.kind,
            c.confidence,
            c.promoted_at,
            json_extract(je.value, '$.value') AS entity_value,
            json_extract(je.value, '$.type') AS entity_type
        FROM memory_canonical c,
             json_each(c.entities_json) AS je
        WHERE c.tombstoned = 0
          AND c.kind != 'mental_model'
          AND LOWER(json_extract(je.value, '$.value')) LIKE LOWER(?)
          AND c.tier = ?
    """
    params: list[Any] = [entity_pattern, tier]

    if user_id is not None:
        base_sql += " AND (c.user_id IS NULL OR c.user_id = ?)"
        params.append(user_id)

    if entity_types:
        type_placeholders = ",".join("?" for _ in entity_types)
        base_sql += (
            f" AND json_extract(je.value, '$.type') IN ({type_placeholders})"
        )
        params.extend(entity_types)

    base_sql += " ORDER BY c.promoted_at DESC LIMIT ?"
    params.append(int(top_k))

    rows = bus_conn.execute(base_sql, params).fetchall()

    facts = []
    edges = []
    seen_fids: set[int] = set()
    for row in rows:
        fid = int(row[0])
        facts.append({
            "fact_id": fid,
            "content": row[1] or "",
            "tier": row[2],
            "user_id": row[3],
            "kind": row[4],
            "confidence": float(row[5] or 0.5),
            "promoted_at": row[6],
            "matched_entity": row[7],
            "matched_entity_type": row[8],
        })
        edges.append({
            "source_fact_id": fid,
            "entity_value": row[7],
            "entity_type": row[8],
        })
        seen_fids.add(fid)

    return {
        "entity": entity,
        "tier": tier,
        "count": len(seen_fids),
        "edges": edges,
        "facts": facts,
    }


def list_entities_by_freq(
    bus_conn: sqlite3.Connection,
    tier: str = "public",
    entity_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the top-N most-referenced entity values in this tier.

    v1.15.37: helps the operator understand which entities the bus
    is tracking. Pure SQL via json_each + GROUP BY.

    Args:
      bus_conn: astor bus sqlite connection.
      tier: public/private/source.
      entity_type: optional filter (e.g. 'person'). None = all.
      limit: max results.

    Returns:
      list of {entity_value, entity_type, fact_count}
    """
    sql = """
        SELECT
            json_extract(je.value, '$.value') AS entity_value,
            json_extract(je.value, '$.type') AS entity_type,
            COUNT(DISTINCT c.id) AS fact_count
        FROM memory_canonical c,
             json_each(c.entities_json) AS je
        WHERE c.tombstoned = 0
          AND c.tier = ?
    """
    params: list[Any] = [tier]

    if entity_type:
        sql += " AND json_extract(je.value, '$.type') = ?"
        params.append(entity_type)

    sql += " GROUP BY entity_value, entity_type ORDER BY fact_count DESC LIMIT ?"
    params.append(int(limit))

    rows = bus_conn.execute(sql, params).fetchall()
    return [
        {"entity_value": r[0], "entity_type": r[1], "fact_count": int(r[2])}
        for r in rows
    ]
