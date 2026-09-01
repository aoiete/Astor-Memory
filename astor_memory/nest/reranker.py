"""reranker.py — v1.10.9 (2026-08-26)

Lightweight cross-encoder reranker used as a post-recall reranking stage for
Astor's hybrid retrieval. Lifts Multi-hop accuracy by demoting weak/irrelevant
candidates while promoting strong multi-hop bridges.

Why we built this:
  - Astor's hybrid recall already returns a ranked candidate list (vector + BM25 +
    keyword Jaccard + outcome boost + temporal boost). The scores are independently
    computed, so a fact that's top-ranked by BM25 may be a poor semantic match.
  - For Multi-hop queries (e.g. "A is B's friend, B works at C, what does A do?"),
    the LLM must follow a *chain* of facts. The chain is brittle: if even one link
    is a weak paraphrase, the LLM drops the chain.
  - A small reranker takes the top-N candidates and re-orders them with a query-
    document cross-attention signal, lifting chain coherence significantly.

Implementation choice (zero new dependency):
  - We use a *lexical overlap rerank* with semantic-aware bonus terms. This is
    fast (microseconds per call), uses no LLM tokens, and works without an
    internet model download. If a proper cross-encoder model is available
    (sentence-transformers), use that instead via the ASTOR_RERANK_MODEL env var.

Module-level API:
  - rerank_candidates(query: str, candidates: list[dict]) -> list[dict]
    Each candidate dict has keys: {id, score, content, ...}. Returns candidates
    re-sorted by the new score (highest first) with rerank_score added.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Any

try:
    from sentence_transformers import CrossEncoder  # type: ignore
    _HAS_CROSS_ENCODER = True
except Exception:
    _HAS_CROSS_ENCODER = False


_MULTI_HOP_BRIDGE_TERMS = {
    'because', 'since', 'therefore', 'so', 'thus', 'hence', 'as a result',
    'mentioned', 'said', 'told', 'asked', 'replied', 'according to',
    'works', 'worked', 'job', 'company', 'friend', 'partner', 'colleague',
    'meeting', 'met', 'visit', 'visited', 'together', 'helped',
    'married', 'relationship', 'family', 'son', 'daughter', 'mother', 'father',
}

_STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for', 'from',
    'has', 'have', 'he', 'her', 'his', 'i', 'in', 'is', 'it', 'its', 'me',
    'my', 'no', 'not', 'of', 'on', 'or', 'our', 'she', 'so', 'the', 'their',
    'them', 'they', 'this', 'to', 'us', 'was', 'we', 'were', 'what', 'when',
    'where', 'which', 'who', 'why', 'will', 'with', 'you', 'your',
}

_TOKEN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z'-]+\b")


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS]


def _idf_bridge_bonus(query_tokens: list[str], candidate_text: str) -> float:
    """Bridge-term coverage: how many connector tokens appear in candidate?

    Multi-hop chains rely on connector / relationship words. If a candidate
    contains multiple bridge terms relative to the query, it is more likely to
    be a meaningful chain link.
    """
    if not candidate_text:
        return 0.0
    cand_lower = candidate_text.lower()
    hits = 0
    for term in _MULTI_HOP_BRIDGE_TERMS:
        if term in cand_lower:
            hits += 1
    q_bridge_hits = sum(1 for t in query_tokens if t in _MULTI_HOP_BRIDGE_TERMS)
    if q_bridge_hits == 0 and hits == 0:
        return 0.0
    return math.log1p(hits) * (1.0 if q_bridge_hits > 0 else 0.5)


def _lexical_overlap(query_tokens: list[str], candidate_text: str) -> float:
    """TF-style lexical overlap between query tokens and candidate text."""
    if not query_tokens or not candidate_text:
        return 0.0
    cand_tokens = _tokens(candidate_text)
    if not cand_tokens:
        return 0.0
    cand_freq = Counter(cand_tokens)
    q_freq = Counter(query_tokens)
    score = 0.0
    for token, q_count in q_freq.items():
        c_count = cand_freq.get(token, 0)
        if c_count == 0:
            continue
        score += (1.0 + math.log1p(q_count)) * (1.0 + math.log1p(c_count))
    norm_q = math.sqrt(sum((1.0 + math.log1p(c)) ** 2 for c in q_freq.values()))
    norm_c = math.sqrt(sum((1.0 + math.log1p(c)) ** 2 for c in cand_freq.values()))
    if norm_q == 0 or norm_c == 0:
        return 0.0
    return score / (norm_q * norm_c)


_CROSS_ENCODER_MODEL = None


def _load_cross_encoder():
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    if not _HAS_CROSS_ENCODER:
        return None
    model_name = os.environ.get("ASTOR_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    try:
        _CROSS_ENCODER_MODEL = CrossEncoder(model_name)
        return _CROSS_ENCODER_MODEL
    except Exception:
        return None


def rerank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int | None = None,
    rerank_weight: float = 0.65,
) -> list[dict[str, Any]]:
    """Rerank candidates using a cross-encoder if available, else lexical+bridge fallback.

    Args:
      query: user query string
      candidates: list of dicts each with at minimum a 'content' field. Optional
        'id', 'score' keys may be present and will be preserved.
      top_n: if set, only return top-n candidates after reranking. If None,
        returns all candidates reranked.
      rerank_weight: blend factor between the original score and the rerank score
        (final_score = rerank_weight * rerank_score + (1 - rerank_weight) * original_score).

    Returns:
      list[dict] sorted by final_score descending, with 'rerank_score' added.
    """
    if not candidates:
        return candidates

    ce = _load_cross_encoder()
    if ce is not None:
        try:
            pairs = [(query, c.get('content', '')) for c in candidates]
            scores = ce.predict(pairs, show_progress_bar=False)
            rerank_scores = [float(s) for s in scores]
        except Exception:
            ce = None

    if ce is None:
        query_tokens = _tokens(query)
        rerank_scores = []
        for c in candidates:
            text = c.get('content', '') or c.get('overview', '') or ''
            lex = _lexical_overlap(query_tokens, text)
            bridge = _idf_bridge_bonus(query_tokens, text)
            rerank_scores.append(0.85 * lex + 0.15 * bridge)

    normalized = []
    if rerank_scores:
        lo, hi = min(rerank_scores), max(rerank_scores)
        span = hi - lo if hi > lo else 1.0
        normalized = [(s - lo) / span for s in rerank_scores]

    out = []
    for c, rs, ns in zip(candidates, rerank_scores, normalized):
        original = float(c.get('score', 0.0))
        final = rerank_weight * ns + (1.0 - rerank_weight) * original
        new_c = dict(c)
        new_c['rerank_score'] = ns
        new_c['score'] = final
        out.append(new_c)

    out.sort(key=lambda x: x.get('score', 0.0), reverse=True)
    if top_n is not None and len(out) > top_n:
        out = out[:top_n]
    return out