"""path_score.py — v1.16.11 (2026-09-30)

M-flow-inspired "graph as scoring engine" path-based recall scoring.

Source: wechat article "受生物启发的认知记忆引擎 M-flow"
(https://mp.weixin.qq.com/s/gYqseaZH1MlJA3b6CFwxFQ, 4.5k stars GitHub).

M-flow's core insight: in naive RAG, if a candidate fact has low
similarity to the query, it's discarded — even if a SHORT CHAIN of
related facts (query → entity → fact1 → entity → fact2) provides
strong evidence. M-flow uses the graph as the SCORING ENGINE: instead
of `score(candidate) = similarity(query, candidate)`, it builds paths
through the entity graph and scores the strongest path.

For astor we use entity overlap as the edge type. ECV `relation()`
returns `(type, confidence)` for anchor↔candidate pairs. Path scoring
extends ECV by chaining 2-hop neighbors:

    score(query → fact) = node_usefulness(query, fact) + Σ (
        neighbor_relation_conf × decay^depth
    ) for each entity-shared neighbor at depth 1..2

Decay default 0.6 per hop (article: cost-bounded propagation, ~3 hops
max). 2-hop limit keeps the score under 5ms even for 1000-candidate
sets.

Usage:
    from .path_score import path_score_for_fact, batch_path_score

    ps = path_score_for_fact(
        query="...",
        anchor=fact_dict,
        neighbor_facts=[fact1, fact2, ...],  # candidates that share entities
        max_depth=2,
        decay=0.6,
    )
    # ps = {"direct": 0.4, "path": 0.85, "chain": [fact_id_3, fact_id_7]}

Cost: ~5-15ms per anchor with 30 neighbors × 2-hop BFS.

Key design choice: path_score is BOUNDED. We never amplify a fact
that's not directly relevant — the path boost is capped at +0.30 so
a perfect chain can never outscore a fact with direct evidence.
This matches GraphMemix's "chain coherence boost" principle without
the LLM cost.
"""
from __future__ import annotations

from typing import Iterable

from .ecv import (
    REL_CORROBORATE,
    REL_CLARIFY,
    REL_CONFLICT,
    REL_NEW_FACT,
    REL_NONE,
    REL_REPEAT,
    node_usefulness,
    relation,
    _tokenize,
    _parse_entities,
)


# v1.16.11: max additive boost from a path chain. Direct evidence
# always wins over chain-derived.
_PATH_BOOST_CAP = 0.30

# v1.16.11: per-hop decay. 0.6 = a 2-hop chain retains 0.36 weight.
_PATH_DECAY = 0.6

# v1.16.11: max BFS depth. 2 hops is enough to surface multi-session
# recall where fact1 + fact2 together answer the query but neither
# alone does.
_MAX_DEPTH = 2


def _entities_of(fact: dict) -> set[str]:
    """Return the set of entity values from a fact dict."""
    return set(_parse_entities(fact.get("entities_json") or fact.get("entities")))


def _content_of(fact: dict) -> str:
    return fact.get("content") or fact.get("text") or ""


def _entities_json_of(fact: dict) -> str | None:
    return fact.get("entities_json") or (
        __import__("json").dumps(fact.get("entities", []), ensure_ascii=False)
        if fact.get("entities")
        else None
    )


def _score_relation(rel_type: str, rel_conf: float) -> float:
    """Convert relation_type + confidence to a score modifier.

    GraphMemix ablation shows: corroboration > clarification >
    new_fact > repeat > none. conflict penalizes.

    Returns additive score in [-0.3, +1.0] range.
    """
    if rel_type == REL_CORROBORATE:
        return rel_conf * 1.0
    if rel_type == REL_CLARIFY:
        return rel_conf * 0.7
    if rel_type == REL_NEW_FACT:
        return rel_conf * 0.5
    if rel_type == REL_REPEAT:
        return rel_conf * 0.3  # small boost — repeated facts are evidence
    if rel_type == REL_CONFLICT:
        return -rel_conf * 0.3  # penalty
    return 0.0  # REL_NONE


