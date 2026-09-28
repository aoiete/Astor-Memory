"""hyde.py — Hypothetical Document Embeddings (Ship O, 2026-09-28)

For short, abstract queries (e.g. "fit 健身 计划", "今日日柱"), the query
embedding and a target fact embedding live in different semantic regions
of vector space. Naive cosine similarity misses the right fact.

HyDE flips the script: instead of asking "is this query close to this fact?",
we ask "if we knew the answer, what would it look like?" The LLM generates a
plausible short answer (~50-100 tokens) for the query, then we embed THAT
and search. The hypothetical answer lives in the same semantic region as
the actual stored facts, so cosine hits the right neighborhood.

References:
  Gao et al, 2022, "Precise Zero-Shot Dense Retrieval without Relevance
  Labels" (HyDE paper). https://arxiv.org/abs/2212.10496

Trigger:
  - Query length < 8 tokens AND no synonym expansion hit (covers the
    common short-query case).
  - Gated by ASTOR_HYDE=1 env var (default OFF). Cheap model only.
  - LLM call failure → silently fall back to original query embed
    (so the path is never a regression).

Cost:
  - One LLM call (~1-2s with cheap model, ~$0.0003 / 1k tokens).
  - One extra embed (negligible).
  - One extra vector search (4× pool, merged with primary).

Return shape:
  - Returns merged (fid, score) list. Original query hits are kept; HyDE
    hits are weighted 0.5× and merged by max score per fact_id.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from functools import lru_cache

# ---------------------------------------------------------------------------
# Config (env-driven so operators can A/B without code change)
# ---------------------------------------------------------------------------
# Match llm_rerank.py: also accept OPENAI_API_KEY as the upstream token
# (single key reused across providers via the OpenRouter-compat layer).
OPENROUTER_API_KEY = (
    os.environ.get("OPENAI_API_KEY", "")
    or os.environ.get("OPENROUTER_API_KEY", "")
)
HYDE_MODEL = os.environ.get("ASTOR_HYDE_MODEL", "minimax/minimax-m2")
HYDE_TIMEOUT = float(os.environ.get("ASTOR_HYDE_TIMEOUT", "8"))
HYDE_MAX_TOKENS = int(os.environ.get("ASTOR_HYDE_MAX_TOKENS", "120"))
HYDE_HIT_WEIGHT = float(os.environ.get("ASTOR_HYDE_HIT_WEIGHT", "0.5"))

# Cheap-model preferred; throughput filter so we don't slow recall path.
_HYDE_BODY = {
    "model": HYDE_MODEL,
    "max_tokens": HYDE_MAX_TOKENS,
    "temperature": 0.0,
    "provider": {
        "sort": "throughput",
        "preferred_min_throughput": {"p50": 30},
        "allow_fallbacks": True,
    },
}

# Token count heuristic for short queries (matches synonym_expander's).
_TOKEN_RE = re.compile(r'[\u4e00-\u9fff]|[a-zA-Z]+')


def _is_short(query: str) -> bool:
    """True if query has < 8 tokens — HyDE sweet spot."""
    return len(_TOKEN_RE.findall(query)) < 8


def _call_hyde_llm(query: str) -> str:
    """Generate a hypothetical answer to `query`. Returns "" on any failure."""
    if not OPENROUTER_API_KEY:
        return ""
    prompt = (
        "You are answering a knowledge-base query. Generate a 1-2 sentence "
        "hypothetical answer that a knowledgeable operator would write to "
        f"this query: {query}\n\n"
        "Answer in the same language as the query (Chinese for Chinese, "
        "English for English). Be specific — include concrete terms that "
        "would match a stored fact."
    )
    body = json.dumps({**_HYDE_BODY, "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=HYDE_TIMEOUT) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            content = r["choices"][0]["message"].get("content", "").strip()
            import sys as _sys
            _sys.stderr.write(f"[HYDE_LLM] ok {time.time()-t0:.2f}s query='{query[:40]}'\n")
            return content
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, Exception):
        return ""


@lru_cache(maxsize=512)
def _cached_hypothetical(query: str) -> str:
    return _call_hyde_llm(query)


def hypothetical_answer(query: str, use_cache: bool = True) -> str:
    """Return LLM-generated hypothetical answer for `query`. Cached.

    Returns "" when:
    - OPENROUTER_API_KEY unset
    - Query not short enough (≥8 tokens; HyDE has no value there)
    - LLM call fails (network, timeout, parse)
    - Env gate ASTOR_HYDE is not '1'
    """
    if os.environ.get("ASTOR_HYDE", "0") != "1":
        return ""
    if not _is_short(query):
        return ""
    if use_cache:
        return _cached_hypothetical(query)
    return _call_hyde_llm(query)


def merge_hyde_hits(
    primary_hits: list[tuple[int, float]],
    hyde_hits: list[tuple[int, float]],
    weight: float = HYDE_HIT_WEIGHT,
) -> list[tuple[int, float]]:
    """Merge HyDE-side hits into primary hits, weighted by `weight`.

    For each fact_id: final_score = max(primary_score, hyde_score * weight).
    Returns merged list sorted by score desc, deduped by fact_id.
    """
    out: dict[int, float] = {int(f): s for f, s in primary_hits}
    for fid, score in hyde_hits:
        fid = int(fid)
        scaled = score * weight
        if fid not in out or scaled > out[fid]:
            out[fid] = scaled
    return sorted(out.items(), key=lambda x: x[1], reverse=True)