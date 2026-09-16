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

# Phase E (2026-09-16): auto-memory noise filter + tier routing for
# auto-observed turns. ``astor_auto_observe`` consumes these.
MIN_AUTO_OBSERVE_LENGTH = 30
NOISE_PREFIXES = (
    "嗯",
    "ok",
    "好",
    "好的",
    "thanks",
    "谢谢",
    "hi",
    "hello",
    "👍",
    "你好",
    "",
)
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

    v1.3.0 (2026-08-25): added event_date + event_date_precision fields.
    Enables temporal rerank during recall: "when did X happen" queries can
    be matched against fact event_date, and facts without a date can be
    ranked lower for temporal queries. The LLM extractor (llm_extract.py)
    populates these via prompt; regex mode leaves them None.

    v1.6.0 (2026-08-25): added abstract (L0) + overview (L1) fields for
    OpenViking-style progressive loading. The system prompt only loads
    L0 abstracts (~50 tokens each); agent drills into L1/L2 only when
    needed. LLM mode populates via prompt; regex mode derives heuristically
    (abstract = first sentence, overview = first 240 chars).
    """
    content: str
    kind: str
    confidence: float = 0.7
    importance: float = 0.5
    tags: list[str] | None = None
    keywords: list[str] | None = None   # A-MEM-style; JSON-encoded into DB
    context: str = ''                  # A-MEM-style; human-readable 1-2 sentences
    event_date: str | None = None      # ISO-8601 date 'YYYY-MM-DD' if applicable
    event_date_precision: str = 'none'  # 'day'|'month'|'year'|'none'
    abstract: str = ''                 # L0: ≤80 tokens, one-sentence summary
    overview: str = ''                 # L1: ≤300 tokens, structured digest
    # v1.12.0 (2026-08-29): hierarchical extraction per Mem0 2026 lesson.
    # topic groups facts into session-level themes for retrieval scoping;
    # session_id identifies the conversation this fact originated from.
    topic: str = ''
    session_id: str = ''
    # v1.14.21 (2026-09-15 Ship B): RippleMem-style structured entity binding.
    # List of {type, value} extracted from content. Types: 'person', 'time',
    # 'topic'. Distinct from keywords (semantic retrieval boost) and
    # context (human-readable). Future Ship C entity_lex 3rd path uses this.
    entities: list[dict] | None = None  # [{"type": "person", "value": "sunday"}]


def extract_entities(text: str, fact_id: int = 0) -> list[dict]:
    """v1.14.21 (2026-09-15 Ship B): RippleMem-style structured entity binding.

    Extracts person / location / time / topic entities from text. Cheap regex
    heuristics, no LLM. Backward-compatible: returns [] when no entity is
    detectable.

    Returns list of {"type": str, "value": str, "fact_id": int}. fact_id is
    filled by caller. Capped at 16 per fact.
    """
    import re as _re_e
    if not text or not text.strip():
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(typ: str, val: str) -> None:
        v = val.strip()
        if not v:
            return
        if (typ, v.lower()) in seen:
            return
        seen.add((typ, v.lower()))
        out.append({"type": typ, "value": v[:128], "fact_id": fact_id})

    # --- person: Chinese names (2-4 Han chars) — Ship D (2026-09-15) ---
    # Dependency-aware heuristic: a Han token is treated as person ONLY if
    # the surrounding context signals a name reference. Patterns accepted:
    #   - "X 是/有/在/了/说/做/叫/来/去" (declarative)
    #   - "在/我/他/她/这/那/叫/对 X" (introducing phrase)
    #   - "叫 X" (introducing by name)
    #   - "X 的" (possessive, strong person signal)
    #   - "X 给/跟/向/对/和/与" (relation)
    # Tokens in noise patterns (verb / time / location-only / single-char
    # common words) are skipped. Tightens precision at the cost of some
    # recall (better for post-filter use case).
    _PERSON_STOP = {"用户", "今日", "明天", "昨天", "早上", "晚上", "中午",
                    "下午", "现在", "之前", "之后", "每天", "每周", "每月",
                    "要求", "希望", "觉得", "应该", "已经", "可以", "不能",
                    "不要", "不会", "请看", "请按", "请您", "想要",
                    "开始", "结束", "完成", "进行", "继续", "停止", "暂停",
                    # Ship D additions: verbs / time / common phrases
                    "交易", "买入", "卖出", "购买", "使用", "看", "去", "来",
                    "做了", "有了", "走了", "看了", "买了", "卖了", "用了",
                    "当天", "当天买入", "当天卖出", "今天", "明天", "昨天",
                    "股票", "金额", "价格", "订单", "数量", "时间", "日期",
                    "公司", "行业", "部门", "项目", "产品", "系统", "服务",
                    "页面", "按钮", "表格", "图片", "视频", "文档", "数据",
                    "用户", "客户", "对方", "本人", "自己", "大家", "他们",
                    "我们", "你们", "他们", "她们", "它们", "这事", "那个",
                    "这个", "哪个", "那些", "这些", "然后", "接着", "于是",
                    "现在", "以后", "以后", "此时", "那时", "过去", "未来",
                    "真的", "假的", "对的", "错的", "好的", "坏的", "贵的",
                    "便宜", "高", "低", "大", "小", "多", "少", "长", "短"}
    # postfix chars that STRONGLY signal the previous Han token is a name
    _PERSON_POSTFIX = {"是", "有", "在", "了", "说", "做", "叫", "来", "去",
                       "的", "给", "跟", "向", "对", "和", "与", "们"}  # 们 = plural marker (我们/他们)
    # prefix chars that introduce a name (weaker signal; only count if the
    # token is followed by a postfix signal too)
    _PERSON_PREFIX = {"在", "我", "他", "她", "这", "那", "叫", "对", "跟", "给"}
    for m in _re_e.finditer(r"[\u4e00-\u9fff]{2,4}", text):
        s = m.group(0)
        if s in _PERSON_STOP:
            continue
        if s[0] in {"今", "明", "昨", "前", "后"} and len(s) <= 3:
            continue
        # Context scan: 1-2 chars on each side
        s_start = m.start()
        s_end = m.end()
        prev_char = text[s_start - 1] if s_start > 0 else ''
        next_char = text[s_end] if s_end < len(text) else ''
        prev2 = text[s_start - 2:s_start] if s_start >= 2 else ''
        # Rule 1: previous char is a strong introducer
        if prev_char in _PERSON_PREFIX:
            _add("person", s)
            if len(out) >= 16:
                return out
            continue
        # Rule 2: next char is a strong postfix (的/是/在/了/...)
        if next_char in _PERSON_POSTFIX:
            _add("person", s)
            if len(out) >= 16:
                return out
            continue
        # Rule 3: previous char is 的 (X的 is possessive — strong person signal)
        if prev_char == "的":
            _add("person", s)
            if len(out) >= 16:
                return out
            continue
        # Otherwise: probably a verb / noun, skip (precision over recall)
        continue

    # --- person: English Capitalized ---
    _STOP_CAP = {"The", "This", "That", "These", "Those", "I", "You", "We",
                 "They", "He", "She", "It", "And", "But", "Or", "Not", "No",
                 "Yes", "If", "When", "Where", "How", "Why", "What", "Who",
                 "Then", "Now", "Here", "There", "Today", "Yesterday",
                 "Tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday", "January", "February",
                 "March", "April", "May", "June", "July", "August",
                 "September", "October", "November", "December", "API",
                 "ASTOR", "CEO", "CTO", "AI", "OK", "TODO", "URL", "USD",
                 "USDC", "BTC", "ETH", "Post", "Pre", "Ship", "Fact",
                 "User", "Admin", "Bus", "Note", "Class"}
    for m in _re_e.finditer(r"\b[A-Z][a-zA-Z]{1,30}\b", text):
        w = m.group(0)
        if w in _STOP_CAP:
            continue
        if len(w) < 2:
            continue
        _add("person", w)
        if len(out) >= 16:
            return out

    # --- time: ISO dates + Chinese dates + relative anchors ---
    for m in _re_e.finditer(r"\b\d{4}-\d{1,2}(?:-\d{1,2})?\b", text):
        _add("time", m.group(0))
        if len(out) >= 16:
            return out
    for m in _re_e.finditer(r"\b\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?", text):
        _add("time", m.group(0))
        if len(out) >= 16:
            return out
    for m in _re_e.finditer(
        r"(?:今天|昨天|明天|前天|后天|上周|本周|下周|上个月|这个月|下个月|今晚|明早|今早)",
        text):
        _add("time", m.group(0))
        if len(out) >= 16:
            return out

    # --- topic: tickers + quoted phrases ---
    for m in _re_e.finditer(r"\b[A-Z]{2,5}\b", text):
        w = m.group(0)
        if w in _STOP_CAP:
            continue
        _add("topic", w)
        if len(out) >= 16:
            return out
    for m in _re_e.finditer(r"[""“]([^""”]{2,40})[""”]", text):
        _add("topic", m.group(1))
        if len(out) >= 16:
            return out

    return out


def astor_regex_extract(text: str) -> list[AstorFact]:
    """Extract facts using regex patterns. No LLM.

    v1.2.0: also derives keywords + context heuristically:
    - keywords = [kind, ...content_words] (top-5 distinctive tokens)
    - context = first 120 chars of input text

    v1.6.0: derives abstract (L0) + overview (L1) heuristically:
    - abstract = first sentence (split by . / 。 / newline, ≤80 tokens)
    - overview = first 240 chars of full text (≤300 tokens)

    v1.13.2 (2026-09-04): default confidence raised from 0.7 to 0.85.
    Regex extraction is deterministic and verifiable (the content
    pattern matches a literal substring of the source), unlike LLM
    extraction which can fabricate. Raising regex confidence above
    the auto_link min_confidence=0.6 gate ensures these facts aren't
    silently demoted, and above typical LLM 0.7 means they outrank
    LLM hallucinations in provenance graph traversal. Triggered by
    sunday-rejection-bug (fact 8608): LLM-extracted fact with the same
    0.7 default propagated as authoritative and was quoted by agent
    in a user-facing refusal.
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
            # v1.6.0: derive L0 abstract from first sentence of input text
            # (not content_part, since content_part may be a clause not a sentence)
            abstract = _re.split(r'[.!?。！？\n]', text.strip(), maxsplit=1)[0].strip()
            # Cap abstract at ~80 tokens (very rough heuristic: 4 chars/token)
            if len(abstract) > 320:
                abstract = abstract[:317] + "..."
            facts.append(AstorFact(
                content=content_part,
                kind=kind,
                confidence=0.85,
                importance=0.5,
                tags=[kind, 'auto_extracted'],
                keywords=[kind] + distinct_words,
                context=text[:120].strip(),
                entities=extract_entities(text),
                abstract=abstract,
                overview=text[:240].strip(),
                # v1.12.0: derive topic from first noun phrase (≤5 words) and
                # session_id from doc-timestamp marker if present. Regex path
                # has no LLM, so heuristics must suffice.
                topic=_derive_topic_heuristic(text, content_part),
                session_id=_derive_session_id_heuristic(text),
            ))
            break  # one fact per match
    return facts