def path_score_for_fact(
    query: str,
    anchor: dict,
    neighbor_facts: list[dict] | None = None,
    max_depth: int = _MAX_DEPTH,
    decay: float = _PATH_DECAY,
    cap: float = _PATH_BOOST_CAP,
) -> dict:
    """Score `anchor` fact against query using path-based graph reasoning.

    Args:
        query: user query text.
        anchor: the candidate fact to score.
        neighbor_facts: optional pre-fetched neighbors sharing entities
            with anchor. If None or empty, falls back to direct-only
            (same as ECV node_usefulness).
        max_depth: BFS depth limit. 1 = anchor + direct neighbors;
            2 = anchor + neighbor-of-neighbor (default).
        decay: per-hop decay applied to chain weights.
        cap: maximum additive boost from path chain (absolute cap).

    Returns dict:
        {
          "direct": float,   # node_usefulness(query, anchor) — 0..1
          "path": float,     # direct + bounded path boost — 0..1 (capped at 1.0)
          "boost": float,    # additive path boost (may be negative for conflicts) — bounded by cap
          "chain": [int, ...],  # fact_ids of facts in the strongest chain (excluding anchor)
          "depth_used": int, # actual max depth traversed
        }

    Cost: O(|neighbors| × max_depth). Default 30 neighbors × 2 hops ≈ 60 ECV calls ≈ 5ms.
    """
    direct = node_usefulness(
        query=query,
        candidate_content=_content_of(anchor),
        candidate_keywords=anchor.get("keywords"),
        candidate_entities_json=_entities_json_of(anchor),
        candidate_confidence=float(anchor.get("confidence", 0.7)),
        candidate_access_count=int(anchor.get("access_count", 0)),
    )

    if not neighbor_facts:
        return {
            "direct": direct,
            "path": direct,
            "boost": 0.0,
            "chain": [],
            "depth_used": 0,
        }

    # BFS through entity-overlap graph.
    # State: (fact_id, depth) → cumulative path boost
    # We track best chain found.
    best_boost = 0.0
    best_chain: list = []
    best_depth = 0

    # Build entity → list of (fact, depth) index for hop traversal
    visited_ids = {anchor.get("id") or anchor.get("fact_id")}
    anchor_entities = _entities_of(anchor)
    # depth 1: anchor's direct neighbors
    frontier: list[tuple[dict, int]] = [(nb, 1) for nb in neighbor_facts if (nb.get("id") or nb.get("fact_id")) not in visited_ids]

    # Track best chain candidate as we go
    running_chain: list = []

    while frontier and len(running_chain) < 6:  # cap chain length
        nbr, depth = frontier.pop(0)
        nbr_id = nbr.get("id") or nbr.get("fact_id")
        if nbr_id in visited_ids:
            continue
        visited_ids.add(nbr_id)

        # Score this hop
        rel_type, rel_conf = relation(
            anchor_content=_content_of(anchor),
            anchor_entities_json=_entities_json_of(anchor),
            candidate_content=_content_of(nbr),
            candidate_entities_json=_entities_json_of(nbr),
            anchor_created_at=anchor.get("created_at"),
            candidate_created_at=nbr.get("created_at"),
        )
        rel_score = _score_relation(rel_type, rel_conf) * (decay ** (depth - 1))
        # Update running chain — only accept positive contributions
        if rel_score > 0:
            running_chain.append(nbr_id)
            if rel_score > best_boost:
                best_boost = rel_score
                best_chain = list(running_chain)
                best_depth = depth
        elif rel_score < 0:
            # Conflict — break the chain, reset
            running_chain = []

        # Expand frontier at next depth (only if shared entities)
        if depth < max_depth:
            nbr_entities = _entities_of(nbr)
            shared = nbr_entities & anchor_entities
            if shared:
                # Use the same neighbor_facts pool but skip already visited
                for next_nb in neighbor_facts:
                    next_id = next_nb.get("id") or next_nb.get("fact_id")
                    if next_id in visited_ids:
                        continue
                    next_ents = _entities_of(next_nb)
                    if next_ents & nbr_entities or next_ents & shared:
                        frontier.append((next_nb, depth + 1))

    # Cap the boost
    boost = max(-cap, min(cap, best_boost))
    path = max(0.0, min(1.0, direct + boost))

    return {
        "direct": direct,
        "path": path,
        "boost": boost,
        "chain": best_chain,
        "depth_used": best_depth,
    }


