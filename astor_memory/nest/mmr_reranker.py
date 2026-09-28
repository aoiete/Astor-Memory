"""mmr_reranker.py — Maximal Marginal Relevance reranker (Ship L, 2026-09-28)

Diversity-aware reranking after hybrid_merge. Helps recall rank quality when
multiple candidates match a query but only the most relevant ones should occupy
the top-k slots. Without MMR, lifestyle-style queries (e.g. "poker NL10 NL20
现金局") surface 5 near-duplicate fact phrasings at top-k and the canonical
answer gets pushed to rank 6-7. MMR (lambda=0.7 default) trades a small
similarity hit for diversity gain.

Algorithm:
  MMR(d) = λ * sim(d, query) - (1 - λ) * max sim(d, selected)
  Greedy: pick argmax MMR, add to selected, repeat.

Complexity: O(k * |cand|) — fine for top_k=10 with candidate_n=4*top_k=40.

Diversity metric:
  Token Jaccard on fact content (cheap, no LLM). Catches near-duplicate
  phrasings of the same fact ("用户喜欢德州" vs "用户偏好德州") without
  requiring embedding computation.

References:
  Carbonell & Goldstein (1998), "The Use of MMR, Diversity-Based Reranking
  for Reordering Documents and Producing Summaries".
"""

import re
from typing import Sequence

# Default lambda — high enough to keep relevance the dominant signal.
DEFAULT_LAMBDA = 0.7

# Token pattern: each CJK character is its own token (matches lex_index
# _tokenize behavior so MMR diversity aligns with BM25's notion of "same
# content"). Latin word runs stay as-is.
_TOKEN_RE = re.compile(r'[A-Za-z]+|[\u4e00-\u9fff]')


def _tokens(text: str) -> set[str]:
    """Tokenize for diversity Jaccard. Each CJK char = own token (matches
    lex_index._tokenize). Strips case for Latin, preserves CJK chars.
    """
    if not text:
        return set()
    out: set[str] = set()
    for m in _TOKEN_RE.findall(text):
        out.add(m.lower() if m.isascii() else m)
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union


def mmr_rerank(
    candidates: Sequence[tuple[int, float]],
    contents: dict[int, str],
    query: str,
    top_k: int,
    lambda_: float = DEFAULT_LAMBDA,
) -> list[tuple[int, float]]:
    """MMR rerank candidates.

    Args:
      candidates: list of (fact_id, score) sorted by score descending
        (the canonical output of hybrid_merge).
      contents: dict mapping fact_id -> content text. Used to compute
        token-Jaccard diversity. Caller must supply the content lookup.
        Facts missing from contents get empty token set (treated as
        maximally diverse, so they won't be evicted).
      query: original query string. Tokenized once for diversity-to-query
        signal (currently unused — kept for future cosine-to-query path).
      top_k: how many facts to return.
      lambda_: relevance/diversity trade-off. 1.0 = pure relevance (no MMR),
        0.0 = pure diversity. Default 0.7 (Carbonell & Goldstein rec).

    Returns:
      list of (fact_id, score) of length ≤ top_k, ordered by MMR score.
      Original scores are preserved (MMR uses them as the relevance term).
    """
    if not candidates or top_k <= 0:
        return []
    if top_k >= len(candidates):
        return list(candidates)
    if lambda_ >= 1.0:
        return list(candidates[:top_k])

    # Pre-compute fact token sets (one DB hit per candidate at call site).
    fact_tokens: dict[int, set[str]] = {
        fid: _tokens(contents.get(fid, "")) for fid, _ in candidates
    }
    # query token set kept for future cosine-to-query path; currently
    # we use candidate score as the relevance signal.
    _ = _tokens(query)

    # Greedy MMR selection. selected[] tracks chosen fact tokens.
    selected_idx: list[int] = []
    selected_tokens: list[set[str]] = []
    remaining = list(range(len(candidates)))

    # First pick: argmax score (deterministic tie-break by original rank).
    first_idx = max(remaining, key=lambda i: (candidates[i][1], -i))
    selected_idx.append(first_idx)
    selected_tokens.append(fact_tokens[candidates[first_idx][0]])
    remaining.remove(first_idx)

    # Subsequent picks: argmax MMR.
    while len(selected_idx) < top_k and remaining:
        best_idx = None
        best_mmr = -float("inf")
        for i in remaining:
            fid_i, score_i = candidates[i]
            tok_i = fact_tokens[fid_i]
            # max similarity to any already-selected (token Jaccard)
            max_sim = 0.0
            for sel_tok in selected_tokens:
                j = _jaccard(tok_i, sel_tok)
                if j > max_sim:
                    max_sim = j
            mmr = lambda_ * score_i - (1.0 - lambda_) * max_sim
            # Deterministic tie-break by original rank (lower index wins).
            if mmr > best_mmr or (mmr == best_mmr and (best_idx is None or i < best_idx)):
                best_mmr = mmr
                best_idx = i
        if best_idx is None:
            break
        selected_idx.append(best_idx)
        selected_tokens.append(fact_tokens[candidates[best_idx][0]])
        remaining.remove(best_idx)

    return [candidates[i] for i in selected_idx]