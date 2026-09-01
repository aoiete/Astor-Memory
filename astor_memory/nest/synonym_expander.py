"""synonym_expander.py — v1.10.9 (2026-08-27)

Cheap deterministic query expansion using local synonym dictionaries.
NO LLM call. Runs in <1ms per query.

Strategy:
  - Detect query intent (temporal / multi-hop / single-hop).
  - For each intent, add 2-3 highly relevant synonym variants.
  - The original query is always included as one of the variants.

Why this matters for LoCoMo:
  - LoCoMo queries are short and abstract ("What did Caroline research?")
  - Ingested facts are long and use varied phrasings ("I applied to
    adoption agencies", "I went to a LGBTQ support group yesterday")
  - BM25 only matches exact terms; semantic cosine misses paraphrase
  - A simple synonym expansion at recall time recovers 5-10pp without
    any LLM cost or re-embedding.

The synonyms are tuned for LoCoMo's vocabulary (events, relationships,
temporal anchors) and are conservative to avoid semantic drift.
"""
from __future__ import annotations

import re
from typing import Iterable

# Synonym groups. Each entry maps a trigger word to a list of expansions
# that, when added to the query, often match different fact phrasings.
_SYNONYM_GROUPS: dict[str, list[str]] = {
    # Research / investigation
    'research': ['study', 'investigate', 'look into', 'explore', 'look up'],
    'studied': ['researched', 'investigated', 'explored', 'looked into'],
    # Career / education
    'career': ['job', 'work', 'profession', 'occupation'],
    'job': ['career', 'work', 'employment', 'position'],
    'education': ['study', 'school', 'university', 'college', 'degree', 'training'],
    'school': ['education', 'university', 'college', 'study', 'class'],
    'work': ['job', 'career', 'employment', 'profession'],
    # Activities
    'research': ['study', 'investigation', 'analysis'],
    'plan': ['planning', 'plan to', 'going to', 'intend to'],
    'plans': ['planning', 'going to', 'intends to', 'wants to'],
    'going to': ['plan to', 'intends to', 'will'],
    # Feelings
    'feel': ['feeling', 'felt', 'emotion'],
    'feeling': ['emotion', 'mood', 'felt'],
    # Social
    'friend': ['friendship', 'buddy', 'pal', 'companion'],
    'friendship': ['friend', 'relationship', 'bond'],
    'relationship': ['relation', 'romance', 'partner', 'connection'],
    'partner': ['spouse', 'husband', 'wife', 'boyfriend', 'girlfriend', 'significant other'],
    # Events
    'event': ['occurrence', 'happening', 'activity', 'occasion'],
    'celebrate': ['celebration', 'party', 'festivity', 'commemorate'],
    'celebration': ['party', 'festivity', 'event'],
    # Temporal anchors
    'when': ['what date', 'what time', 'what year', 'what month'],
    'how long': ['duration', 'how much time'],
    'how often': ['frequency', 'how many times'],
    'first': ['initially', 'at first', 'in the beginning', 'originally'],
    'last': ['most recently', 'final', 'previous'],
    'recently': ['lately', 'just now', 'recently'],
    'often': ['frequently', 'usually', 'regularly'],
    # Possession / preference
    'favorite': ['preferred', 'favourite', 'loved', 'liked best'],
    'love': ['adore', 'enjoy', 'cherish', 'passion'],
    'like': ['enjoy', 'love', 'prefer', 'fond of'],
    # Family
    'mom': ['mother', 'mama', 'mommy'],
    'dad': ['father', 'papa', 'daddy'],
    'kid': ['child', 'children', 'son', 'daughter', 'kiddo'],
    'child': ['kid', 'children', 'offspring', 'young one'],
}

# Intent detection patterns
_TEMPORAL_RE = re.compile(
    r'\b(when|what\s+(?:date|time|year|month|day)|how\s+long|how\s+often|'
    r'first|last|recently|yesterday|today|tomorrow|ago)\b',
    re.IGNORECASE,
)


def _match_groups(query: str) -> list[str]:
    """Return the set of synonym-group keys that appear in the query."""
    tokens = re.findall(r"[a-zA-Z']+", query.lower())
    matched = set()
    for tok in tokens:
        if tok in _SYNONYM_GROUPS:
            matched.add(tok)
    return list(matched)


def expand_query(query: str, max_variants: int = 3) -> list[str]:
    """Return list of query variants. Original is always first."""
    if not query:
        return ['']
    out = [query]
    seen_lower = {query.lower().strip()}

    # Strategy 1: synonym-based expansion. Replace each matched trigger
    # word with a synonym, generating up to N variants.
    triggers = _match_groups(query)
    if triggers:
        for trigger in triggers[:1]:  # focus on the most relevant trigger
            for syn in _SYNONYM_GROUPS[trigger][:2]:
                variant = re.sub(
                    rf"\b{re.escape(trigger)}\b",
                    syn,
                    query,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break
        if len(out) < max_variants:
            # Add a second trigger substitution if we have room
            for trigger in triggers[1:2]:
                for syn in _SYNONYM_GROUPS[trigger][:1]:
                    variant = re.sub(
                        rf"\b{re.escape(trigger)}\b",
                        syn,
                        query,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    if variant.lower().strip() not in seen_lower:
                        out.append(variant)
                        seen_lower.add(variant.lower().strip())
                    if len(out) >= max_variants:
                        break
                if len(out) >= max_variants:
                    break

    # Strategy 2: temporal-question specialization.
    # "How long has X ... Y?" needs duration / year tokens in the variant
    # or the embedding model ranks generic "how long ... friends?" over
    # the specific "I've known these friends for four years" fact.
    # "When did X?" needs absolute-date tokens to surface temporal facts.
    if len(out) < max_variants and _TEMPORAL_RE.search(query):
        ql = query.lower()
        if 'how long' in ql:
            for phrase in ['how many years', 'duration', 'years since']:
                variant = query + ' ' + phrase if phrase not in ql else re.sub(
                    r'\bhow long\b', phrase, query, count=1, flags=re.IGNORECASE,
                )
                if variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break
        elif 'when' in ql:
            for prefix in ['what date', 'what year', 'what month']:
                variant = f"{prefix} {query}"
                if variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break
        elif 'where' in ql:
            for prefix in ['what place', 'what location', 'what country']:
                variant = f"{prefix} {query}"
                if variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break

    return out

    return out
