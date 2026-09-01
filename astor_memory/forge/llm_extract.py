"""
LLM-based extraction with fallback provider chain.

Per Plan § LLM fallback provider:
- Primary: m3 (default)
- Fallback chain: configurable
- Retry per provider, then next
"""

from __future__ import annotations

import os
import re
import requests
from .extractor import AstorFact


# 2026-08-25: L0/L1 provenance helpers. Used as fallback when LLM
# extractor (M3/gemini/anthropic) doesn't emit abstract + overview fields.
# OpenViking-style progressive loading: L0 fits in one sentence (~80 tokens),
# L1 is a structured digest (~300 tokens).
def _derive_abstract(content: str) -> str:
    """L0 abstract: first sentence, capped at ~80 tokens (~320 chars)."""
    import re as _re
    if not content:
        return ''
    # Split on sentence terminator (English + Chinese) or newline
    parts = _re.split(r'[.!?。！？\n]', content.strip(), maxsplit=1)
    abstract = parts[0].strip()
    if len(abstract) > 320:
        abstract = abstract[:317] + '...'
    return abstract


def _derive_overview(content: str) -> str:
    """L1 overview: first 240 chars of content (~60 tokens)."""
    if not content:
        return ''
    return content[:240].strip()


def _call_m3(text: str, api_key: str, base_url: str = 'https://api.minimax.io/v1',
             model: str = 'MiniMax-M3', timeout: int = 30) -> list[dict]:
    """Call M3 API (default primary). Uses OpenAI-compatible /chat/completions format."""
    return _call_openai(text, api_key, base_url=base_url, model=model, timeout=timeout)


