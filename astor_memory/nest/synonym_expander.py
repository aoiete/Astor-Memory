"""synonym_expander.py — query expansion with Chinese + English support.

v1.10.9 (2026-08-27): initial — English-only synonym dict, no LLM.
v1.15.31 (2026-09-28, Ship K): Chinese synonym dict + bi-gram split +
  short-query LLM fallback. Backward compatible: returns same shape
  (list[str], original first). Existing tests still pass.

Strategy (multi-language):
  1. Detect query script (CJK / Latin / mixed).
  2. CJK queries: bi-gram substring split (cheap, ~1ms, no LLM).
     Each bi-gram becomes a candidate variant by appending.
  3. Latin queries: trigger-word synonym substitution (legacy v1.10.9).
  4. Short queries (< 8 tokens AND no synonym matched): optional LLM
     fallback via ASTOR_LLM_EXPAND=1 env var. Caller gates this to
     avoid surprise cost. Default off.

Why this matters:
  - eval baseline (2026-09-28) shows lifestyle mrr=0.556 (n=10),
    fortune mrr=0.814 (n=21). The 5 misses (Q59 fit 健身 计划,
    Q60 workout 周训练, Q79 RAG 知识库 bge-reranker) all have CJK
    tokens that the English-only expander can't see.
  - LoCoMo queries are short and abstract — same problem applies
    in English but the synonym dict already covers that ground.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

# ---------------------------------------------------------------------------
# English synonym groups (unchanged from v1.10.9)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Chinese synonym groups — Ship K (2026-09-28)
#
# Tuned for the operator's domain (RAG/astor/trading/poker/bazi/timeseries).
# Conservative: each entry maps to 2-3 high-recall expansions.
# Future tuning: add domain-specific Chinese pairs as misses surface.
# ---------------------------------------------------------------------------
_CN_SYNONYM_GROUPS: dict[str, list[str]] = {
    # Fitness / health
    '健身': ['锻炼', '运动', '训练', 'workout', 'fitness'],
    '锻炼': ['健身', '运动', '训练', 'workout'],
    '运动': ['健身', '锻炼', '训练'],
    '训练': ['健身', '锻炼', 'training', 'practice'],
    # Knowledge / RAG
    '知识库': ['RAG', '知识', '文档', 'corpus', 'knowledge base', '文档库'],
    '知识': ['knowledge', '信息', '资料', '文档'],
    '文档': ['document', '文件', '资料', 'doc'],
    '检索': ['搜索', 'search', 'recall', '查找'],
    '重排': ['rerank', 're-rank', '排序', '重排序'],
    # Astor / memory
    '记忆': ['memory', '回忆', '存储', '存'],
    '召回': ['recall', '检索', '查询', '搜索'],
    '向量': ['embedding', 'vector', '表征'],
    # Bazi / fortune
    '八字': ['四柱', '命理', 'bazi', '排盘'],
    '排盘': ['八字', '排八字', 'paipan', '起盘'],
    '日柱': ['日主', '日干', 'dayun', '日'],
    '身强': ['身旺', '强', '旺', 'strong'],
    '身弱': ['弱', '衰', 'weak'],
    '时区': ['timezone', 'time zone', '时差'],
    # Trading / finance
    '股票': ['stock', 'equity', 'shares', '持仓'],
    '交易': ['trade', 'trading', '下单', '成交'],
    '持仓': ['position', 'positions', 'stock', '股票'],
    '止盈': ['take profit', 'TP', 'sell high'],
    '止损': ['stop loss', 'SL', 'sell low'],
    # Poker
    '扑克': ['poker', '德州', '德扑', 'texas holdem'],
    '德州': ['德扑', 'texas holdem', 'NLHE', '扑克'],
    '买入': ['buy-in', '买入', 'entry', '进入'],
    # Workflow / cron
    '定时': ['cron', 'schedule', 'scheduled', '周期'],
    '备份': ['backup', 'snapshot', '存档'],
}

# ---------------------------------------------------------------------------
# Intent detection patterns
# ---------------------------------------------------------------------------
_TEMPORAL_RE = re.compile(
    r'\b(when|what\s+(?:date|time|year|month|day)|how\s+long|how\s+often|'
    r'first|last|recently|yesterday|today|tomorrow|ago)\b',
    re.IGNORECASE,
)
_CN_TEMPORAL_RE = re.compile(r'(今日|今日|今天|昨日|昨天|明日|明天|何时|几时|多久|最近|刚才|之前|之后)')


def _has_cjk(text: str) -> bool:
    """True if text contains any CJK Unified Ideograph."""
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def _has_latin(text: str) -> bool:
    return any(ch.isascii() and ch.isalpha() for ch in text)


def _match_groups(query: str) -> list[str]:
    """English synonym-group keys appearing in query."""
    tokens = re.findall(r"[a-zA-Z']+", query.lower())
    matched = set()
    for tok in tokens:
        if tok in _SYNONYM_GROUPS:
            matched.add(tok)
    return list(matched)


def _match_cn_groups(query: str) -> list[str]:
    """Chinese trigger keys appearing as substrings in query.

    Substring (not token-boundary) match because Chinese has no spaces.
    Prefer longer triggers first so "日柱" matches before "日".
    """
    triggers = sorted(_CN_SYNONYM_GROUPS.keys(), key=len, reverse=True)
    matched = []
    for t in triggers:
        if t in query and t not in matched:
            matched.append(t)
    return matched


def _cn_bigrams(text: str, max_n: int = 3) -> list[str]:
    """Extract CJK bigram/trigram substrings as cheap semantic anchors."""
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    out = []
    for run in cjk_runs:
        if len(run) >= 2:
            # bigrams
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                if bg not in out:
                    out.append(bg)
        if len(run) >= 3 and len(out) < max_n * 2:
            # one trigram as long-anchor
            tg = run[:3]
            if tg not in out:
                out.append(tg)
    return out[:max_n]


def _llm_expand(query: str, max_variants: int = 3) -> list[str]:
    """Optional LLM fallback for short queries. ASTOR_LLM_EXPAND=1 gates it.

    Implementation lives in caller (server.py) — this is a marker so we know
    the gate fired. Returns empty list when gate is off so callers see no-op.
    """
    if os.environ.get('ASTOR_LLM_EXPAND', '0') != '1':
        return []
    # Caller may monkey-patch this name at import time to plug in real LLM.
    # Default behavior: no-op (the gate is the contract).
    return []


def expand_query(query: str, max_variants: int = 3) -> list[str]:
    """Return list of query variants. Original is always first.

    Backward-compatible signature. Multi-language:
      - English: trigger-word synonym substitution (v1.10.9 path).
      - Chinese: trigger-key substring substitution + bi-gram anchors.
      - Mixed:   run both pipelines.
      - Short queries (< 8 tokens AND no synonym matched): optional LLM fallback.
    """
    if not query:
        return ['']
    out = [query]
    seen_lower = {query.lower().strip()}

    has_cn = _has_cjk(query)
    has_lat = _has_latin(query)

    # --- Chinese expansion ---------------------------------------------------
    if has_cn:
        cn_triggers = _match_cn_groups(query)
        for trigger in cn_triggers[:2]:  # at most 2 triggers → up to 4 variants
            for syn in _CN_SYNONYM_GROUPS[trigger][:2]:
                # skip synonym equal to trigger (e.g. 'RAG' in '知识库' group
                # when query already contains RAG)
                if syn.lower() == trigger.lower():
                    continue
                # skip if synonym already present in query (avoid "fit 健身"
                # → "fit 健身 + 锻炼" variant by replacing same token twice)
                if syn in query:
                    continue
                # substring replace (no word boundary in CJK)
                variant = query.replace(trigger, syn, 1)
                if variant != query and variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break
            if len(out) >= max_variants:
                break

    # If still room, append CJK bigram anchors as expanded queries.
    if len(out) < max_variants:
        for bg in _cn_bigrams(query, max_n=2):
            variant = f"{query} {bg}"
            if variant.lower().strip() not in seen_lower:
                out.append(variant)
                seen_lower.add(variant.lower().strip())
            if len(out) >= max_variants:
                break

    # --- English expansion (legacy v1.10.9) ---------------------------------
    if has_lat:
        triggers = _match_groups(query)
        if triggers:
            for trigger in triggers[:1]:
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

    # --- Temporal specialization (mixed-script) ------------------------------
    if len(out) < max_variants:
        ql = query.lower()
        if has_cn and _CN_TEMPORAL_RE.search(query):
            for prefix in ['当前时间', '今天的', '现在的']:
                variant = f"{prefix} {query}"
                if variant.lower().strip() not in seen_lower:
                    out.append(variant)
                    seen_lower.add(variant.lower().strip())
                if len(out) >= max_variants:
                    break
        elif _TEMPORAL_RE.search(query):
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

    # --- LLM fallback for short, synonym-barren queries ----------------------
    if len(out) < max_variants:
        # heuristic: token count (CJK char + Latin word) < 8 AND no synonym hit
        n_tokens = len(re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+', query))
        synonym_hit = (len(out) > 1)  # we already added at least one variant
        if n_tokens < 8 and not synonym_hit:
            for v in _llm_expand(query, max_variants=max_variants - len(out)):
                if v and v.lower().strip() not in seen_lower:
                    out.append(v)
                    seen_lower.add(v.lower().strip())
                if len(out) >= max_variants:
                    break

    return out