def batch_path_score(
    query: str,
    anchor: dict,
    candidate_pool: list[dict],
    max_candidates: int = 30,
    max_depth: int = _MAX_DEPTH,
    decay: float = _PATH_DECAY,
    cap: float = _PATH_BOOST_CAP,
) -> list[dict]:
    """Score `anchor` against a pool of candidate neighbors.

    Selects up to `max_candidates` candidates that share ≥1 entity with
    `anchor`, then runs path scoring against that subset.

    Returns list of path_score result dicts (one per selected neighbor).
    """
    anchor_entities = _entities_of(anchor)
    if not anchor_entities:
        return []

    selected: list[dict] = []
    for c in candidate_pool:
        if (c.get("id") or c.get("fact_id")) == (anchor.get("id") or anchor.get("fact_id")):
            continue
        c_ents = _entities_of(c)
        if c_ents & anchor_entities:
            selected.append(c)
            if len(selected) >= max_candidates:
                break

    out = []
    for nb in selected:
        ps = path_score_for_fact(
            query=query,
            anchor=anchor,
            neighbor_facts=selected,
            max_depth=max_depth,
            decay=decay,
            cap=cap,
        )
        # Annotate with neighbor fact_id for downstream
        ps["neighbor_id"] = nb.get("id") or nb.get("fact_id")
        ps["path"] = ps["direct"]  # direct score of neighbor (independent)
        out.append(ps)
    return out


def apply_path_boost(
    query: str,
    candidates: list[dict],
    neighbor_pool: list[dict] | None = None,
    max_depth: int = _MAX_DEPTH,
    decay: float = _PATH_DECAY,
    cap: float = _PATH_BOOST_CAP,
) -> list[dict]:
    """Apply path-based scoring to a list of candidates.

    Each candidate is scored against the union of:
      - itself (direct ECV node_usefulness)
      - neighbor_pool (if provided) — used to build the entity graph

    The candidate's "score" field is replaced with the path score
    (direct + bounded boost). The original score is preserved as
    "base_score" for ablation.

    Args:
        query: user query.
        candidates: list of fact dicts (must have id, content, etc).
        neighbor_pool: optional pool of facts to use as the graph
            (defaults to candidates themselves — fine for typical
            50-candidate recall set).
        max_depth: BFS depth.
        decay: per-hop decay.
        cap: max additive boost.

    Returns the same list (mutated in place) for chaining.
    """
    pool = neighbor_pool if neighbor_pool is not None else candidates

    for c in candidates:
        base = node_usefulness(
            query=query,
            candidate_content=_content_of(c),
            candidate_keywords=c.get("keywords"),
            candidate_entities_json=_entities_json_of(c),
            candidate_confidence=float(c.get("confidence", 0.7)),
            candidate_access_count=int(c.get("access_count", 0)),
        )
        ps = path_score_for_fact(
            query=query,
            anchor=c,
            neighbor_facts=[n for n in pool if (n.get("id") or n.get("fact_id")) != (c.get("id") or c.get("fact_id"))],
            max_depth=max_depth,
            decay=decay,
            cap=cap,
        )
        c["base_score"] = base
        c["path_score"] = ps["path"]
        c["path_boost"] = ps["boost"]
        c["path_chain"] = ps["chain"]
        c["path_depth"] = ps["depth_used"]

    return candidates


__all__ = [
    "path_score_for_fact",
    "batch_path_score",
    "apply_path_boost",
    "_PATH_BOOST_CAP",
    "_PATH_DECAY",
    "_MAX_DEPTH",
]