def _derive_topic_heuristic(full_text: str, content_part: str, max_words: int = 5) -> str:
    """v1.12.0: Heuristic topic extraction for regex extractor (no LLM).

    Strategy:
      1. Strip meta markers ([Doc timestamp: ...]) and trailing metadata.
      2. Try to find capitalized proper-noun sequences (e.g. "AXTI", "OpenAI").
      3. Fall back to first 2-4 distinctive content words.
      4. Filter out stop words and the regex 'Test step N' boilerplate.
    """
    import re as _re
    # Strip meta markers so [Doc timestamp: ...] doesn't pollute the topic
    clean = _re.sub(r'\[Doc timestamp:[^\]]+\]\s*', '', full_text)
    clean = _re.sub(r'topic:\s*[\w-]+', '', clean)
    clean = _re.sub(r'session:\s*[\w-]+', '', clean)

    # Try capitalized-proper-noun sequences first (most distinctive)
    proper = _re.findall(r'\b([A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,})*)\b', clean)
    if proper:
        # Prefer the first 2-word sequence (avoids "Doc Timestamp" noise)
        candidate = proper[0]
        # If single-word candidate is generic ("Doc", "Test"), skip
        if candidate.lower() not in ('doc', 'test', 'note', 'example', 'step',
                                       'note timestamp', 'doc timestamp'):
            return candidate

    # Fall back to distinctive content words (lowercase + filter stop words)
    stop = {'this', 'that', 'with', 'from', 'have', 'been', 'were', 'they',
            'them', 'what', 'when', 'where', 'which', 'their', 'there',
            'each', 'every', 'into', 'about', 'before', 'after', 'because',
            'also', 'only', 'very', 'just', 'than', 'more', 'less',
            'the', 'and', 'for', 'but', 'not', 'you', 'are', 'was',
            'step', 'test', 'heuristic', 'heuristics', 'final', 'with'}
    words = _re.findall(r'\b[A-Za-z]{3,}\b', content_part)
    distinct = []
    seen = set()
    for w in words:
        wl = w.lower()
        if wl in stop or wl in seen:
            continue
        seen.add(wl)
        distinct.append(w.capitalize())
        if len(distinct) >= max_words:
            break
    if not distinct:
        return ''
    return ' '.join(distinct[:4])  # cap at 4 words for readability