def _call_openai(text: str, api_key: str, base_url: str = 'https://api.openai.com/v1',
                model: str = 'gpt-4o-mini', timeout: int = 30) -> list[dict]:
    """Call OpenAI Chat Completions API."""
    body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': (
                'You extract structured facts. Output raw JSON only. No markdown. No explanation.\n\n'
                'Each fact object has fields:\n'
                '  - content: short factual statement (1-2 sentences)\n'
                '  - kind: one of (fact, user_preference, decision, event, trading_fact)\n'
                '  - confidence: 0.0-1.0\n'
                '  - importance: 0.0-1.0\n'
                '  - tags: list of short strings\n'
                '  - keywords: list of 3-7 distinct keywords/phrases that summarize the fact (for search rerank)\n'
                '  - context: 1-2 sentence summary explaining what this fact is about\n'
                '  - event_date: ISO-8601 date "YYYY-MM-DD" if the fact mentions a specific past/future date, else null\n'
                '  - event_date_precision: "day" if YYYY-MM-DD known, "month" if only YYYY-MM, "year" if only YYYY, "none" if no date\n'
                '  - abstract: L0 one-sentence summary of the fact (≤80 tokens / ≤320 chars). MANDATORY for every fact. Used for progressive loading in system prompt.\n'
                '  - overview: L1 structured digest with key params/details (≤300 tokens / ≤1200 chars). Recommended for facts >2 sentences. MANDATORY for decisions/trading_facts.\n'
                'When the input mentions "today" / "yesterday" / relative dates, infer the absolute date from context if possible; otherwise leave null.'
            )},
            {'role': 'user', 'content': f'Extract structured facts from: {text}\nReturn as JSON array.'},
        ],
        'temperature': 0.1,
        'max_tokens': 800,
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    r = requests.post(f'{base_url}/chat/completions', json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    content = r.json()['choices'][0]['message']['content']
    return _parse_json_array(content)


# v1.10.8 (2026-08-26): unified prompt used by ALL providers. Previously
# anthropic/gemini/ollama had a minimal prompt (only 5 fields), which meant
# falling back from primary (m3/openai) to a fallback provider silently
# dropped keywords/context/event_date/abstract/overview from the output.
# Extracting once + centralizing here fixes the prompt consistency bug.
_UNIFIED_SYSTEM_PROMPT = (
    'You extract structured facts. Output raw JSON only. No markdown. No explanation.\n\n'
    'Each fact object has fields:\n'
    '  - content: short factual statement (1-2 sentences)\n'
    '  - kind: one of (fact, user_preference, decision, event, trading_fact)\n'
    '  - confidence: 0.0-1.0\n'
    '  - importance: 0.0-1.0\n'
    '  - tags: list of short strings\n'
    '  - keywords: list of 3-7 distinct keywords/phrases that summarize the fact (for search rerank)\n'
    '  - context: 1-2 sentence summary explaining what this fact is about\n'
    '  - event_date: ISO-8601 date "YYYY-MM-DD" if the fact mentions a specific past/future date, else null\n'
    '  - event_date_precision: "day" if YYYY-MM-DD known, "month" if only YYYY-MM, "year" if only YYYY, "none" if no date\n'
    '  - abstract: L0 one-sentence summary of the fact (≤80 tokens / ≤320 chars). MANDATORY for every fact. Used for progressive loading in system prompt.\n'
    '  - overview: L1 structured digest with key params/details (≤300 tokens / ≤1200 chars). Recommended for facts >2 sentences. MANDATORY for decisions/trading_facts.\n'
    '  - topic: short noun phrase (≤5 words) grouping this fact into a session-level theme (e.g. "AXTI monthly backtest", "MoMoo TFSA rebalance"). MANDATORY — empty string if no clear theme.\n'
    '  - session_id: stable identifier for the conversation/session this fact came from (e.g. "telegram-2026-08-29-2230", "wechat-sunday-2026-08-13"). If unknown, use the doc timestamp as the session anchor (e.g. "doc-2026-08-29"). MANDATORY.\n'
    'CRITICAL — TEMPORAL NORMALIZATION (v1.10.9):\n'
    ' - When the input mentions ANY relative time ("yesterday", "last week", "3 days ago", "tomorrow", "next Friday", "the other day", "last Monday"), you MUST resolve it to an absolute ISO-8601 date based on the conversation timestamp provided in the document.\n'
    ' - The system ALWAYS prepends a `[Doc timestamp: YYYY-MM-DD ...]` marker to each document. Use that as the anchor date ("today") when resolving relative references.\n'
    ' - Always set event_date to the resolved absolute YYYY-MM-DD string — never leave null when a relative time was mentioned.\n'
    ' - For multi-day events or ambiguous relative terms ("recently", "a while back"), set event_date to the start day and use precision "day" or "month".\n'
    ' - For raw absolute dates already in the text (e.g. "May 7, 2023"), keep them as-is in YYYY-MM-DD form.\n'
    'When the input mentions "today" / "yesterday" / relative dates WITHOUT a doc-timestamp anchor, infer from context if possible; otherwise leave null.\n'
    'CRITICAL — ENTITY PRESERVATION (v1.11, 2026-08-27):\n'
    ' - Keep ALL named entities verbatim in `content`. NEVER generalize, paraphrase, or drop them.\n'
    ' - Book/movie/song/article titles → keep exact title with quotes, e.g. content="Caroline loved the book \\"Becoming Nicole\\" by Amy Ellis Nutt."\n'
    ' - People names → keep full name, e.g. "Amy Ellis Nutt" not "the author".\n'
    ' - Places (country, city, venue) → keep exact name, e.g. "Sweden" not "Europe".\n'
    ' - Numbers (ages, counts, dates, quantities) → preserve as written. "3 children" not "some children". "13 August" not "mid-August".\n'
    ' - If multiple entities of the same type appear (e.g. several book titles), preserve EACH as a separate fact — do not collapse them.\n'
    ' - When in doubt, prefer a longer fact that retains all entities over a shorter one that drops them.\n'
    'This is the highest-priority rule. A fact that drops entity information is wrong even if other fields look correct.\n'
)


def _call_anthropic(text: str, api_key: str, base_url: str = 'https://api.anthropic.com/v1',
                    model: str = 'claude-3-5-haiku-latest', timeout: int = 30) -> list[dict]:
    """Call Anthropic Messages API."""
    body = {
        'model': model,
        'max_tokens': 800,
        'system': _UNIFIED_SYSTEM_PROMPT,
        'messages': [
            {'role': 'user', 'content': f'Extract structured facts from: {text}\nReturn as JSON array.'},
        ],
    }
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'Content-Type': 'application/json',
    }
    r = requests.post(f'{base_url}/messages', json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    content = r.json()['content'][0]['text']
    return _parse_json_array(content)


def _call_gemini(text: str, api_key: str, base_url: str = 'https://generativelanguage.googleapis.com/v1beta',
                 model: str = 'gemini-1.5-flash', timeout: int = 30) -> list[dict]:
    """Call Google Gemini generateContent API."""
    url = f'{base_url}/models/{model}:generateContent?key={api_key}'
    body = {
        'contents': [{
            'parts': [{
                # v1.10.8: use unified system prompt + full field list (was minimal).
                'text': _UNIFIED_SYSTEM_PROMPT + f'\n\nExtract structured facts from: {text}\nReturn as JSON array.',
            }],
        }],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 800},
    }
    r = requests.post(url, json=body, timeout=timeout)
    r.raise_for_status()
    content = r.json()['candidates'][0]['content']['parts'][0]['text']
    return _parse_json_array(content)


