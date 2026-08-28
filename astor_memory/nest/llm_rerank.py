"""llm_rerank.py — v1.10.9 (2026-08-27)

LLM-based re-ranking for the top-K candidates returned by Astor's hybrid
recall. Sits in /v1/read between candidate_fids filtering and final
result assembly. The re-ranker picks the truly relevant facts out of
top-30 to improve downstream answer generation.

Uses google/gemini-3-flash-preview with provider routing to avoid the
60 RPM cap on the default Gemini provider. Cache: simple in-process
LRU keyed by (query, candidate_fids, top_n).

Set ASTOR_RERANK=1 to enable. ASTOR_RERANK_MODEL defaults to
google/gemini-3-flash-preview. ASTOR_RERANK_K defaults to 30 (max
candidates sent to the reranker). ASTOR_RERANK_TOP defaults to 5
(after reranking, keep this many).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from functools import lru_cache
from typing import Iterable

# 2026-08-27 fix: prefer OPENAI_API_KEY over OPENROUTER_API_KEY. hermes redaction
# may strip OPENROUTER_API_KEY to 15-char placeholder while OPENAI_API_KEY keeps
# the real value. Both point to https://openrouter.ai/api/v1 in this codebase.
OPENROUTER_API_KEY = (
    os.environ.get("OPENAI_API_KEY", "")
    or os.environ.get("OPENROUTER_API_KEY", "")
)
RERANK_MODEL = os.environ.get("ASTOR_RERANK_MODEL", "google/gemini-3-flash-preview")
RERANK_K = int(os.environ.get("ASTOR_RERANK_K", "30"))
RERANK_TOP = int(os.environ.get("ASTOR_RERANK_TOP", "5"))


def _call_rerank_llm(query: str, candidates: list[tuple[int, str]]) -> list[int]:
    """Return fact_ids in relevance order, top RERANK_TOP."""
    if not candidates:
        return []
    if not OPENROUTER_API_KEY:
        return [fid for fid, _ in candidates[:RERANK_TOP]]

    # Build prompt
    lines = [f"Query: {query}", "", "Candidates (id: text):"]
    for i, (fid, text) in enumerate(candidates[:RERANK_K]):
        snippet = text.replace("\n", " ")[:200]
        lines.append(f"{fid}: {snippet}")
    lines.extend([
        "",
        f"Pick the {RERANK_TOP} most relevant candidate fact IDs for answering the query.",
        "Output a JSON array of fact IDs, most relevant first. Example: [12, 5, 9]",
    ])
    prompt = "\n".join(lines)

    body = json.dumps({
        "model": RERANK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "provider": {
            "sort": "throughput",
            "preferred_min_throughput": {"p50": 30},
            "allow_fallbacks": True,
        },
    }).encode("utf-8")
    import sys as _sys
    _t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            content = r["choices"][0]["message"].get("content", "")
            obj = json.loads(content)
            if isinstance(obj, list):
                ids = obj
            elif isinstance(obj, dict):
                ids = obj.get("ids", obj.get("results", obj.get("facts", [])))
            else:
                ids = []
            out = [int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()][:RERANK_TOP]
            _msg = f"[RERANK_LLM] ok {time.time()-_t0:.2f}s query='{query[:40]}' n_cands={len(candidates)} -> {len(out)} fids\n"
            _sys.stderr.write(_msg); _sys.stderr.flush(); print(_msg, end='', flush=True)
            return out
    except Exception:
        return [fid for fid, _ in candidates[:RERANK_TOP]]


@lru_cache(maxsize=1024)
def _cached_rerank(query: str, candidate_key: str, candidates: tuple) -> tuple:
    """Cached re-rank. Returns tuple of fact_ids in relevance order."""
    cand_list = list(candidates)  # candidates is tuple of (fid, text)
    return tuple(_call_rerank_llm(query, cand_list))


def rerank_candidates(query: str, candidates: list[tuple[int, str]]) -> list[int]:
    """Public entry: return re-ordered fact_ids.

    candidates: list of (fact_id, content_text), already in recall order.
    Returns fact_ids in reranked order (top RERANK_TOP).

    2026-08-27 fix: trigger rerank when n >= 3 (was RERANK_TOP+1=6).
    Small per-conv DBs (22 facts) often return 5 candidates; previously
    rerank never fired because 5 <= RERANK_TOP=5. Now always rerank.
    """
    import sys as _sys
    _msg_entry = f"[RERANK] query='{query[:40]}' n_candidates={len(candidates)} RERANK_TOP={RERANK_TOP} OPENROUTER_KEY={'set' if OPENROUTER_API_KEY else 'EMPTY'}\n"
    _sys.stderr.write(_msg_entry); _sys.stderr.flush(); print(_msg_entry, end='', flush=True)
    if not candidates:
        return []
    if not OPENROUTER_API_KEY:
        return [fid for fid, _ in candidates[:RERANK_TOP]]

    # Stable cache key
    cand_key = hashlib.md5(
        str(sorted([(f, t[:100]) for f, t in candidates])).encode()
    ).hexdigest()
    cache_key = (query[:200], cand_key, tuple(candidates))
    try:
        ranked = list(_cached_rerank(*cache_key))
        if not ranked:
            return [fid for fid, _ in candidates[:RERANK_TOP]]
        return ranked
    except Exception as e:
        _msg = f"[RERANK] FAIL: {type(e).__name__}: {e}\n"
        _sys.stderr.write(_msg); _sys.stderr.flush(); print(_msg, end='', flush=True)
        return [fid for fid, _ in candidates[:RERANK_TOP]]