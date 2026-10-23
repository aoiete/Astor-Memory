"""query_rewriter.py — v1.10.9 (2026-08-27)

LLM-based query rewriting + temporal anchor inference for Astor /v1/read.

Why we need this (despite ingestion-side temporal normalization):
  - LoCoMo / LongMemEval / BEAM queries often contain relative time phrases
    ('yesterday', 'last week', '3 years ago'). The query is asked *now* but
    targets events in the past.
  - Without temporal anchor awareness, our hybrid retriever cannot promote
    facts whose event_date matches the implicit 'now' of the conversation.
  - Running an LLM rewrite for every query is too expensive, so we use
    cheap heuristic + a tiny LLM call only when the query contains
    temporal-relative signals.

What this does:
  - Detects temporal signals in the query (regex-based, no LLM).
  - If a query is classified as temporal AND no `query_timestamp` was passed
    by the caller, we ask a small LLM (gpt-4o-mini or our existing primary)
    to infer the absolute anchor (best-effort) and a rephrased query that
    embeds more cleanly into the L2 cosine space of past-event facts.
  - The rewrite happens at recall time, NOT at ingest time, so it works
    on data already in the DB without re-embedding.

Performance:
  - ~20ms for the regex+heuristic path (no LLM call) for non-temporal queries.
  - ~700ms for the LLM path, only triggered for temporal queries that
    lack a caller-supplied anchor.
  - Cached aggressively by (query_text, conversation_user) tuple.
"""
from __future__ import annotations

import os
import re
import json
import time
from typing import Any
from urllib import request as _urllib_request
from urllib.error import URLError

_TEMPORAL_SIGNAL_RE = re.compile(
    r"\b(yesterday|today|tomorrow|last\s+(?:week|month|year|night|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\d+\s+(?:days?|weeks?|months?|years?)\s+ago|a\s+(?:couple|few)\s+(?:of\s+)?(?:days?|weeks?|months?)\s+ago|the\s+(?:other\s+day|day\s+before\s+yesterday)|last\s+night|this\s+morning|tonight|recently|lately|in\s+the\s+past)\b",
    re.IGNORECASE,
)

_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_CACHE_TTL_SEC = 600  # 10 minutes


def has_temporal_signal(query: str) -> bool:
    return bool(_TEMPORAL_SIGNAL_RE.search(query or ''))


def _call_llm_for_rewrite(query: str, user_id: str | None) -> dict[str, Any] | None:
    """Best-effort LLM call to infer the conversation's query_timestamp
    anchor and a rephrased query that embeds more cleanly into past-event
    cosine space. Returns None on any failure.
    """
    openai_base = os.environ.get('OPENAI_BASE_URL', 'https://api.openrouter.ai/api/v1')
    api_key = (
        os.environ.get('OPENAI_API_KEY')
        or os.environ.get('OPENROUTER_API_KEY')
        or os.environ.get('MINIMAX_API_KEY')
    )
    if not api_key:
        return None

    sys_prompt = (
        'You are a temporal reasoning assistant. Given a user query, infer '
        'the implied "as of" date (when the question is being asked) and '
        'rephrase the query so it embeds more cleanly for vector retrieval.\n'
        'Respond with strict JSON: {"anchor_date": "YYYY-MM-DD or null", "rephrased": "..."}.\n'
        'If the query has no temporal signal, return {"anchor_date": null, "rephrased": null}.'
    )
    user_prompt = (
        f'Query: {query}\n'
        f'User: {user_id or "unknown"}\n'
        'Return JSON only.'
    )
    body = json.dumps({
        'model': 'google/gemini-3.7-flash',
        'messages': [
            {'role': 'system', 'content': sys_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': 200,
        'temperature': 0.0,
    }).encode('utf-8')
    req = _urllib_request.Request(
        f'{openai_base.rstrip("/")}/chat/completions',
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with _urllib_request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            text = data['choices'][0]['message']['content']
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
    except (URLError, KeyError, json.JSONDecodeError, TimeoutError, OSError):
        return None


def rewrite_query(query: str, user_id: str | None = None,
                 caller_anchor: str | None = None) -> dict[str, Any]:
    """Return dict {rephrased_query, anchor_date, used_llm}.

    Behavior:
      - If caller_anchor is provided, skip LLM and return immediately.
      - If query has no temporal signal, return rephrased=query (no change).
      - Otherwise call LLM (cached) for anchor+rephrase.
    """
    cache_key = (query, user_id or '_')
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached['ts']) < _CACHE_TTL_SEC:
        return cached['data']

    out: dict[str, Any] = {
        'rephrased_query': query,
        'anchor_date': caller_anchor[:10] if caller_anchor else None,
        'used_llm': False,
    }
    if caller_anchor:
        pass
    elif has_temporal_signal(query):
        llm_result = _call_llm_for_rewrite(query, user_id)
        if llm_result:
            anchor = llm_result.get('anchor_date')
            rephrased = llm_result.get('rephrased')
            if rephrased:
                out['rephrased_query'] = rephrased
            if anchor:
                out['anchor_date'] = anchor
            out['used_llm'] = True

    _CACHE[cache_key] = {'ts': time.time(), 'data': out}
    return out
