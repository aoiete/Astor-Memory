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

# v1.10.8 (2026-08-26): canonical provenance_kind enum. Previously
# only documented in docstring; auto_link wrote 'auto_link' which was
# missing, and versioning.py had no place to write 'restored'. Centralize
# here so reflection/auto_link/versioning/provenance all agree.
PROVENANCE_KINDS = frozenset({
    'rule',          # user-defined explicit rule
    'extracted',     # forge regex/llm produced this fact
    'inferred',      # derivation via graph traversal
    'manual',        # user-inserted via /v1/write directly
    'merged',        # reflection merge winner
    'auto_link',     # auto_link.py added an edge
    'restored',      # versioning.py restore_fact()
    'reflection_merge',  # legacy alias for 'merged'
})
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

    v1.10.8 (2026-08-26): use json_each() instead of LIKE matching. Previous
    implementation searched for `[{parent_id}]` and `,{parent_id}` substrings,
    but json.dumps([1, 2]) produces `'[1, 2]'` (comma + space). That meant
    multi-element arrays like `[23, 1]` or `[1, 23]` silently failed to
    match the parent_id in non-first position — only single-element arrays
    were reliably found. json_each unrolls the JSON array per row, then a
    simple `je.value = ?` filters correctly regardless of position or
    whitespace. Available since SQLite 3.38, which is universally shipped
    in modern systems.
    """
    conn = _open_conn(tier, user_id)
    try:
        rows = conn.execute(
            "SELECT m.id, m.content, m.kind, m.parent_fact_ids, "
            "m.provenance_kind, m.provenance_agent, m.provenance_depth, "
            "m.promoted_at "
            "FROM memory_canonical m, json_each(m.parent_fact_ids) je "
            "WHERE je.value = ? "
            "ORDER BY m.id",
            (int(parent_id),),
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
                        seen.add(cur_id)  # ensure not re-walked
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
    # v1.10.8 (2026-08-26): MERGE into existing parent_fact_ids instead of
    # overwriting. Previously `SET parent_fact_ids = ?` would wipe out any
    # auto_link edges auto_link.py had added. Now we read existing parents,
    # union with new parents, dedupe, sort, then UPDATE.
    row = bus.conn.execute(
        "SELECT parent_fact_ids, provenance_kind, provenance_agent "
        "FROM memory_canonical WHERE id = ?",
        (int(fact_id),),
    ).fetchone()
    existing_parents = []
    if row and row[0]:
        try:
            existing_parents = json.loads(row[0])
        except Exception:
            existing_parents = []
    merged = sorted(set(int(p) for p in existing_parents) | set(int(p) for p in parents))
    merged_json = json.dumps(merged)
    # v1.10.8: also preserve existing provenance_kind/agent if set, same as
    # auto_link fix. Only update if both columns are currently empty.
    existing_kind = (row[1] if row else None) or ''
    existing_agent = (row[2] if row else None) or ''
    final_kind = existing_kind or kind
    final_agent = existing_agent or agent
    bus.conn.execute(
        "UPDATE memory_canonical SET "
        "parent_fact_ids = ?, "
        "provenance_kind = ?, "
        "provenance_agent = ?, "
        "provenance_depth = ?, "
        "provenance_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ?",
        (merged_json, final_kind, final_agent, int(depth), fact_id),
    )
    bus.conn.commit()
    # v1.10.8 (2026-08-26): write audit row so provenance changes are traceable.
    # Previously the system claimed "audit-safe" but provenance was the one path
    # that bypassed write_audit.
    try:
        bus.write_audit(
            event='provenance_recorded',
            actor='nest.provenance',
            target_type='fact',
            target_id=str(fact_id),
            new_state={'parents': merged, 'kind': final_kind,
                       'agent': final_agent, 'depth': int(depth)},
            reason=f'record_provenance: {len(parents)} new parent(s)',
            severity='info',
        )
    except Exception as _e:
        # Never block on audit failures; log only
        import sys as _sys_p
        print(f'[astor.provenance] audit log failed (non-fatal): {_e}',
              file=_sys_p.stderr)
    return {
        'fact_id': fact_id, 'parents': merged,
        'provenance_kind': final_kind, 'provenance_agent': final_agent,
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

    v1.10.8 (2026-08-26):
      - Draw chain-style edges instead of star: each ancestor's parent
        is its direct upstream (not the root). Fixes depth>1 chains being
        rendered as a hub-and-spoke pattern that misleads visual debugging.
      - Guard against fact=None when ancestors exist (previous code crashed
        with `f{fact["id"]}` on a None fact).
    """
    out = ['digraph provenance {', '  rankdir=LR;',
           f'  node [shape=record, style="filled,rounded", fillcolor=white];']

    def _node_label(fid: int, kind, agent) -> str:
        k = kind if kind is not None else '?'
        a = agent if agent is not None else '?'
        return f'  f{fid} [label="{{{fid}|{k}|{a}}}"];'

    # v1.10.8: maintain a depth-indexed map of rendered fact_ids to draw
    # chain-style edges (parent -> grandparent -> ..., not root -> everyone).
    rendered: dict[int, int] = {}  # fact_id -> parent_id (for chain edges)

    if direction in ('up', 'both'):
        up = get_provenance(fact_id, tier=tier, user_id=user_id,
                            max_depth=max_depth)
        fact = up.get('fact')
        if fact:
            out.append(_node_label(fact['id'],
                                    fact.get('provenance_kind'),
                                    fact.get('provenance_agent')))
            rendered[fact['id']] = fact_id  # root has no parent
        for a in up.get('ancestors', []):
            af = a['fact']
            if af is None:
                continue
            out.append(_node_label(af['id'],
                                    af.get('provenance_kind'),
                                    af.get('provenance_agent')))
            # v1.10.8: draw edge from ancestor's depth-parent, not from root.
            # If we don't know the ancestor's parent, draw from the previous
            # depth-1 ancestor in this walk (chain-style fallback).
            par_id = a.get('parent_id')
            if par_id and par_id in rendered:
                out.append(f'  f{par_id} -> f{af["id"]} [label="parent depth={a["depth"]}"];')
            elif par_id is None:
                # parent_id not present in returned dict — fall back to root
                root = fact_id if fact else par_id
                if root:
                    out.append(f'  f{root} -> f{af["id"]} [label="parent depth={a["depth"]}"];')
            rendered[af['id']] = par_id or 0

    if direction in ('down', 'both'):
        # v1.10.8: make sure `fact` is populated before drawing descendant edges
        if 'fact' not in locals() or fact is None:
            try:
                fact = _read_fact(tier, user_id, fact_id)
                if fact:
                    out.append(_node_label(fact['id'],
                                            fact.get('provenance_kind'),
                                            fact.get('provenance_agent')))
            except Exception:
                pass
        down = get_lineage(fact_id, tier=tier, user_id=user_id,
                            max_depth=max_depth)
        prev_depth_node: dict[int, int] = {}  # depth -> most recent fact_id
        if fact:
            prev_depth_node[0] = fact['id']
        for d in down.get('descendants', []):
            fid = d.get('fact_id')
            if fid is None:
                continue
            out.append(_node_label(fid,
                                    d.get('provenance_kind'),
                                    d.get('agent')))
            # v1.10.8: chain edge from depth-1 ancestor (parent), not from root
            par_depth = d['depth'] - 1
            if par_depth in prev_depth_node:
                par_id = prev_depth_node[par_depth]
                out.append(f'  f{par_id} -> f{fid} [label="child depth={d["depth"]}"];')
            elif fact:
                # fallback when no parent at depth-1 (root only)
                out.append(f'  f{fact["id"]} -> f{fid} [label="child depth={d["depth"]}"];')
            # Track this fact as the latest at its depth
            if d['depth'] in prev_depth_node:
                prev_depth_node[d['depth']] = fid
            else:
                prev_depth_node[d['depth']] = fid
    out.append('}')
    return "\n".join(out)