def _derive_session_id_heuristic(text: str) -> str:
    """v1.12.0: Extract session_id from [Doc timestamp: ...] marker or ISO date.

    Returns '' if no marker found. Format: 'doc-YYYY-MM-DD'.
    """
    import re as _re
    m = _re.search(r'\[Doc timestamp:\s*(\d{4}-\d{2}-\d{2})', text)
    if m:
        return f'doc-{m.group(1)}'
    m2 = _re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if m2:
        return f'doc-{m2.group(1)}'
    return ''


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


def astor_classify_outcome(text: str) -> str:
    """v1.13.1 (2026-09-14, Ship F): classify text into 3-zone outcome.

    Returns one of:
      - 'success'  (matches success_pattern phrases — go to user_preference)
      - 'failure'  (matches failure_pattern phrases — go to failure_pattern)
      - 'lesson'   (matches lesson_pattern phrases   — go to lesson zone)
      - 'neutral'  (no zone match — kind='fact', normal flow)

    Lesson takes precedence over failure and success (lessons are
    rare + specific, false-positive cost is high). Failure takes
    precedence over success (a text that says "搞砸了" is not a success).
    Success is the default for capture_intent-style phrases that don't
    trip failure/lesson detectors.

    The function delegates to the per-zone detectors in pattern_detector.py
    (Ship E) so the regex sets stay in one place.
    """
    if not text or not text.strip():
        return 'neutral'
    try:
        # Lazy import — pattern_detector also imports from extractor in some
        # cases, avoid circular.
        from .pattern_detector import (
            astor_detect_lesson_pattern,
            astor_detect_failure_pattern,
            astor_detect_success_pattern,
        )
    except ImportError:
        return 'neutral'
    if astor_detect_lesson_pattern(text):
        return 'lesson'
    if astor_detect_failure_pattern(text):
        return 'failure'
    if astor_detect_success_pattern(text) or astor_detect_capture_intent(text):
        return 'success'
    return 'neutral'


