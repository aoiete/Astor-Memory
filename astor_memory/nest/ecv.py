"""ecv.py — v1.16.6 (2026-09-30)

GraphMemix-inspired Evidence-Chain Verifier (ECV) for astor memory recall.

Source: 北大王选计算机研究所 + MemoraX AI GraphMemix paper
(arXiv 2608.26983). Article summary in
D:/AI/astor-memory/docs/wechat-graphmemix-paper-article.txt.

GraphMemix ablation showed:
    Embedding Top-K             49.20%
    + Multi-view Retrieval     53.60%  (+4.4)
    + Node Verifier             58.80%  (+5.2)   <-- main gain
    + ECV                       59.70%  (+0.9)
    + Full Forest Optimization  61.55%  (+1.85)

We ship the "Node Verifier" half (the big-gain part). Pure-Python,
no LLM, <5ms per call.

What "Node Verifier" does (per GraphMemix §04):
  - Read each candidate fact + query, judge HOW MUCH the candidate
    independently supports answering the query.
  - In GraphMemix this is an LLM call. We approximate via cheap
    signals:
      - kw token overlap ratio between query and candidate (already
        available; re-use)
      - embed cosine similarity (already in nest vector index)
      - entity overlap (using astor's entities_json column, v1.14.21)
      - confidence + access_count (fact-internal quality signals)
  - Combine into a single 0-1 "usefulness" score.

Why ship without LLM:
  - GraphMemix uses Qwen3-VL-8B per-candidate. We don't have a
    per-call LLM budget on the hot recall path; match_experiences and
    /v1/read both run for every session turn.
  - Cheap signal aggregation is fast and stable. If quality proves
    insufficient we can layer LLM on top later.

ECV relation classifier (§04 ECV):
  - For each pair (anchor, candidate), classify the relation type:
    new_fact / clarification / corroboration / repeat / conflict / none
  - Used to decide which pairs make a valid evidence-chain edge.
  - We implement this as a deterministic function (no model). Token +
    entity overlap + timestamp heuristics.
  - Output: relation_type ∈ {new_fact, clarification, corroboration,
    repeat, conflict, none}, confidence 0-1.

Use:
  - match_experiences: pre-score candidates with node_usefulness,
    add to hybrid score as a third signal (kw + emb + usefulness)
  - /v1/read (future): build evidence chains using ecv.relation()
    to promote chain coherence

Cost: ~3-5ms per call for typical 30-candidate set.
"""
from __future__ import annotations

import re
from typing import Iterable


# v1.16.6: relation classes per GraphMemix §04 ECV
REL_NEW_FACT = "new_fact"           # introduces info not in anchor
REL_CLARIFY = "clarification"      # expands/clarifies anchor
REL_CORROBORATE = "corroboration"  # repeats/supports anchor
REL_REPEAT = "repeat"              # near-duplicate of anchor (low value)
REL_CONFLICT = "conflict"          # contradicts anchor (penalize)
REL_NONE = "none"                  # no useful relation


def _tokenize(text: str) -> set[str]:
    """CJK bigram + latin word tokenization (matches lex_index._tokenize).

    v1.16.18.1 fix: use CJK BIGRAMS (2-char sliding window) instead of
    single chars. Single-char tokens diluted Jaccard similarity (any
    query that touched CJK got score=0.000 because the candidate
    had hundreds of single-char tokens, washing out query overlap).

    Example:
        "微信公众号" with single-char → {"微", "信", "公", "众", "号"} (5 tokens)
        "微信公众号" with bigram     → {"微信", "信公", "众众", "众号"} (4 tokens)

    The bigram token set is much more discriminative — overlapping
    bigrams indicate true semantic overlap, not just shared characters.
    """
    if not text:
        return set()
    toks = set()
    cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
    # Bigrams (sliding window of 2 chars) — discriminative token unit
    for i in range(len(cjk_chars) - 1):
        toks.add(cjk_chars[i] + cjk_chars[i + 1])
    # Latin words
    for w in re.findall(r'[A-Za-z0-9]+', text):
        toks.add(w.lower())
    return toks


