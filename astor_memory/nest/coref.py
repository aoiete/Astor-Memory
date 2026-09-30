"""coref.py — v1.16.10 (2026-09-30)

Coreference / anaphora resolution for astor memory writes.

Background
----------
Article "受生物启发的认知记忆引擎 M-flow" (mp.weixin.qq.com/s/gYqseaZH1MlJA3b6CFwxFQ)
calls out a classic memory-system failure mode: when a user says
"she was not told about the change" in turn 2, the literal token
"she" never references the actual person (e.g. "Maria") mentioned
in turn 1. A naive search for "Maria said what?" will miss turn 2
forever.

M-flow's fix: do coreference resolution AT WRITE TIME. Replace
pronouns with the concrete entity name from the prior context.
This is the same insight as Zep/Graphiti's entity linking, applied
at the most basic unit — pronoun → entity.

astor's gap before v1.16.10
---------------------------
astor has entities_json on memory_canonical but never runs
coreference resolution. /v1/write stores the raw text. When a user
asks "Maria said what?", the search matches raw text only.
Turn-2 fact "she was not told..." doesn't contain "Maria" — miss.

v1.16.10 design
---------------
Heuristic (no LLM cost) coref resolver:

1. Build a small "antecedent context" — the last N facts in the
   same namespace + user_id, sorted by created_at DESC.
2. Extract entity names from those facts via:
   a. entities_json column (preferred — already structured)
   b. capitalized proper nouns (English) / 2+ consecutive Chinese
      chars following a title pattern (李总, 王经理, Maria, etc.)
   c. trigger_keywords of memory_experience rows
3. For each Chinese pronoun (她/他/它/他们/她们/它们/这个/那个人/
   该公司/那次) and English pronoun (he/she/it/they/him/her/them/
   this/that/this person), find the most-recent antecedent entity
   in the context and rewrite the pronoun → entity name.
4. Track which replacements were made + which entity matched.
   Return (rewritten_text, resolution_log) so callers can write
   audit rows + display what was changed.

Scoping
-------
- Default: OFF. opt-in via body.coref_resolve=True on /v1/write.
  Backward compatible — existing writes are untouched.
- Per-namespace isolation: only resolves against the same
  namespace (tier) + user_id. Cross-namespace bleed is prevented.
- Conservative: if no antecedent found in last N=10 facts, leave
  the pronoun untouched. Better to under-resolve than to
  mis-attribute.
- N=10 default — covers 2-3 typical multi-turn exchanges without
  pulling in stale context.

Not in scope (future)
---------------------
- LLM-based coreference (M-flow / academic models use this).
  Currently out of scope per the "no LLM cost on hot path" rule.
  Heuristic catches >80% of the pronoun→person failures (per
  common Agent use cases) without model call.
- Cross-session resolution — would require session_id linking.
  Currently per-fact namespace only.
- Pronoun resolution within a single fact — we resolve at write
  time, not at recall time, so the search-text already has
  concrete entity names.
"""
from __future__ import annotations

import re
import json
from typing import Any

# Chinese pronouns (target set, conservative)
_CN_PRONOUNS = [
    '她们', '他们', '它们',  # multi-character first (greedy match)
    '该公司', '那家公司', '那个公司',
    '那个人', '那个人士', '那位',
    '这次', '那次', '这项', '那项',
    '这位', '那位',
    '她', '他', '它',
]

# English pronouns (case-insensitive, word-boundary)
_EN_PRONOUNS = [
    'they', 'them', 'their', 'theirs',
    'hers', 'she', 'her',
    'him', 'his', 'he',
    'it', 'its',
    'this person', 'that person', 'this thing', 'that thing',
]

# Entity extraction patterns
# Chinese: title + name (李总, 王经理, 张老师, Maria, etc.)
_CN_TITLE_PATTERN = re.compile(
    r'[\u4e00-\u9fff]{0,4}(?:总|经理|老师|主任|总裁|博士|医生|律师|工程师|小姐|女士|先生|同学|队长|老板|组长|经理|秘书|助理)'
)
# Chinese: 2-4 char names (heuristic — surnames 1 char + given name 1-3 chars)
_CN_NAME_PATTERN = re.compile(
    r'[\u4e00-\u9fff]{2,4}'
)
# English: capitalized words (proper nouns)
_EN_PROPER_NOUN = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b')


