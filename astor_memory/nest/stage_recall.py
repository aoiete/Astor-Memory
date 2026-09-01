"""stage_recall.py — v1.10.9 (2026-08-27)

Stage-wise recall with entity coverage re-ranking. NO LLM call. <5ms
per query.

Why this matters for Multi-hop:
  - LoCoMo multi-hop queries ("A based on B", "How did A and B first meet",
    "According to A, what did B think...") typically mention 2-3 distinct
    named entities (people, topics, events).
  - Astor's hybrid recall can return the right facts in top-30, but
    ranking them by BM25 + cosine alone tends to put the right
    *individual* fact at the top, not the *combination* of all needed
    facts.
  - Re-ranking by entity coverage (does the candidate mention the
    same entities as the query?) promotes facts that co-mention multiple
    entities, which is exactly what multi-hop questions need.

Algorithm:
  1. Take the top-K_candidates (default K=30) from the existing
     hybrid recall.
  2. Extract the set of entities from the query (capitalized tokens +
     a few common role nouns).
  3. For each candidate, compute the fraction of query entities that
     appear (case-insensitive) in its content.
  4. Re-score: `new_score = original_score * (1 + boost_strength *
     entity_coverage)`. boost_strength defaults to 0.5 (gentle).
  5. Return the top top_k facts by new score.
"""
from __future__ import annotations

import re
from typing import Iterable

_ENTITY_RE = re.compile(r"\b([A-Z][a-z][a-zA-Z'-]{2,})\b")
# Common role nouns that should count as entities (signals identity)
_ROLE_TERMS = re.compile(
    r"\b(friend|partner|spouse|husband|wife|child|kid|son|daughter|"
    r"mother|father|mom|dad|sister|brother|boss|teacher|student|"
    r"doctor|counselor|therapist|artist|mentor|colleague|neighbor)\b",
    re.IGNORECASE,
)


def _extract_query_entities(query: str) -> set[str]:
    """Return the set of distinct entity tokens in the query (lowercased)."""
    ents: set[str] = set()
    if not query:
        return ents
    for m in _ENTITY_RE.findall(query):
        ents.add(m.lower())
    for m in _ROLE_TERMS.findall(query):
        ents.add(m.lower())
    return ents


def stage_recall_rerank(
    candidates: list[tuple[int, float]],
    content_for_fid: dict[int, str],
    query: str,
    top_k: int,
    boost_strength: float = 0.5,
) -> list[tuple[int, float]]:
    """Re-rank candidates by entity coverage with the query.

    Args:
      candidates: list of (fact_id, original_score) from hybrid recall.
      content_for_fid: mapping fact_id -> full content (used to count
        entity occurrences).
      query: the original user query.
      top_k: how many results to return.
      boost_strength: how much to boost facts matching more entities
        (0=no effect, 1=double weight for full coverage).

    Returns: top-k (fact_id, new_score) sorted descending.
    """
    if not candidates:
        return []
    ents = _extract_query_entities(query)
    if not ents:
        return candidates[:top_k]

    n_ents = len(ents)
    out: list[tuple[int, float]] = []
    for fid, score in candidates:
        content = (content_for_fid.get(int(fid), '') or '').lower()
        if not content:
            out.append((int(fid), score))
            continue
        hits = sum(1 for e in ents if e in content)
        coverage = hits / n_ents
        # Only positive boost; never penalize. If coverage == 0, factor = 1.
        new_score = score * (1.0 + boost_strength * coverage)
        out.append((int(fid), new_score))

    out.sort(key=lambda x: x[1], reverse=True)
    return out[:top_k]
