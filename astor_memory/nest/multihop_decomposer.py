"""multihop_decomposer.py — v1.10.9 (2026-08-27)

Lightweight multi-hop query decomposer. NO LLM call. Returns the
original query + 0-2 sub-queries that the hybrid retriever should also
search. Used for queries that the heuristic detector flags as multi-hop.

Strategy:
  - Look for known "multi-hop connectors" in the query:
      "based on", "according to", "how did", "first",
      "between", "mentioned", "told", "asked", "accordingly",
      "connection", "relationship with", "how do ... know each other",
  - Split the query on these connectors and emit each side as a sub-query.
  - If the query contains a quoted entity reference (a person name in
    particular), emit a focused sub-query around that entity.

This is purely deterministic; it costs <1ms per query. We rely on the
hybrid recall to surface facts that match the sub-queries and on
LLM judgment to compose the final answer.
"""
from __future__ import annotations

import re
from typing import List

_MULTIHOP_TRIGGERS = (
    'based on',
    'according to',
    'how did',
    'how do',
    'how are',
    'first met',
    'first time',
    'in common',
    'connection between',
    'connection with',
    'relationship between',
    'relationship with',
    'mentioned that',
    'told me about',
    'told her about',
    'told him about',
    'asked about',
    'referenced',
    'related to',
    'feel about',
    'think about',
    'know about',
    'know each other',
    'influence',
    'connect',
)

_SPLIT_PATTERN = re.compile(
    r'(?:^|\W)(based on|according to|how did|how do|how are|'
    r'first met|first time|in common|connection between|connection with|'
    r'relationship between|relationship with|mentioned that|'
    r'told (?:me|her|him|us) about|asked about|referenced|'
    r'related to|feel about|think about|know about|know each other|'
    r'influence|connect)\b',
    re.IGNORECASE,
)


def is_multihop_query(query: str) -> bool:
    """Quick test: does the query look like a multi-hop question?"""
    q = (query or '').lower()
    if not q:
        return False
    return any(t in q for t in _MULTIHOP_TRIGGERS)


def decompose(query: str) -> List[str]:
    """Return [query, sub_query_1, sub_query_2, ...].

    The original query is always first so single-fact recall still works.
    Sub-queries are heuristic splits at known multi-hop connectors.
    """
    if not query:
        return ['']
    out: list[str] = [query]
    if not is_multihop_query(query):
        return out

    # 1) Split on the FIRST detected connector.
    m = _SPLIT_PATTERN.search(query)
    if m:
        # Two halves around the connector; strip connector from each side.
        left = query[:m.start()].strip(' ,?."')
        right = query[m.end():].strip(' ,?."')
        if left and right and len(left) >= 5 and len(right) >= 5:
            if right not in out:
                out.append(right)
            # The left half is often a generic statement; we add a more
            # focused sub-query by keeping the connector text to help the
            # retriever find the bridge fact.
            focused = f"{left.strip(' .,?')} {m.group(0)} {right}"
            if focused != query and focused not in out:
                out.append(focused)

    # 2) Extract any quoted entities as focused sub-queries.
    quoted = re.findall(r'"([^"]+)"', query)
    for q in quoted:
        if len(q) >= 3 and q not in out:
            out.append(q)

    # 3) If query mentions two distinct capitalized names (e.g. "Caroline
    # and Melanie"), emit focused sub-queries for each.
    proper = re.findall(r"\b([A-Z][a-z][a-zA-Z'-]+)\b", query)
    if len(set(proper)) >= 2:
        for name in set(proper):
            sub = f"{name} {query}"
            if sub not in out:
                out.append(sub)

    return out