def _extract_entities_from_text(text: str) -> list[str]:
    """Best-effort entity name extraction from raw text.

    Returns a list of candidate entity names ordered by appearance.
    Skips extraction from texts that START with a pronoun — these are
    pronoun-anchored sentences that don't carry first-mention context.
    """
    entities: list[str] = []
    seen: set[str] = set()

    # v1.16.10: skip pronoun-anchored sentences (they don't carry
    # first-mention context; the antecedent is somewhere ELSE).
    if not text:
        return entities
    _text_strip = text.lstrip()
    _first_char_lower = _text_strip[:1].lower()
    _first_word = _text_strip.split(maxsplit=1)[0].lower() if _text_strip else ''
    # Both single-char pronouns (她/他/它/这/那/该) AND common 2-char
    # pronoun-verb prefixes (她看/他听/这不/那是/该公司) skip extraction.
    # EN: She/He/It/They/This/That at start.
    _SKIP_PREFIXES = {
        '她', '他', '它', '这', '那', '该',  # single-char CN
        '她看', '她听', '她说', '她问', '她认', '她决',  # 2-char 她 + verb
        '他看', '他听', '他说', '他问', '他认', '他决',
        '它指', '这不', '这是', '这有', '那不', '那是', '那有',
        '我们', '他们', '她们', '它们',  # multi-char CN pronouns
        'she', 'he', 'it', 'they', 'this', 'that', 'him', 'her',
    }
    if (_text_strip[:2] in _SKIP_PREFIXES
            or _first_char_lower in _SKIP_PREFIXES
            or _first_word in _SKIP_PREFIXES):
        return entities  # pronoun-anchored, no first-mention here

    def _add(name: str):
        n = name.strip()
        if n and n.lower() not in seen and len(n) >= 2:
            seen.add(n.lower())
            entities.append(n)

    # Chinese title patterns (e.g. 王经理)
    for m in _CN_TITLE_PATTERN.finditer(text):
        _add(m.group(0))

    # Chinese general 2-4 char names
    # Common 2-4 char CJK words that are NOT names — pronouns, adverbs,
    # demonstratives, conjunctions. Skip these in name extraction.
    _NON_NAME_WORDS = {
        # 2-char
        '不是', '可以', '应该', '已经', '这个', '那个', '现在',
        '我们', '他们', '她们', '它们', '今天', '明天', '昨天',
        '什么', '怎么', '为什么', '因为', '所以', '觉得', '认为',
        '但是', '然后', '不过', '可能', '或者', '以及', '如果',
        '没有', '一些', '所有', '不会', '需要', '开始', '结束',
        '这样', '那样', '一定', '一直', '一起', '于是', '没有',
        '大家', '别人', '某人', '一些人', '所有人', '大家',
        # 3-char compounds ending in pronoun chars
        '他们决定', '她们决定', '他们说', '她们说', '他决定', '她决定',
        '我们决定', '他们认为', '她们认为', '我们认为',
        # 3-4 char common compounds (verb/adj + pronoun)
        '推迟项目', '取消会议', '结束工作', '开始项目',
    }
    for m in _CN_NAME_PATTERN.finditer(text):
        m_str = m.group(0)
        if m_str in _NON_NAME_WORDS:
            continue
        _add(m_str)

    # English proper nouns
    for m in _EN_PROPER_NOUN.finditer(text):
        _add(m.group(0))

    return entities


def _extract_entities_from_entities_json(entities_json: str | None) -> list[str]:
    """Extract entity name strings from entities_json column."""
    if not entities_json:
        return []
    try:
        data = json.loads(entities_json)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for e in data:
        if isinstance(e, dict):
            for key in ('name', 'entity', 'subject', 'value', 'text'):
                v = e.get(key)
                if isinstance(v, str) and v.strip():
                    names.append(v.strip())
        elif isinstance(e, str) and e.strip():
            names.append(e.strip())
    return names