def _call_ollama(text: str, api_key: str = '',  # ollama is local, no key needed
                 base_url: str = 'http://localhost:11434', model: str = 'llama3.2', timeout: int = 60) -> list[dict]:
    """Call local Ollama API (no API key, localhost)."""
    body = {
        'model': model,
        # v1.10.8: use unified system prompt + full field list (was minimal).
        'prompt': _UNIFIED_SYSTEM_PROMPT + f'\n\nExtract structured facts from: {text}\nReturn as JSON array.',
        'stream': False,
        'options': {'temperature': 0.1},
    }
    r = requests.post(f'{base_url}/api/generate', json=body, timeout=timeout)
    r.raise_for_status()
    content = r.json()['response']
    return _parse_json_array(content)


def _parse_json_array(content: str) -> list[dict]:
    """Strip thinking/markdown/fences + extract first balanced JSON array.

    Shared by all providers since all LLM responses need the same cleanup.
    Per Plan § Forge M3 think handling + general markdown strip.
    """
    # Strip think blocks (M3 specific)
    if '<think>' in content:
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    # Strip markdown code fences
    content = re.sub(r'^```(?:json)?\s*', '', content.strip())
    content = re.sub(r'\s*```\s*$', '', content.strip())

    # Find first balanced JSON array (handles strings with escaped quotes)
    start = content.find('[')
    if start < 0:
        return []
    # v1.11 (2026-08-27): require content between [ and ] — empty brackets
    # happen when Gemini prepends whitespace/newlines; we want to detect
    # that as "no facts" not "parsed successfully".
    # Find matching ] starting from a non-whitespace character.
    depth = 0
    in_str = False
    esc = False
    end_idx = -1
    for i in range(start, len(content)):
        c = content[i]
        if esc:
            esc = False
            continue
        if c == '\\':
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx < 0:
        return []
    # Ensure the slice has actual content between [ and ] (not just whitespace)
    inner = content[start+1:end_idx].strip()
    if not inner:
        return []
    import json
    try:
        parsed = json.loads(content[start:end_idx+1])
    except json.JSONDecodeError:
        return []
    # v1.11 (2026-08-27): Gemini-3.7-flash sometimes returns a flat string array
    # `["fact 1", "fact 2", ...]` instead of the rich object array the prompt
    # requested. Wrap each string into a minimal fact dict so downstream code
    # (which expects {content, kind, ...}) still works.
    if isinstance(parsed, list):
        if not parsed:
            return []
        if all(isinstance(x, str) for x in parsed):
            return [
                {
                    'content': s,
                    'kind': 'fact',
                    'confidence': 0.7,
                    'importance': 0.5,
                    'tags': [],
                    'keywords': s.split()[:5],
                    'context': s[:200],
                    'event_date': None,
                    'event_date_precision': 'none',
                    'abstract': s[:320],
                    'overview': s[:1200],
                }
                for s in parsed
            ]
    return parsed


def _call_provider(provider: str, text: str, api_key: str, **kwargs) -> list[dict]:
    """Call a specific LLM provider. Returns raw fact dicts.

    Supported: m3, openai, anthropic, gemini, ollama, deepseek, zhipu.
    Each provider has its own env var for api_key (set by caller).
    """
    if provider == 'm3':
        return _call_m3(text, api_key, **kwargs)
    elif provider == 'openai':
        return _call_openai(text, api_key, **kwargs)
    elif provider == 'anthropic':
        return _call_anthropic(text, api_key, **kwargs)
    elif provider == 'gemini':
        return _call_gemini(text, api_key, **kwargs)
    elif provider == 'ollama':
        return _call_ollama(text, **kwargs)
    elif provider == 'deepseek':
        # DeepSeek uses OpenAI-compatible API
        return _call_openai(text, api_key, base_url='https://api.deepseek.com/v1', model='deepseek-chat', **kwargs)
    elif provider == 'zhipu':
        # 智谱 GLM uses OpenAI-compatible API
        return _call_openai(text, api_key, base_url='https://open.bigmodel.cn/api/paas/v4', model='glm-4-flash', **kwargs)
    else:
        raise ValueError(f'Unknown provider: {provider}')


