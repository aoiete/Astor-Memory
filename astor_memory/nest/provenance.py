"""
Per-fact provenance graph (2026-08-16 opt4).

Each memory_canonical fact carries:
  - parent_fact_ids   : JSON list of fact_ids this fact was derived from
  - provenance_kind   : 'rule' | 'extracted' | 'inferred' | 'manual' | 'merged'
  - provenance_agent  : which producer (e.g. 'forge.regex_v2', 'agent.admin')
  - provenance_depth  : 0 = directly observed, 1+ = derivative depth
  - provenance_at     : when the lineage was last updated

This module exposes:
  - get_provenance(fact_id, ...)     -> upward chain (ancestors)
  - get_lineage(fact_id, ...)        -> downward chain (descendants)
  - record_provenance(fact_id, parents, kind, agent, ...)
  - graph_dot(fact_id, ...) -> Graphviz DOT for visualisation
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .._internal.acl_layout import get_astor_dir, get_db_path


def _bus_path(tier: str, user_id: str | None) -> Path:
    return get_db_path(tier, 'bus', user_id)


def _open_conn(tier: str = 'public', user_id: str | None = None) -> sqlite3.Connection:
    p = _bus_path(tier, user_id)
    if not p.exists():
        raise FileNotFoundError(f'bus db missing: {p}')
    conn = sqlite3.connect(
        f'file:{p}?mode=ro', uri=True,
        check_same_thread=False, timeout=5,
    )
    return conn


def _read_fact(
    tier: str, user_id: str | None, fact_id: int,
) -> Optional[dict]:
    """Read a fact row from bus. Returns None if missing."""
    conn = _open_conn(tier, user_id)
    try:
        row = conn.execute(
            "SELECT id, content, kind, importance, tags, metadata, "
            "event_id, namespace, user_id, promoted_at, "
            "tombstoned, "
            "parent_fact_ids, provenance_kind, provenance_agent, "
            "provenance_depth, provenance_at "
            "FROM memory_canonical WHERE id = ?",
            (fact_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    keys = ('id', 'content', 'kind', 'importance', 'tags', 'metadata',
            'event_id', 'namespace', 'user_id', 'promoted_at',
            'tombstoned',
            'parent_fact_ids', 'provenance_kind', 'provenance_agent',
            'provenance_depth', 'provenance_at')
    rec = dict(zip(keys, row))
    # Parse JSON columns
    try:
        rec['tags'] = json.loads(rec['tags'] or '[]')
    except Exception:
        rec['tags'] = []
    try:
        rec['metadata'] = json.loads(rec['metadata'] or '{}')
    except Exception:
        rec['metadata'] = {}
    try:
        rec['parent_fact_ids'] = json.loads(rec['parent_fact_ids'] or '[]')
    except Exception:
        rec['parent_fact_ids'] = []
    return rec


def _iter_descendants(
    tier: str, user_id: str | None, parent_id: int, *,
    max_depth: int = 8,
) -> list[dict]:
    """Find facts whose parent_fact_ids JSON contains parent_id.

    NOTE: SQLite JSON search requires LIKE on a normalised JSON string,
    which works for small lists. For lists > 100 parents per fact, this
    is slow — but typical usage is 1-3 parents per fact.
    """
    conn = _open_conn(tier, user_id)
    try:
        # Use LIKE with bracket-quoted int — JSON arrays serialise as
        # either "[id]" or "[id, id2, ...]". Both contain the int with
        # comma/bracket delimiters.
        target_str = f'[{parent_id}]'  # exact single-element array
        target_str2 = f',{parent_id}'   # multi-element array
        rows = conn.execute(
            "SELECT id, content, kind, parent_fact_ids, provenance_kind, "
            "provenance_agent, provenance_depth, promoted_at "
            "FROM memory_canonical "
            "WHERE parent_fact_ids LIKE ? OR parent_fact_ids LIKE ? "
            "ORDER BY id",
            (f'%{target_str}%', f'%{target_str2}%'),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        parents = []
        try:
            parents = json.loads(r[3] or '[]')
        except Exception:
            parents = []
        if parent_id not in parents:
            continue
        out.append({
            'id': r[0], 'content': r[1], 'kind': r[2],
            'parent_fact_ids': parents,
            'provenance_kind': r[4],
            'provenance_agent': r[5],
            'provenance_depth': r[6],
            'promoted_at': r[7],
        })
    return out


def get_provenance(
    fact_id: int,
    tier: str = 'public',
    user_id: str | None = None,
    *,
    max_depth: int = 8,
    scope_search: bool = True,
) -> dict:
    """Walk upward from a fact through parent_fact_ids chain.

    Args:
      fact_id: starting fact (it must be in (tier, user_id))
      tier, user_id: tier scope
      max_depth: cap on upward chain depth (loop protection)
      scope_search: also probe adjacent tiers for parent_fact_ids
                    (cross-tier fallback for moved/mirrored facts)

    Returns:
      {
        fact: <fact>,
        ancestors: [
          {fact: <ancestor>, depth: <int>, relation: 'parent'},
          ...
        ],
        event: <source event info or None>,
        chain_broken: bool,
        notes: [str],
      }
    """
    out = {
        'fact_id': fact_id, 'tier': tier, 'user_id': user_id,
        'depth_walked': 0, 'chain_broken': False,
        'ancestors': [], 'event': None, 'notes': [],
    }
    seen = set()
    frontier = [(fact_id, 0)]  # (id, depth)
    scopes_searched = [(tier, user_id)]
    notes = []
    while frontier and len(out['ancestors']) < 64:
        cur_id, cur_depth = frontier.pop(0)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        if cur_id != fact_id:
            # Load this ancestor
            try:
                anc = _read_fact(tier, user_id, cur_id)
            except FileNotFoundError:
                anc = None
            if anc is None and scope_search:
                # Probe adjacent tiers
                found = None
                if tier == 'public':
                    probe = [('source', None), ('private', 'admin')]
                elif tier == 'source':
                    probe = [('public', None)]
                else:
                    probe = [('public', None)]
                for t2, u2 in probe:
                    try:
                        a = _read_fact(t2, u2, cur_id)
                    except FileNotFoundError:
                        a = None
                    if a is not None:
                        anc = a
                        out['notes'].append(
                            f'cross-tier probe: {tier}/{user_id} -> {t2}/{u2}'
                        )
                        scopes_searched.append((t2, u2))
                        break
            if anc is None:
                out['chain_broken'] = True
                notes.append(f'parent {cur_id} not found in any probed scope')
                continue
            out['ancestors'].append({
                'fact': anc,
                'depth': cur_depth,
                'relation': 'parent',
            })
            out['depth_walked'] = max(out['depth_walked'], cur_depth)
        # Read parents of this fact (we need them even on iteration 0)
        try:
            fact_obj = _read_fact(tier, user_id, cur_id)
        except FileNotFoundError:
            continue
        if fact_obj is None:
            continue
        if cur_id == fact_id:
            out['fact'] = fact_obj
            # Capture the source event on the root fact
            try:
                conn = _open_conn(tier, user_id)
                try:
                    row = conn.execute(
                        "SELECT id, ts, source, action, agent_id, content "
                        "FROM events WHERE id = ?",
                        (fact_obj['event_id'],),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    out['event'] = {
                        'id': row[0], 'ts': row[1],
                        'source': row[2], 'action': row[3],
                        'agent_id': row[4],
                        'content_preview': str(row[5] or '')[:120],
                    }
            except Exception:
                pass
        parents = fact_obj.get('parent_fact_ids', [])
        if not parents:
            continue
        if cur_depth >= max_depth:
            out['chain_broken'] = True
            notes.append(f'max_depth ({max_depth}) reached; stopped walk')
            continue
        for p in parents:
            if p in seen:
                continue
            frontier.append((int(p), cur_depth + 1))
    out['scopes_searched'] = scopes_searched
    out['notes'].extend(notes)
    return out


def get_lineage(
    fact_id: int,
    tier: str = 'public',
    user_id: str | None = None,
    *,
    max_depth: int = 8,
) -> dict:
    """Walk downward from a fact, finding facts that derived from it.

    Returns:
      {
        fact_id, tier, user_id,
        descendants: [
          {fact_id, content, kind, depth, agent},
          ...
        ],
        depth_walked: int,
        nodes_visited: int,
      }
    """
    out = {
        'fact_id': fact_id, 'tier': tier, 'user_id': user_id,
        'descendants': [],
        'depth_walked': 0, 'nodes_visited': 0,
    }
    seen = set()
    frontier = [(fact_id, 0)]
    while frontier and len(out['descendants']) < 256:
        cur_id, cur_depth = frontier.pop(0)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        if cur_depth >= max_depth:
            continue
        children = _iter_descendants(tier, user_id, cur_id)
        for c in children:
            out['nodes_visited'] += 1
            out['descendants'].append({
                'fact_id': c['id'],
                'content': str(c['content'])[:120],
                'kind': c['kind'],
                'depth': cur_depth + 1,
                'agent': c['provenance_agent'],
                'provenance_kind': c['provenance_kind'],
                'promoted_at': c['promoted_at'],
            })
            frontier.append((c['id'], cur_depth + 1))
        out['depth_walked'] = max(out['depth_walked'], cur_depth + 1)
    return out


def record_provenance(
    fact_id: int,
    parents: list[int],
    *,
    kind: str = 'extracted',
    agent: str = 'forge.regex_v2',
    depth: int | None = None,
    tier: str = 'public',
    user_id: str | None = None,
) -> dict:
    """Update a fact's provenance metadata in bus.

    If depth is None, we auto-compute it from parents' max depth + 1.
    """
    bus_user = user_id if tier in ('private', 'repo') else None
    from .. import astor_bus
    bus = astor_bus(tier=tier, user_id=bus_user)
    # Compute depth if not given
    if depth is None:
        max_par_depth = -1
        for p in parents:
            row = bus.conn.execute(
                "SELECT provenance_depth FROM memory_canonical WHERE id = ?",
                (int(p),),
            ).fetchone()
            if row is not None:
                max_par_depth = max(max_par_depth, int(row[0] or 0))
        depth = max_par_depth + 1
    parents_json = json.dumps(sorted(set(int(p) for p in parents)))
    bus.conn.execute(
        "UPDATE memory_canonical SET "
        "parent_fact_ids = ?, "
        "provenance_kind = ?, "
        "provenance_agent = ?, "
        "provenance_depth = ?, "
        "provenance_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ?",
        (parents_json, kind, agent, int(depth), fact_id),
    )
    bus.conn.commit()
    return {
        'fact_id': fact_id, 'parents': json.loads(parents_json),
        'provenance_kind': kind, 'provenance_agent': agent,
        'provenance_depth': int(depth),
    }


def graph_dot(
    fact_id: int,
    *,
    direction: str = 'both',  # 'up' | 'down' | 'both'
    tier: str = 'public',
    user_id: str | None = None,
    max_depth: int = 6,
) -> str:
    """Render provenance graph for a fact as Graphviz DOT. Useful for
    debugging lineage visually.

    Args:
      fact_id: root fact
      direction: 'up' (ancestors), 'down' (descendants), 'both'
      max_depth: cap traversal depth
    Returns: a multi-line DOT string.
    """
    out = ['digraph provenance {', '  rankdir=LR;',
           f'  node [shape=record, style="filled,rounded", fillcolor=white];']
    if direction in ('up', 'both'):
        up = get_provenance(fact_id, tier=tier, user_id=user_id,
                            max_depth=max_depth)
        fact = up.get('fact')
        if fact:
            out.append(
                f'  f{fact["id"]} [label="{{{fact["id"]}|{fact.get("provenance_kind","?")}|{fact.get("provenance_agent","?")}}}"];'
            )
        for a in up.get('ancestors', []):
            af = a['fact']
            out.append(
                f'  f{af["id"]} [label="{{{af["id"]}|{af.get("provenance_kind","?")}|{af.get("provenance_agent","?")}}}"];'
            )
            out.append(f'  f{fact["id"]} -> f{af["id"]} [label="parent depth={a["depth"]}"];')
    if direction in ('down', 'both'):
        down = get_lineage(fact_id, tier=tier, user_id=user_id,
                            max_depth=max_depth)
        if 'fact' not in locals() or fact is None:
            try:
                fact = _read_fact(tier, user_id, fact_id)
                if fact:
                    out.append(
                        f'  f{fact["id"]} [label="{{{fact["id"]}|{fact.get("provenance_kind","?")}|{fact.get("provenance_agent","?")}}}"];'
                    )
            except Exception:
                pass
        for d in down.get('descendants', []):
            out.append(
                f'  f{d["fact_id"]} [label="{{{d["fact_id"]}|{d.get("provenance_kind","?")}|{d.get("agent","?")}}}"];'
            )
            out.append(f'  f{fact["id"]} -> f{d["fact_id"]} [label="child depth={d["depth"]}"];')
    out.append('}')
    return "\n".join(out)
