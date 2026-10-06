""""
astor_memory.recall_rrf — Reciprocal Rank Fusion for TEMPR-style 4-way recall.

v1.16.73 (2026-10-06): Ship F. Replaces astor's current weighted-sum hybrid
merge with proper RRF (the 5-stage paper pattern from LongMemEval-S best
paper, mp/-ula31Nw4kmDYPQooKPsGA).

Why RRF > weighted sum:
  - No need to normalize per-source scores (BM25 has no upper bound, vector
    cosine is [0,1]). Each source contributes rank, not magnitude.
  - Robust to source-quality variation: a fact that's #1 in BM25 but #50 in
    vector still gets a meaningful contribution.
  - Standard recipe: score = sum(1 / (k + rank)) across sources.
    k=60 is the canonical constant (Cormack et al. 2009).

Function:
    rrf_fusion(*ranked_lists, k=60) -> list[(fact_id, rrf_score)]

Usage:
    bm25_hits = lex.bm25_search(...)           # [(fid, score), ...]
    vec_hits  = nest.search(...)                # [(fid, score), ...]
    time_hits = bus_time_filter(since, until)   # [(fid, score), ...]
    merged = rrf_fusion(bm25_hits, vec_hits, time_hits)
    # rerank with top-N by rrf_score

Each input is a ranked list (descending by source-native score).
"""
from __future__ import annotations


def rrf_fusion(*ranked_lists: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion across N ranked lists.

    score(fact) = sum_i 1 / (k + rank_i(fact))

    Args:
        ranked_lists: variable number of lists, each (fact_id, score) sorted
            descending by their source-native score. Score is ignored —
            only rank matters.
        k: rank constant (default 60 per Cormack et al. 2009).

    Returns:
        [(fact_id, rrf_score)] sorted descending by rrf_score.
        Facts absent from a list simply contribute 0 from that source.
    """
    if not ranked_lists:
        return []
    rrf: dict[int, float] = {}
    for hits in ranked_lists:
        for rank_idx, (fact_id, _src_score) in enumerate(hits):
            rank_idx_1 = rank_idx + 1  # 1-based rank
            contribution = 1.0 / (k + rank_idx_1)
            rrf[int(fact_id)] = rrf.get(int(fact_id), 0.0) + contribution
    return sorted(rrf.items(), key=lambda x: x[1], reverse=True)