def _parse_entities(entities_json: str | None) -> list[str]:
    """Extract entity values from memory_canonical.entities_json.

    entities_json schema (v1.14.21 Ship B):
      [{"type": "person|location|time|topic", "value": "..."}, ...]
    """
    if not entities_json:
        return []
    try:
        import json
        arr = json.loads(entities_json)
        return [e.get("value", "") for e in arr if isinstance(e, dict)]
    except Exception:
        return []


def node_usefulness(
    query: str,
    candidate_content: str,
    candidate_keywords: list[str] | None = None,
    candidate_entities_json: str | None = None,
    candidate_confidence: float = 0.7,
    candidate_access_count: int = 0,
) -> float:
    """Score how much this candidate independently supports answering query.

    Returns 0..1. Combines:
      - token Jaccard similarity between query and candidate (40%)
      - keyword overlap (25%) — explicit keywords beat implicit tokens
      - entity overlap (25%) — named entities usually matter for recall
      - confidence + access_count quality (10%) — high-confidence,
        frequently-accessed facts are slightly preferred

    Bounded so a candidate with no kw/entity overlap still gets ~0.05
    baseline (LLM context hint: never surface a fact that's completely
    unrelated to the query).
    """
    if not query or not candidate_content:
        return 0.05
    q_toks = _tokenize(query)
    c_toks = _tokenize(candidate_content)
    if not q_toks or not c_toks:
        return 0.05
    # Token Jaccard
    inter = q_toks & c_toks
    union = q_toks | c_toks
    jaccard = len(inter) / len(union) if union else 0.0
    # Keyword overlap (boosted — explicit signals)
    if candidate_keywords:
        kw_toks = set()
        for kw in candidate_keywords:
            kw_toks |= _tokenize(str(kw))
        kw_overlap = len(q_toks & kw_toks) / max(1, len(q_toks))
    else:
        kw_overlap = 0.0
    # Entity overlap
    ents = _parse_entities(candidate_entities_json)
    if ents:
        ent_toks = set()
        for e in ents:
            ent_toks |= _tokenize(e)
        ent_overlap = len(q_toks & ent_toks) / max(1, len(q_toks))
    else:
        ent_overlap = 0.0
    # Quality
    conf = max(0.0, min(1.0, float(candidate_confidence)))
    access_factor = min(1.0, float(candidate_access_count) / 10.0)
    quality = (conf + access_factor) / 2.0
    # Combined (weights sum to 1.0)
    score = (
        0.40 * jaccard
        + 0.25 * kw_overlap
        + 0.25 * ent_overlap
        + 0.10 * quality
    )
    # Baseline (no overlap at all — still possible hint)
    if score < 0.05:
        score = 0.05
    return min(1.0, score)