# Env var mapping per provider (Plan § LLM fallback provider chain)
PROVIDER_ENV_KEYS: dict[str, str] = {
    'm3': 'MINIMAX_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'gemini': 'GOOGLE_API_KEY',
    'ollama': '',  # local, no key
    'deepseek': 'DEEPSEEK_API_KEY',
    'zhipu': 'ZHIPU_API_KEY',
}


def astor_get_api_key(provider: str) -> str:
    """Read provider API key from env (with fallback aliases)."""
    env_var = PROVIDER_ENV_KEYS.get(provider, '')
    if not env_var:
        return ''
    return os.environ.get(env_var, '')


def astor_llm_extract(text: str, primary: str = 'm3', fallback_chain: list[str] | None = None,
                retry_per_provider: int = 2, timeout: int = 30,
                base_url: str | None = None, model: str | None = None) -> list[AstorFact]:
    """LLM extract with fallback provider chain.

    Per Plan § LLM fallback provider:
    - Primary provider tried first (default: m3)
    - Fallback chain tried in order if primary fails
    - Each provider retried retry_per_provider times before next
    - If ALL fail, falls back to regex extraction (graceful degradation)

    v1.10.9 (2026-08-27): accept base_url/model kwargs so callers can route
    'openai' provider through OpenRouter-compatible endpoints (OPENAI_BASE_URL
    env var) and pin the model (ASTOR_LLM_MODEL env var).
    """
    fallback_chain = fallback_chain or []
    providers = [primary] + [p for p in fallback_chain if p != primary]

    # v1.10.9: pass base_url/model through to provider-specific callers.
    _provider_extra = {}
    if base_url:
        _provider_extra['base_url'] = base_url
    if model:
        _provider_extra['model'] = model

    last_error: Exception | None = None
    for provider in providers:
        api_key = astor_get_api_key(provider)
        if not api_key and provider != 'ollama':
            # Skip provider with no key (except ollama which is local)
            continue
        for retry in range(retry_per_provider):
            try:
                raw_facts = _call_provider(provider, text, api_key, timeout=timeout, **_provider_extra)
                return [
                    AstorFact(
                        content=f.get('content', ''),
                        kind=f.get('kind', 'fact'),
                        confidence=f.get('confidence', 0.7),
                        importance=f.get('importance', 0.5),
                        tags=f.get('tags', []),
                        # v1.2.0: A-MEM-style structured fields. LLM providers
                        # that don't yet return them (legacy prompts, smaller
                        # models) get safe fallbacks.
                        keywords=f.get('keywords') or [],
                        context=str(f.get('context', '') or '')[:500],
                        # v1.3.0 (2026-08-25): temporal fields for temporal rerank
                        event_date=f.get('event_date') or None,
                        event_date_precision=str(f.get('event_date_precision') or 'none')[:16],
                        # v1.6.0 (2026-08-25): L0/L1 progressive loading
                        # (OpenViking-style). LLM emits abstract + overview
                        # when present; fall back to deriving from content.
                        abstract=str(f.get('abstract', '') or '').strip()[:500] or _derive_abstract(
                            f.get('content', '')
                        ),
                        overview=str(f.get('overview', '') or '').strip()[:1500] or _derive_overview(
                            f.get('content', '')
                        ),
                        # v1.12.0 (2026-08-29): hierarchical extraction per
                        # Mem0 2026 lesson. Falls back to empty string when LLM
                        # doesn't emit these fields (legacy providers).
                        topic=str(f.get('topic', '') or '').strip()[:100],
                        session_id=str(f.get('session_id', '') or '').strip()[:128],
                    )
                    for f in raw_facts
                ]
            except Exception as e:
                last_error = e
                continue  # next retry of same provider

    # All providers failed (or no keys); fall back to regex
        from .extractor import astor_regex_extract
        from .relative_date import resolve_relative_dates_batch
        regex_facts = astor_regex_extract(text)
        return resolve_relative_dates_batch(regex_facts, anchor=None)


    def astor_llm_extract_with_anchor(
        text: str,
        anchor: str | None = None,
        primary: str = 'm3',
        fallback_chain: list[str] | None = None,
        retry_per_provider: int = 2,
        timeout: int = 30,
    ):
        """v1.10.9 (2026-08-26): LLM extract with explicit document timestamp anchor.

        The anchor (e.g. "2023-05-08") tells the extractor what "yesterday / last
        week" should resolve to. We also apply a deterministic regex-based
        post-processor (relative_date.resolve_relative_dates_batch) so facts that
        the LLM still left without event_date get a resolved absolute date from
        the anchor.
        """
        from .relative_date import resolve_relative_dates_batch

        if anchor:
            anchor_note = f'\n\n[Doc timestamp: {anchor}] — Treat this as "today" when resolving relative time references (yesterday / last week / 3 days ago / next Friday / etc.).\n'
            annotated_text = anchor_note + text
        else:
            annotated_text = text

        facts = astor_llm_extract(
            annotated_text,
            primary=primary,
            fallback_chain=fallback_chain,
            retry_per_provider=retry_per_provider,
            timeout=timeout,
        )

        if anchor and facts:
            from dataclasses import asdict, is_dataclass
            dict_facts = [asdict(f) if is_dataclass(f) else f.__dict__ for f in facts]
            resolved = resolve_relative_dates_batch(dict_facts, anchor)
            for f, src in zip(facts, resolved):
                if src.get('event_date'):
                    f.event_date = src.get('event_date')
                if src.get('event_date_precision'):
                    f.event_date_precision = src.get('event_date_precision')

        return facts


    def astor_llm_extract_with_provider(
        text: str,
        primary: str = 'm3',
        fallback_chain: list[str] | None = None,
        retry_per_provider: int = 2,
        timeout: int = 30,
    ) -> tuple[list, str]:
        """v1.10.8 (2026-08-26): same as astor_llm_extract but returns the ACTUAL
    provider name that produced the result, so audit logs are honest.

    Returns: (facts_list, actual_provider_name) where actual_provider_name is
    one of:
      - 'm3' / 'openai' / 'anthropic' / 'gemini' / 'ollama' / 'deepseek' / 'zhipu'
      - 'regex_fallback' — when all LLM providers failed and we used regex

    Backward-compatible: callers that need just the list can still call
    astor_llm_extract() unchanged.
    """
    fallback_chain = fallback_chain or []
    providers = [primary] + [p for p in fallback_chain if p != primary]

    last_error: Exception | None = None
    for provider in providers:
        api_key = astor_get_api_key(provider)
        if not api_key and provider != 'ollama':
            continue
        # 2026-08-27 fix: read OPENAI_BASE_URL env so 'openai' provider can
        # route through OpenRouter (or any other OpenAI-compatible endpoint).
        # Without this, _call_openai hits api.openai.com and fails because
        # the deployed key is for openrouter.ai.
        import os as _os_e
        _openai_base = _os_e.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        _provider_kwargs = {
            'timeout': timeout,
            'base_url': _openai_base,
            # Default model when caller doesn't pin one. Use the cheaper
            # flash model for extraction.
            'model': _os_e.environ.get("ASTOR_LLM_MODEL", "google/gemini-3.7-flash"),
        }
        for retry in range(retry_per_provider):
            try:
                raw_facts = _call_provider(provider, text, api_key, **_provider_kwargs)
                facts = [
                    AstorFact(
                        content=f.get('content', ''),
                        kind=f.get('kind', 'fact'),
                        confidence=f.get('confidence', 0.7),
                        importance=f.get('importance', 0.5),
                        tags=f.get('tags', []),
                        keywords=f.get('keywords') or [],
                        context=str(f.get('context', '') or '')[:500],
                        event_date=f.get('event_date') or None,
                        event_date_precision=str(f.get('event_date_precision') or 'none')[:16],
                        abstract=str(f.get('abstract', '') or '').strip()[:500] or _derive_abstract(
                            f.get('content', '')
                        ),
                        overview=str(f.get('overview', '') or '').strip()[:1500] or _derive_overview(
                            f.get('content', '')
                        ),
                    )
                    for f in raw_facts
                ]
                return facts, provider  # SUCCESS — record actual provider
            except Exception as e:
                last_error = e
                continue

    # All LLM providers failed; fall back to regex
    from .extractor import astor_regex_extract
    return astor_regex_extract(text), 'regex_fallback'


__all__ = [
    'astor_llm_extract',
    'astor_llm_extract_with_provider',
    'astor_get_api_key',
    'PROVIDER_ENV_KEYS',
]
