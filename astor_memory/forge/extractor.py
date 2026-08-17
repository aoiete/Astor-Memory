"""
Fact extraction with extract_mode parameter.

Per Plan § Bus direct entry:
- 'auto': regex for short (< 200 chars), none for long (> 1000 chars), regex default
- 'none': store raw, no fact extraction
- 'regex': extract via regex patterns (no LLM)
- 'llm': extract via M3 LLM call (4s, $0.004)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

AstorExtractMode = Literal['auto', 'none', 'regex', 'llm']

# Regex patterns for fact categorization (Plan § Forge regex patterns)
PATTERNS = [
    (r'(?:I (?:prefer|preferred|like|liked|love|loved|hate|hated|dislike|disliked)|我喜欢|我讨厌|我偏好)\s+(.+)', 'user_preference'),
    (r'(?:I (?:decide|decided|will|choose|chose|chose)|我决定|让我|让我们)\s+(.+)', 'decision'),
    (r'(?:today|yesterday|刚才|现在|今天|昨天)\s+(.+)', 'event'),
    (r'(\w+)\s+(?:price|stock|收盘|股价)\s+(.+)', 'trading_fact'),
    (r'(.+)', 'fact'),  # catch-all
]


@dataclass
class AstorFact:
    """Extracted fact from text (before storage).

    v1.2.0 (2026-08-16): added keywords + context fields. LLM mode
    populates them via prompt; regex mode derives heuristically.
    """
    content: str
    kind: str
    confidence: float = 0.7
    importance: float = 0.5
    tags: list[str] | None = None
    keywords: list[str] | None = None   # A-MEM-style; JSON-encoded into DB
    context: str = ''                  # A-MEM-style; human-readable 1-2 sentences


def astor_regex_extract(text: str) -> list[AstorFact]:
    """Extract facts using regex patterns. No LLM.

    v1.2.0: also derives keywords + context heuristically:
    - keywords = [kind, ...content_words] (top-5 distinctive tokens)
    - context = first 120 chars of input text
    """
    import re as _re
    facts = []
    for pattern, kind in PATTERNS:
        m = _re.match(pattern, text, _re.IGNORECASE)
        if m:
            # Heuristic keywords: kind + up to 5 longest words >3 chars
            content_part = m.group(1).strip()
            words = _re.findall(r'[a-zA-Z一-鿿]{4,}', content_part)
            distinct_words = list(dict.fromkeys(words))[:5]  # preserve order, dedup
            facts.append(AstorFact(
                content=content_part,
                kind=kind,
                confidence=0.7,
                importance=0.5,
                tags=[kind, 'auto_extracted'],
                keywords=[kind] + distinct_words,
                context=text[:120].strip(),
            ))
            break  # one fact per match
    return facts


def astor_detect_capture_intent(text: str) -> bool:
    """Detect phrases signaling 'remember this' (Plan § Insight 17)."""
    phrases = [
        r'\bremember (?:this|that)\b',
        r'\bfrom now on\b',
        r'\bI (?:changed|updated|switched to)\b',
        r'\bactually[,.]?\s',
        r'\bdon\'?t forget\b',
        r'\bremember\b',
        r'\bkeep (?:in mind|note of)\b',
        r'\bimportant\b',
        r'\b关键\b',
        r'\b记住\b',
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in phrases)


def astor_choose_extract_mode(text: str) -> AstorExtractMode:
    """Per Plan § Bus direct entry auto-mode heuristic."""
    text_len = len(text)
    if text_len < 200:
        return 'regex'
    if text_len > 1000:
        return 'none'
    return 'regex'


def astor_extract_facts(
    text: str,
    mode: AstorExtractMode = 'auto',
    *,
    tier: str = 'public',
    user_id: str | None = None,
    actor: str = 'system',
) -> list[AstorFact]:
    """Extract facts based on mode.

    P1-fix 2026-08-15: also persist an llm_call_log row to the per-tier forge
    DB so the audit trail is complete. Latency/model/provider tracked.
    """
    import hashlib, time as _time, json as _json
    from . import astor_forge_log_call

    t0 = _time.time()
    if mode == 'auto':
        mode = astor_choose_extract_mode(text)

    facts: list[AstorFact] = []
    success = 1
    error_msg = None
    provider = 'regex_fallback'
    model_name = None

    try:
        if mode == 'none':
            return []  # Store raw event, no fact extraction

        if mode == 'regex':
            facts = astor_regex_extract(text)
            # Insight 17: capture-intent auto-detection
            if astor_detect_capture_intent(text):
                for f in facts:
                    f.confidence = 0.95  # user wants remembered
                    if f.tags is None:
                        f.tags = []
                    f.tags.append('capture_intent')

        elif mode == 'llm':
            # LLM extract (lazy import to avoid loading requests when not needed)
            from .llm_extract import astor_llm_extract
            provider = 'llm'
            facts = astor_llm_extract(text)
    except Exception as exc:
        success = 0
        error_msg = str(exc)[:200]
        facts = []

    # P1-fix 2026-08-15: log the extraction call. Audit row is mandatory per
    # ACL plan even if extraction itself produced 0 facts (records the attempt).
    try:
        input_hash = hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()
        astor_forge_log_call(
            actor=actor,
            user_id=user_id or '_current',
            tier=tier,
            provider=provider,
            model=model_name,
            operation='extract',
            input_hash=input_hash,
            input_length=len(text),
            output_json=_json.dumps([f.content for f in facts])[:4000] if facts else None,
            success=success,
            error_msg=error_msg,
            latency_ms=int((_time.time() - t0) * 1000),
            reason=f'mode={mode}',
        )
    except Exception as log_exc:
        # Audit failure must not block write path; surface to stderr.
        import sys as _sys
        print(f'[astor.forge] log_call failed: {log_exc}', file=_sys.stderr)

    return facts


__all__ = [
    'AstorFact', 'AstorExtractMode', 'astor_extract_facts',
    'astor_regex_extract', 'astor_choose_extract_mode', 'astor_detect_capture_intent',
]