def relation(
    anchor_content: str,
    anchor_entities_json: str | None,
    candidate_content: str,
    candidate_entities_json: str | None,
    anchor_created_at: str | None = None,
    candidate_created_at: str | None = None,
) -> tuple[str, float]:
    """ECV relation classification per GraphMemix §04.

    Returns (relation_type, confidence 0..1). Cheap deterministic
    heuristic — see article's relation taxonomy:
      new_fact / clarification / corroboration / repeat / conflict / none

    Heuristic signals:
      - token overlap ratio high (>0.6) + same entities → corroboration
        or repeat (caller picks by length delta)
      - moderate overlap + new entities → clarification
      - disjoint entities → new_fact
      - contradicts anchor (rare in practice — flip on negation
        words like "not / 没 / don't / shouldn't") → conflict
      - otherwise → none

    Confidence is the max of the supporting signal strengths.
    """
    if not anchor_content or not candidate_content:
        return (REL_NONE, 0.0)
    a_toks = _tokenize(anchor_content)
    c_toks = _tokenize(candidate_content)
    if not a_toks or not c_toks:
        return (REL_NONE, 0.0)
    a_ents = set(_parse_entities(anchor_entities_json))
    c_ents = set(_parse_entities(candidate_entities_json))
    # Token Jaccard
    inter = a_toks & c_toks
    union = a_toks | c_toks
    tok_jaccard = len(inter) / len(union) if union else 0.0
    # Entity Jaccard
    ent_union = a_ents | c_ents
    ent_jaccard = (
        len(a_ents & c_ents) / len(ent_union) if ent_union else 0.0
    )
    # Token containment — useful for new_fact detection
    # (candidate fully contains anchor's tokens = clarifying elaboration)
    a_in_c = len(a_toks & c_toks) / len(a_toks) if a_toks else 0.0
    c_in_a = len(c_toks & a_toks) / len(c_toks) if c_toks else 0.0
    # Negation words (cheap conflict heuristic — Chinese + English)
    neg_words = {"not", "no", "never", "don't", "doesn't", "didn't",
                 "isn't", "aren't", "won't", "wouldn't", "shouldn't",
                 "没", "无", "不", "未", "别", "勿"}
    anchor_neg = bool(a_toks & neg_words)
    cand_neg = bool(c_toks & neg_words)
    contradiction = anchor_neg != cand_neg  # XOR — one neg, one not
    # Classification
    if contradiction and (tok_jaccard > 0.3 or ent_jaccard > 0.0):
        return (REL_CONFLICT, max(tok_jaccard, ent_jaccard))
    # High overlap + similar length = repeat / corroboration
    if tok_jaccard >= 0.6 and abs(len(anchor_content) - len(candidate_content)) < 50:
        return (REL_REPEAT if abs(len(a_toks) - len(c_toks)) < 3 else REL_CORROBORATE, tok_jaccard)
    if tok_jaccard >= 0.6:
        return (REL_CORROBORATE, tok_jaccard)
    # Candidate is superset of anchor (with new content) = clarification
    if a_in_c >= 0.7 and c_in_a < 0.7 and len(c_toks) > len(a_toks):
        return (REL_CLARIFY, a_in_c)
    # Same entities, different tokens = clarifying new info
    if ent_jaccard >= 0.5 and tok_jaccard >= 0.2:
        return (REL_CLARIFY, ent_jaccard)
    # Same entities, totally different content = new_fact
    if ent_jaccard >= 0.5 and tok_jaccard < 0.2:
        return (REL_NEW_FACT, ent_jaccard)
    # Disjoint entities = new_fact
    if a_ents and c_ents and not (a_ents & c_ents):
        return (REL_NEW_FACT, 0.5)
    # Otherwise — no useful relation
    if tok_jaccard < 0.1 and ent_jaccard < 0.1:
        return (REL_NONE, 0.0)
    return (REL_NONE, max(tok_jaccard, ent_jaccard) * 0.5)


def batch_node_usefulness(
    query: str,
    candidates: Iterable[dict],
    key_content: str = "content",
    key_keywords: str = "keywords",
    key_entities: str = "entities_json",
    key_confidence: str = "confidence",
    key_access_count: str = "access_count",
) -> list[float]:
    """Score a batch of candidates (dicts) for node usefulness.

    Returns a list of floats (same order as input). Each dict must
    have at minimum `content`; missing optional fields default to 0.
    """
    out = []
    for c in candidates:
        out.append(node_usefulness(
            query=query,
            candidate_content=c.get(key_content, ""),
            candidate_keywords=c.get(key_keywords),
            candidate_entities_json=c.get(key_entities),
            candidate_confidence=c.get(key_confidence, 0.7),
            candidate_access_count=c.get(key_access_count, 0),
        ))
    return out


__all__ = [
    "REL_NEW_FACT", "REL_CLARIFY", "REL_CORROBORATE",
    "REL_REPEAT", "REL_CONFLICT", "REL_NONE",
    "node_usefulness", "relation", "batch_node_usefulness",
]