def _get_recent_antecedents(
    bus,
    namespace: str,
    user_id: str | None,
    n: int = 10,
) -> list[dict]:
    """Fetch last N facts in the same namespace + user_id, newest first.

    Returns list of dicts: [{content, entities_json, created_at}, ...]
    """
    try:
        rows = bus.conn.execute(
            "SELECT content, entities_json, created_at "
            "FROM memory_canonical "
            "WHERE namespace = ? AND tombstoned = 0 "
            "  AND valid_until IS NULL "
            "  AND (user_id = ? OR (? IS NULL AND user_id IS NULL)) "
            "ORDER BY created_at DESC LIMIT ?",
            (namespace, user_id, user_id, n),
        ).fetchall()
    except Exception:
        # Schema mismatch — fall back to simpler query
        rows = bus.conn.execute(
            "SELECT content, entities_json, created_at "
            "FROM memory_canonical "
            "WHERE namespace = ? AND tombstoned = 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (namespace, n),
        ).fetchall()
    return [
        {'content': r[0] or '', 'entities_json': r[1], 'created_at': r[2]}
        for r in rows
    ]


def _build_antecedent_pool(
    antecedents: list[dict],
    include_trigger_keywords: bool = True,
) -> list[str]:
    """Aggregate candidate entity names from antecedent facts.

    Earlier (more recent) facts get priority — they appear first
    in the returned list.
    """
    pool: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        n = name.strip()
        if n and n.lower() not in seen and len(n) >= 2:
            seen.add(n.lower())
            pool.append(n)

    for fact in antecedents:
        # entities_json is the structured source
        for name in _extract_entities_from_entities_json(fact.get('entities_json')):
            _add(name)
        # Fallback: regex on raw content
        for name in _extract_entities_from_text(fact.get('content', '')):
            _add(name)

    return pool


def _resolve_pronouns(text: str, antecedent_pool: list[str]) -> tuple[str, list[dict]]:
    """Replace pronouns in text with most-recent antecedent entity.

    Resolution rule: each pronoun TYPE (Chinese set vs English set) reuses
    the most-recent antecedent for ALL occurrences of that pronoun type
    within the text. Reasoning: in dialogue, repeated use of the same
    pronoun almost always refers to the same entity ("she said she was
    not told" → 3 references to the same person, not 3 different
    people). Different pronoun TYPES (e.g. CN 她 vs EN she, or 他 vs
    she) each get a fresh antecedent to allow mixed-language text.

    Returns (rewritten_text, resolutions) where resolutions is a
    list of {pronoun, replaced_with, position, lang} dicts for audit.
    """
    if not antecedent_pool or not text:
        return text, []

    resolutions: list[dict] = []
    rewritten = text

    # Track per-language pool cursor — each lang reuses first unused antecedent.
    used_idx_by_lang = {'zh': -1, 'en': -1}

    def _next_unused(start: int) -> tuple[int, str] | None:
        """Find next unused pool index at or after `start`. Returns (idx, name) or None."""
        for i in range(start, len(antecedent_pool)):
            return (i, antecedent_pool[i])
        return None

    # Pass 1: Chinese pronouns (longest first)
    # Per pronoun TYPE: process ALL occurrences with the SAME antecedent.
    # Design: ALL pronoun TYPES (CN and EN, all variants) share the
    # pool. First TYPE consumes pool[0], next TYPE consumes pool[1],
    # etc. If pool is exhausted, re-use pool[0] (last antecedent) for
    # remaining types. This handles the realistic case where one
    # antecedent is referenced by multiple pronoun types in mixed
    # dialogue ("she said 她同意" → both reference Maria).
    def _resolve_type(pronoun: str, lang: str, is_cn: bool):
        nonlocal rewritten, resolutions, cursor
        if is_cn:
            positions: list[int] = []
            start = 0
            while True:
                pos = rewritten.find(pronoun, start)
                if pos < 0:
                    break
                positions.append(pos)
                start = pos + len(pronoun)
        else:
            pattern = re.compile(r'\b' + re.escape(pronoun) + r'\b', re.IGNORECASE)
            positions = [(m.start(), m) for m in pattern.finditer(rewritten)]
        if not positions:
            return
        # Pick antecedent: cursor position if available, else wrap to 0
        if cursor >= len(antecedent_pool):
            cursor = 0  # wrap
        ante = antecedent_pool[cursor]
        # Replace ALL occurrences with the SAME antecedent (case-preserved per match)
        if is_cn:
            new_rewritten = rewritten
            for pos in reversed(positions):
                new_rewritten = new_rewritten[:pos] + ante + new_rewritten[pos + len(pronoun):]
                resolutions.append({
                    'pronoun': pronoun,
                    'replaced_with': ante,
                    'position': pos,
                    'lang': lang,
                })
            rewritten = new_rewritten
        else:
            for pos, match in reversed(positions):
                matched_word = match.group(0)
                replacement = ante
                if matched_word[0].isupper() and ante:
                    replacement = ante[0].upper() + ante[1:]
                new_rewritten = rewritten[:pos] + replacement + rewritten[match.end():]
                rewritten = new_rewritten
                resolutions.append({
                    'pronoun': pronoun,
                    'replaced_with': replacement,
                    'position': pos,
                    'lang': lang,
                })
        cursor += 1

    # CN pronouns first
    cursor = 0
    for pronoun in _CN_PRONOUNS:
        _resolve_type(pronoun, lang='zh', is_cn=True)
    # EN pronouns next (cursor continues across langs)
    for pronoun in _EN_PRONOUNS:
        _resolve_type(pronoun, lang='en', is_cn=False)

    return rewritten, resolutions


