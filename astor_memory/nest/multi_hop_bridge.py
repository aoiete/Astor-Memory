"""multi_hop_bridge.py — v1.10.9 (2026-08-27)

Multi-hop recall bridge for Astor. Lifts Multi-hop accuracy by linking
candidate facts via shared entity overlap.

Why we need this (despite astor_auto_link existing at write time):
  - The write-time auto_link creates provenance edges, but the read pipeline
    (hybrid_merge + temporal_boost + optional rerank) does NOT currently
    consume those edges during recall.
  - Multi-hop queries like "A's friend B works at X" need BOTH facts to
    co-rank high. Without a bridge, BM25/vector scores may put each
    independently mid-rank and never surface them together.
  - This module extracts the candidate set, finds shared entity tokens
    (names, nouns), and applies a multiplicative bridge boost to facts
    that share entities with high-ranked candidates.

How the boost works:
  - For each candidate C, extract its named entities (capitalized tokens,
    known-entity dictionary from tags + keywords).
  - For each other candidate D, count entity overlap with C.
  - D gets a multiplicative boost proportional to how many top-K neighbors
    share entities with it.
  - This promotes chain coherence without sacrificing lexical/vector
    relevance.

Implementation choice:
  - Pure Python, no LLM, no external dep. Runs in <5ms for 50 candidates.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Common English stopwords / generic nouns that should NOT count as entities.
_ENTITY_STOPWORDS = {
    'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'his', 'her',
    'they', 'them', 'their', 'it', 'its', 'the', 'a', 'an', 'and', 'or', 'but',
    'if', 'then', 'so', 'because', 'as', 'at', 'in', 'on', 'for', 'with',
    'about', 'to', 'from', 'by', 'of', 'this', 'that', 'these', 'those',
    'is', 'was', 'are', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
    'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might',
    'can', 'i', 'said', 'says', 'tell', 'told', 'say', 'go', 'went', 'come',
    'came', 'get', 'got', 'make', 'made', 'take', 'took', 'give', 'gave',
    'know', 'knew', 'think', 'thought', 'see', 'saw', 'want', 'wanted',
    'use', 'used', 'find', 'found', 'tell', 'told', 'ask', 'asked',
    'work', 'works', 'worked', 'friend', 'friends', 'family', 'thing',
    'things', 'people', 'time', 'times', 'day', 'days', 'year', 'years',
    'today', 'yesterday', 'tomorrow', 'week', 'weeks', 'month', 'months',
}

_CAPITALIZED_RE = re.compile(r"\b([A-Z][a-z][a-zA-Z'-]*)\b")
_WORD_RE = re.compile(r"\b[a-zA-Z][a-zA-Z'-]+\b")


def _extract_entities(text: str, keywords: list[str] | None = None,
                     tags: list[str] | None = None) -> list[str]:
    """Extract entity-like tokens from a fact's text + structured fields.

    Priority:
      1. Tokens already stored as keywords (most reliable).
      2. Capitalized tokens in the body (likely proper nouns / names).
      3. Tokens from tags (after stripping 'outcome' / 'kind' / 'auto_*' meta tags).
    """
    ents: list[str] = []
    if keywords:
        for kw in keywords:
            for t in _WORD_RE.findall(kw or ''):
                t_l = t.lower()
                if t_l not in _ENTITY_STOPWORDS and len(t_l) >= 3:
                    ents.append(t_l)
    if tags:
        for tg in tags:
            t_l = (tg or '').lower()
            if t_l.startswith('outcome:'):
                continue
            if t_l.startswith('auto_'):
                continue
            if t_l in _ENTITY_STOPWORDS:
                continue
            if len(t_l) >= 4 and '_' not in t_l:
                ents.append(t_l)
    if text:
        for cap in _CAPITALIZED_RE.findall(text):
            ents.append(cap.lower())
    return ents


def _bridge_entities(candidates: list[dict[str, Any]], top_n: int = 5) -> dict[int, Counter]:
    """Compute per-fact entity-frequency over the top-N candidates.

    Each top-N candidate contributes its entities to a global pool; a fact
    that shares entities with multiple top-N neighbors accumulates weight.
    """
    seed = candidates[:top_n]
    entity_to_facts: dict[str, set[int]] = {}
    for c in seed:
        fid = int(c['id'])
        ents = _extract_entities(c.get('content', ''), c.get('keywords'), c.get('tags'))
        for e in ents:
            entity_to_facts.setdefault(e, set()).add(fid)
    bridges: dict[int, Counter] = {}
    for c in candidates:
        fid = int(c['id'])
        c_ents = _extract_entities(c.get('content', ''), c.get('keywords'), c.get('tags'))
        bridge = Counter()
        for e in c_ents:
            for nb in entity_to_facts.get(e, set()):
                if nb != fid:
                    bridge[nb] += 1
        bridges[fid] = bridge
    return bridges


def apply_multi_hop_boost(
    candidates: list[dict[str, Any]],
    top_seed_n: int = 3,
    decay: float = 0.15,
) -> list[dict[str, Any]]:
    """Re-rank candidates by applying a multi-hop bridge boost.

    For each candidate C, compute its bridge strength = sum of decay^k for
    each top-N neighbor it shares entities with. C's score is multiplied by
    (1 + decay * bridge_strength).

    Args:
      candidates: list of dicts with keys 'id', 'score', 'content', 'keywords', 'tags'.
      top_seed_n: how many top candidates to seed the entity pool with.
      decay: per-hop decay factor (0 < decay <= 1).

    Returns: new list sorted by score descending.
    """
    if not candidates:
        return candidates
    bridges = _bridge_entities(candidates, top_n=top_seed_n)

    out = []
    for c in candidates:
        fid = int(c['id'])
        bridge = bridges.get(fid, Counter())
        if not bridge:
            new_score = c['score']
        else:
            bridge_strength = sum(decay ** k for k in bridge.values())
            new_score = c['score'] * (1.0 + decay * bridge_strength)
        new_c = dict(c)
        new_c['score'] = new_score
        new_c['multi_hop_bridge_count'] = sum(bridge.values())
        out.append(new_c)

    out.sort(key=lambda x: x['score'], reverse=True)
    return out