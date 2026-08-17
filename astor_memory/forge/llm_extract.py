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
import time
import requests
from typing import Any

from .extractor import AstorFact


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
                '  - context: 1-2 sentence summary explaining what this fact is about'
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


def _call_anthropic(text: str, api_key: str, base_url: str = 'https://api.anthropic.com/v1',
                    model: str = 'claude-3-5-haiku-latest', timeout: int = 30) -> list[dict]:
    """Call Anthropic Messages API."""
    body = {
        'model': model,
        'max_tokens': 600,
        'system': 'You extract facts. Output raw JSON only. No markdown. No explanation.',
        'messages': [
            {'role': 'user', 'content': f'Extract structured facts from: {text}\nReturn as JSON array with fields: content, kind, confidence, importance, tags.'},
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
                'text': f'You extract facts. Output raw JSON only. No markdown. No explanation.\nExtract structured facts from: {text}\nReturn as JSON array with fields: content, kind, confidence, importance, tags.',
            }],
        }],
        'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 600},
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
        'prompt': f'You extract facts. Output raw JSON only. No markdown. No explanation.\nExtract structured facts from: {text}\nReturn as JSON array with fields: content, kind, confidence, importance, tags.',
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
    depth = 0
    in_str = False
    esc = False
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
                import json
                return json.loads(content[start:i+1])
    return []


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
                retry_per_provider: int = 2, timeout: int = 30) -> list[AstorFact]:
    """LLM extract with fallback provider chain.

    Per Plan § LLM fallback provider:
    - Primary provider tried first (default: m3)
    - Fallback chain tried in order if primary fails
    - Each provider retried retry_per_provider times before next
    - If ALL fail, falls back to regex extraction (graceful degradation)
    """
    fallback_chain = fallback_chain or []
    providers = [primary] + [p for p in fallback_chain if p != primary]

    last_error: Exception | None = None
    for provider in providers:
        api_key = astor_get_api_key(provider)
        if not api_key and provider != 'ollama':
            # Skip provider with no key (except ollama which is local)
            continue
        for retry in range(retry_per_provider):
            try:
                raw_facts = _call_provider(provider, text, api_key, timeout=timeout)
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
                    )
                    for f in raw_facts
                ]
            except Exception as e:
                last_error = e
                continue  # next retry of same provider

    # All providers failed (or no keys); fall back to regex
    from .extractor import astor_regex_extract
    return astor_regex_extract(text)


__all__ = ['astor_llm_extract', 'astor_get_api_key', 'PROVIDER_ENV_KEYS']