def astor_should_skip_auto_observe(text: str) -> tuple[bool, str]:
    """Phase E (2026-09-16): decide whether a turn should be skipped from
    automatic ``astor_auto_observe`` ingestion.

    Returns ``(skip, reason)``. ``skip=True`` means do not call ``astor_write``
    at all; Astor would otherwise emit noise rows. Reasons:
      - "empty": text is empty or whitespace-only
      - "too_short": text length < MIN_AUTO_OBSERVE_LENGTH
      - "noise_prefix": first whitespace-separated token (or its first 2 chars
        for CJK) matches NOISE_PREFIXES. Handles ASCII ("hi", "thanks") and
        CJK ("你好") greetings.
    """
    if not text or not text.strip():
        return True, "empty"
    if len(text) < MIN_AUTO_OBSERVE_LENGTH:
        return True, "too_short"
    stripped = text.lstrip()
    first_token = stripped.split(maxsplit=1)[0].lower()
    first_two = stripped[:2].lower()
    if first_token in NOISE_PREFIXES or first_two in NOISE_PREFIXES:
        return True, "noise_prefix"
    return False, ""


def _score_importance(text: str, outcome: str) -> float:
    """Phase E: importance heuristic for auto-observed turns.

    Heuristic:
      - failure / lesson  → 0.85 (always publishable, admin tier)
      - success + capture_intent keyword ("remember", "记住") → 0.8
      - success + long text (> 200) → 0.6 (might be publishable)
      - neutral + long text (> 200) → 0.6 (still might be a useful observation)
      - neutral + medium text (60..200) → 0.5 (borderline)
      - else neutral / short → 0.3 (admin-only, low signal)
    """
    if outcome in ("failure", "lesson"):
        return 0.85
    if outcome == "success":
        if astor_detect_capture_intent(text):
            return 0.8
        if len(text) > 200:
            return 0.6
        return 0.4
    # neutral: long content still might be useful as observation
    if len(text) > 200:
        return 0.6
    if len(text) >= 60:
        return 0.5
    return 0.3