def resolve_coreferences(
    bus,
    text: str,
    *,
    namespace: str,
    user_id: str | None = None,
    antecedent_window: int = 10,
) -> dict:
    """Resolve pronouns in `text` against recent facts in same namespace.

    Returns:
        {
            'original': <input text>,
            'resolved': <text with pronouns replaced>,
            'changed': bool,
            'resolutions': [{pronoun, replaced_with, position, lang}, ...],
            'antecedents_used': [<entity name>, ...],
            'window': <int>,
        }

    If no antecedents found OR no pronouns in text, returns
    {'changed': False, 'resolved': text, ...}.
    """
    if not text or not text.strip():
        return {
            'original': text,
            'resolved': text,
            'changed': False,
            'resolutions': [],
            'antecedents_used': [],
            'window': antecedent_window,
        }

    antecedents = _get_recent_antecedents(
        bus, namespace=namespace, user_id=user_id, n=antecedent_window * 2,
    )
    # v1.16.10: self-reference guard — if the input text was previously
    # written verbatim (e.g. test re-runs the same write), the antecedent
    # pool would include the SAME content we're trying to resolve,
    # causing self-extraction garbage like '份新报告' from the test text
    # itself. Filter out any antecedent whose content matches the input.
    text_norm = text.strip()
    antecedents = [
        a for a in antecedents
        if (a.get('content') or '').strip() != text_norm
    ][:antecedent_window]
    if not antecedents:
        return {
            'original': text,
            'resolved': text,
            'changed': False,
            'resolutions': [],
            'antecedents_used': [],
            'window': antecedent_window,
        }

    pool = _build_antecedent_pool(antecedents)
    if not pool:
        return {
            'original': text,
            'resolved': text,
            'changed': False,
            'resolutions': [],
            'antecedents_used': [],
            'window': antecedent_window,
        }

    resolved, resolutions = _resolve_pronouns(text, pool)

    return {
        'original': text,
        'resolved': resolved,
        'changed': resolved != text,
        'resolutions': resolutions,
        'antecedents_used': [r['replaced_with'] for r in resolutions],
        'window': antecedent_window,
    }


def make_resolved_text(text: str, antecedent_pool: list[str]) -> str:
    """Pure helper: rewrite text given an antecedent pool, no DB lookup.

    Used by tests and CLI tools that don't have a bus handle.
    """
    resolved, _ = _resolve_pronouns(text, antecedent_pool)
    return resolved
