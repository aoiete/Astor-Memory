"""query_expander.py — v1.10.9 (2026-08-27)

Query expansion for Astor hybrid recall. Generates 2-3 query variants to
improve recall without re-embedding. The key insight: when the user asks
"What did Caroline research?", an LLM can identify the entity/verb pair
("Caroline", "research") and generate paraphrases that match different
parts of the original long-fact content.

Why this works for LoCoMo specifically:
  - LoCoMo queries are short and abstract (1 sentence, often 5-10 words).
  - Ingested facts are long (10-30 turn sessions concatenated).
  - BM25 + cosine over the long fact content naturally miss short queries
    because the query terms are diluted in the long content.
  - Generating 2-3 query variants with different phrasings re-aligns the
    query with the dense content representation.

Cost: ~600ms for one Gemini Flash call per query. We CACHE aggressively
so repeated identical queries (test runs) pay nothing.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib import request as _urllib_request
from urllib.error import URLError

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_TTL_SEC = 600

_PROMPT = (
    'You generate query reformulations for a memory retrieval system. '
    'Given a user query, output 2 alternative phrasings that would match '
    'different wording in stored conversation memories. The memories are '
    'long dialogue sessions, so prefer reformulations that:\n'
    '  1. Surface specific entities, actions, and dates\n'
    '  2. Use natural English subject-verb-object structure\n'
    '  3. Avoid over-abstracting (do NOT make them MORE abstract)\n'
    'Output strict JSON: {"variants": ["...", "..."]}. 2 variants only.'
)


def _call_llm(query: str) -> list[str] | None:
    openai_base = os.environ.get('OPENAI_BASE_URL', 'https://api.openrouter.ai/api/v1')
    api_key = (
        os.environ.get('OPENAI_API_KEY')
        or os.environ.get('OPENROUTER_API_KEY')
        or os.environ.get('MINIMAX_API_KEY')
    )
    if not api_key:
        return None
    body = json.dumps({
        'model': 'google/gemini-3.7-flash',
        'messages': [
            {'role': 'system', 'content': _PROMPT},
            {'role': 'user', 'content': f'Query: {query}'},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': 200,
        'temperature': 0.2,
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
            vs = parsed.get('variants', [])
            return [v.strip() for v in vs if v.strip()][:2]
    except Exception:
        return None


def expand_query(query: str, force: bool = False) -> list[str]:
    """Return the original query + up to 2 expanded variants."""
    if not query:
        return ['']
    if not force and not _should_expand(query):
        return [query]
    if not force:
        cache_key = query.lower().strip()
        cached = _CACHE.get(cache_key)
        if cached and (time.time() - cached['ts']) < _CACHE_TTL_SEC:
            return cached['variants']

    variants = _call_llm(query)
    if not variants:
        return [query]
    out = [query] + [v for v in variants if v.lower().strip() != query.lower().strip()]
    _CACHE[query.lower().strip()] = {'ts': time.time(), 'variants': out}
    return out


_TRIVIAL_RE = re.compile(r'^\s*(hi|hello|thanks|thank you|ok|yes|no)\s*[.!?]?$', re.IGNORECASE)


def _should_expand(query: str) -> bool:
    """Skip expansion for trivial responses; expand for content questions."""
    if not query or len(query) < 5:
        return False
    if _TRIVIAL_RE.match(query):
        return False
    return True