def _pick_tier(outcome: str, importance: float) -> str | None:
    """Phase E: route auto-observed turn to a tier.

    - failure / lesson                  → source (admin-only)
    - success / neutral, importance >= 0.7 → public (auto-extracted observations)
    - success / neutral, importance >= 0.5 → public (general long content)
    - else                              → None (skip / admin-only fallback)
    """
    if outcome in ("failure", "lesson"):
        return "source"
    if outcome in ("success", "neutral"):
        if importance >= 0.5:
            return "public"
    return None


def astor_auto_observe(
    text: str,
    agent_id: str,
    user_id: str,
    namespace: str | None = None,
    *,
    min_length: int = MIN_AUTO_OBSERVE_LENGTH,
) -> dict:
    """Phase E: one-call auto-memory hook for any agent.

    Returns ``dict(observed, tier, importance, outcome, skipped_reason,
    facts)``. When ``observed=True``, the caller should treat ``facts`` as
    a sample of extracted statements (NOT a full ETL pipeline — this is
    a quick filter for ``sync_turn``-style agents).

    This function is **deliberately local-only**: it does NOT call
    ``astor_write`` itself. The agent integration layer (Hermes adapter,
    MCP server) decides when and where to actually write the fact.
    """
    if not agent_id:
        return {
            "observed": False,
            "tier": None,
            "importance": 0.0,
            "outcome": "neutral",
            "skipped_reason": "no_agent_id",
            "facts": [],
        }
    skip, reason = astor_should_skip_auto_observe(text or "")
    if skip:
        return {
            "observed": False,
            "tier": None,
            "importance": 0.0,
            "outcome": "neutral",
            "skipped_reason": reason,
            "facts": [],
        }
    eff_min = max(MIN_AUTO_OBSERVE_LENGTH, int(min_length))
    if len(text) < eff_min:
        return {
            "observed": False,
            "tier": None,
            "importance": 0.0,
            "outcome": "neutral",
            "skipped_reason": "too_short",
            "facts": [],
        }
    outcome = astor_classify_outcome(text)
    importance = _score_importance(text, outcome)
    tier = _pick_tier(outcome, importance)
    if tier is None:
        return {
            "observed": False,
            "tier": None,
            "importance": importance,
            "outcome": outcome,
            "skipped_reason": "low_signal",
            "facts": [],
        }
    # Lightweight fact extraction (use existing extractor).
    try:
        facts = astor_regex_extract(text)
        fact_contents = [f.content for f in facts[:3]]
    except Exception:
        fact_contents = []
    return {
        "observed": True,
        "tier": tier,
        "importance": importance,
        "outcome": outcome,
        "skipped_reason": "",
        "facts": fact_contents,
        "namespace": namespace or f"auto_observe/{agent_id}",
        "agent_id": agent_id,
        "user_id": user_id or "admin",
    }


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
    why: str | None = None,
    outcome: Literal['success', 'error', 'neutral'] = 'neutral',
    doc_timestamp: str | None = None,
) -> list[AstorFact]:
    """Extract facts based on mode.

    P1-fix 2026-08-15: also persist an llm_call_log row to the per-tier forge
    DB so the audit trail is complete. Latency/model/provider tracked.

    v1.2.7 (2026-08-19): added `why` + `outcome` parameters per bus-mem-1042
    pattern — distinguish "do X" (success recipe) from "avoid X" (error
    pattern) at write time so recall ranking can boost successes and
    suppress errors. Default outcome='neutral' for backward compat.
    """
    import hashlib, time as _time, json as _json
    from . import astor_forge_log_call

    t0 = _time.time()
    if mode == 'auto':
        mode = astor_choose_extract_mode(text)

    # v1.9.1 (2026-08-25): validate mode. Without this, an unknown mode
    # silently fell through with facts=[], provider='regex_fallback',
    # success=1 — caller saw count=0 with no error and assumed "nothing
    # to extract" instead of "you sent wrong_mode_xyz".
    if mode not in ('none', 'regex', 'llm'):
        raise ValueError(f"invalid mode {mode!r}: must be 'none'|'regex'|'llm'|'auto'")

    facts: list[AstorFact] = []
    success = 1
    error_msg = None
    # v1.10.8 (2026-08-26): mode='none' no longer short-circuits before audit
    # log. Previously `if mode == 'none': return []` left zero audit trail for
    # raw-event store mode (long-text blocks where regex/llm don't apply).
    # Now we set provider='none' and let the path fall through to audit.
    provider = 'regex_fallback'
    model_name = None

    try:
        if mode == 'none':
            provider = 'none'
        elif mode == 'regex':
            # v1.10.8: capture_intent handling moved to unified post-process
            # block (after this if/elif) so llm + regex_fallback also get it.
            facts = astor_regex_extract(text)

        elif mode == 'llm':
            # v1.10.9 fix: import astor_llm_extract only (the _with_provider
            # variant is a NESTED function in llm_extract.py and cannot be
            # imported at module level — calling it here would NameError).
            from .llm_extract import astor_llm_extract
            # v1.10.9: when caller passes doc_timestamp, prefix the text with
            # an explicit anchor marker so the LLM resolves 'yesterday/last
            # week' against the correct calendar date.
            _text_for_llm = text
            if doc_timestamp:
                _text_for_llm = (
                    f"[Doc timestamp: {doc_timestamp[:10]}] Treat this as 'today' "
                    f"when resolving 'yesterday/last week/3 days ago' etc.\n\n"
                    f"{text}"
                )
            # v1.10.9: pass OPENAI_BASE_URL / ASTOR_LLM_MODEL env so the
            # 'openai' provider routes through OpenRouter (default m3 has no
            # key in most deployments, was silently failing).
            import os as _os_e
            import sys as _sys_d
            _facts = astor_llm_extract(
                _text_for_llm,
                fallback_chain=['openai'],
                base_url=_os_e.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
                model=_os_e.environ.get('ASTOR_LLM_MODEL', 'google/gemini-3.7-flash'),
            )
            _sys_d.stderr.write(f"[EXTRACT_DEBUG] mode=llm facts={len(_facts)} text_len={len(text)} text_preview={text[:80]!r}\n")
            _sys_d.stderr.flush()
            # v1.11 (2026-08-27): astor_llm_extract may now return a mix of
            # AstorFact (from the rich dict-array code path) and dict (from
            # the flat string-array fallback wrapper). Normalize to AstorFact
            # so downstream code (which uses `f.content`, `f.kind`, ...) works.
            facts = []
            for f in _facts:
                if isinstance(f, dict) and not isinstance(f, AstorFact):
                    facts.append(AstorFact(
                        content=f.get('content', ''),
                        kind=f.get('kind', 'fact'),
                        confidence=f.get('confidence', 0.7),
                        importance=f.get('importance', 0.5),
                        tags=f.get('tags') or [],
                        keywords=f.get('keywords') or [],
                        context=str(f.get('context', '') or '')[:500],
                        event_date=f.get('event_date') or None,
                        event_date_precision=str(f.get('event_date_precision') or 'none')[:16],
                        abstract=str(f.get('abstract', '') or '')[:500],
                        overview=str(f.get('overview', '') or '')[:1500],
                    ))
                else:
                    facts.append(f)
            # Audit honesty: if we know openai was used, record it.
            provider = 'openai' if facts else 'regex_fallback'
            model_name = None  # populated by llm_extract internals if available
    except Exception as exc:
        success = 0
        error_msg = str(exc)[:200]
        facts = []

    # v1.2.7: tag facts with outcome + why for downstream recall ranking
    # v1.10.9: anchor-based relative-date resolution. If the caller passed
    # a document timestamp (e.g. event.ts for /v1/write), normalize all
    # 'yesterday/last week' references into absolute YYYY-MM-DD so temporal
    # queries can match them later.
    if facts and doc_timestamp:
        try:
            from .relative_date import resolve_relative_dates_batch
            dict_facts = [f.__dict__ for f in facts]
            resolved = resolve_relative_dates_batch(dict_facts, doc_timestamp[:10])
            for f, src in zip(facts, resolved):
                if src.get("event_date") and not f.event_date:
                    f.event_date = src.get("event_date")
                if src.get("event_date_precision") and f.event_date_precision == "none":
                    f.event_date_precision = src.get("event_date_precision")
        except Exception:
            pass
    if facts and (why or outcome != 'neutral'):
        for f in facts:
            if f.tags is None:
                f.tags = []
            if outcome != 'neutral' and outcome not in f.tags:
                f.tags.append(f'outcome:{outcome}')
            if why:
                f.context = (f.context + f'\n[why] {why}').strip() if f.context else f'[why] {why}'
            # v1.13.1 (2026-09-14, Ship F): outcome → zone mapping.
            # Force the fact into the right zone per astor 3-zone architecture
            # (fact 11938). success → user_preference, failure → failure_pattern,
            # lesson → lesson. importance follows the iron-rule convention
            # (fact 3752). This is the bridge between capture_intent hook
            # and the zone taxonomy — without it, every fact lands as
            # kind='fact' and the 3-zone system never gets populated.
            # 2026-09-16 Ship success-pattern routing: success outcome now maps
            # to success_pattern (not user_preference) so the 3-zone architecture
            # (success_pattern / failure_pattern / lesson) stays clean. Public
            # bus already had user_preference for ad-hoc prefs; success_pattern
            # is reserved for reusable methods/patterns/experiences.
            if outcome == 'success':
                f.kind = 'success_pattern'
                f.importance = max(f.importance, 0.85)
            elif outcome == 'failure':
                f.kind = 'failure_pattern'
                f.importance = max(f.importance, 0.90)
            elif outcome == 'lesson':
                f.kind = 'lesson'
                f.importance = max(f.importance, 0.99)
                # v1.13.1 Ship F: enforce LESSON prefix in content
                # (iron rule fact 3752). Only add if missing.
                if f.content and not f.content.upper().startswith('LESSON '):
                    f.content = f'LESSON {f.content}'

    # v1.10.8: capture-intent boost moved OUT of the regex branch — now applies
    # uniformly to regex, llm, and the all-providers-failed regex_fallback path.
    # Previously only `mode == 'regex'` got the 0.95 confidence + 'capture_intent'
    # tag, so a user saying "remember this" while the LLM was the active extractor
    # silently lost the boost.
    if facts and astor_detect_capture_intent(text):
        for f in facts:
            f.confidence = 0.95  # user wants remembered
            if f.tags is None:
                f.tags = []
            if 'capture_intent' not in f.tags:
                f.tags.append('capture_intent')

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
            reason=why or f'mode={mode};outcome={outcome}',
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
