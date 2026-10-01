"""v1.16.29: visibility_classifier — decide whether a fact is `commons` (shared)
or `personal` (user-only) based on content signals.

Three layers (designer intent preserved from user feedback):
  1. admin toggle — bot-binding.db user_meta.allow_commons_write (if 0,
     everything is forced personal, user never learns about the switch)
  2. visibility_hint — caller-passed explicit override ('commons'/'personal'/'auto')
  3. kind-driven auto — AUTO_COMMONS_KINDS + clean content + confidence

The classifier NEVER marks a fact `commons` if it contains:
  - PII (phone, email, SSN, ID card, bank card, geographic, chat_id)
  - first-person pronouns (我/我的/我自己, I/my/our/us, family/home)
  - emotion/health keywords (suicide/depression/诊断/药物)
  - explicit user hint = 'personal'

It marks `commons` ONLY when:
  - kind in AUTO_COMMONS_KINDS (method/recipe/lesson/success_pattern/
    failure_pattern/mental_model/knowledge_page/flow)
  - admin_global_toggle = 1
  - hint != 'personal'
  - no PII / first-person / emotion signals
"""
from __future__ import annotations
import re

# ---------------------------------------------------------------------------
# Kinds that astor auto-promotes to commons when content is clean
# ---------------------------------------------------------------------------
AUTO_COMMONS_KINDS = frozenset({
    'method', 'recipe', 'lesson', 'success_pattern', 'failure_pattern',
    'mental_model', 'knowledge_page', 'flow',
})

_KNOWLEDGE_RE = re.compile(
    # EN method/recipe/lesson cues
    r'\b(method|recipe|lesson|pattern|workflow|step[- ]by[- ]step|how[- ]to)\b|'
    # CN method/recipe/lesson cues
    r'(方法|流程|步骤|教程|配方|实战|技巧|模板|方案|操作指南|经验总结|踩坑|总结|最佳实践)|'
    # Common tool article extract patterns
    r'curl\s*[+\u2212-]?\s*(?:chrome\s*)?UA|'
    r'(?:article\s*)?fetch\s*(?:method|workflow)|'
    r'(?:how\s*to|怎么|如何)\s*(?:抓取|获取|访问|爬|爬取|读取)'
)


def _looks_like_knowledge(text: str) -> bool:
    """Heuristic: detect method/recipe/lesson content even when forge
    classifier returned kind=fact. Catches regex extractor fallback."""
    if not text:
        return False
    return bool(_KNOWLEDGE_RE.search(text))




# ---------------------------------------------------------------------------
# Signal detectors (each returns a list of (signal_name, severity) matches)
# ---------------------------------------------------------------------------

# Phone (CN mobile 11-digit, US with separators, etc.)
_RE_PHONE = re.compile(
    r'(?:'
    r'\b1[3-9]\d{9}\b'                                 # CN mobile
    r'|\b\d{3}[-.\s]\d{3,4}[-.\s]\d{4}\b'               # US with separators
    r'|\+\d{1,3}[-.\s]?\d{4,14}'                         # intl prefix
    r')'
)

# Email
_RE_EMAIL = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')

# China ID card 18 digit (last may be X)
_RE_CN_ID = re.compile(r'\b\d{17}[\dXx]\b')

# Bank card 16-19 digits (with optional Luhn check)
_RE_BANK = re.compile(r'\b\d{16,19}\b')

# US SSN
_RE_SSN = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')

# Chat ID patterns (weixin o-prefix)
_RE_WECHAT_CHAT = re.compile(r'\bo[A-Za-z0-9_-]{20,}@im\.wechat\b')
# Telegram chat_id (numeric, 8-12 digits)
_RE_TG_CHAT = re.compile(r'\b\d{8,12}\b')

# Multilingual PII keyword anchors — pairs with numeric regexes above
_PII_KEYWORDS_ZH = re.compile(
    r'身份证|银行卡|社保|住址|户口|手机号|微信号|邮箱|QQ|账号'
)
_PII_KEYWORDS_EN = re.compile(
    r'SSN|social security|address|phone number|credit card|'
    r'bank account|passport|driver.{0,3}license'
)


def has_pii(text: str) -> list[dict]:
    """Return list of PII matches. Empty list = clean.

    Each match is {name, severity, sample}. Severity 'redact' blocks write
    to commons; severity 'review' is a soft warning.
    """
    if not text:
        return []
    out = []
    for m in _RE_PHONE.finditer(text):
        out.append({'name': 'phone', 'severity': 'redact', 'sample': m.group()})
    for m in _RE_EMAIL.finditer(text):
        out.append({'name': 'email', 'severity': 'redact', 'sample': m.group()})
    for m in _RE_CN_ID.finditer(text):
        out.append({'name': 'cn_id_card', 'severity': 'redact', 'sample': m.group()})
    for m in _RE_SSN.finditer(text):
        out.append({'name': 'ssn', 'severity': 'redact', 'sample': m.group()})
    for m in _RE_BANK.finditer(text):
        digits = m.group()
        if _luhn_ok(digits):
            out.append({'name': 'bank_card', 'severity': 'redact', 'sample': digits})
    for m in _RE_WECHAT_CHAT.finditer(text):
        out.append({'name': 'wechat_chat_id', 'severity': 'redact', 'sample': m.group()})
    # chat_id with nearby keyword context (avoid false positives on bare numbers)
    if _PII_KEYWORDS_ZH.search(text) or _PII_KEYWORDS_EN.search(text):
        out.append({'name': 'pii_keyword_context', 'severity': 'review', 'sample': 'context-only'})
    # Also catch the existing PII gate's chat_id blocklist from old code
    for m in _RE_TG_CHAT.finditer(text):
        # Don't flag 8-12 digit numbers generically — too noisy. Use as 'review'
        # only when adjacent to a PII keyword.
        start = max(0, m.start() - 6)
        end = min(len(text), m.end() + 6)
        ctx = text[start:end]
        if _PII_KEYWORDS_ZH.search(ctx) or _PII_KEYWORDS_EN.search(ctx):
            out.append({'name': 'telegram_chat_id', 'severity': 'redact', 'sample': m.group()})
    return out


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — short-circuit obvious false positives (e.g. long year
    numbers like 20240124006170)."""
    if not digits.isdigit() or len(digits) < 13:
        return False
    s = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


# ---------------------------------------------------------------------------
# First-person + emotion + geographic signals (any match = personal)
# ---------------------------------------------------------------------------
# v1.16.29: standalone 我 + 我的 + 我们 + 我自己 + EN pronouns +
# situational home/family. Negative lookahead on 我 avoids matching 我们/我自己.
_FPII_RE = re.compile(
    r'(?:'
    r'我(?!们|自己)|我的|我们|我自己|'
    r'\bmy\b|\bour\b|\bmine\b|\bus\b|\bme\b|'
    r'家里|family|home|home address'
    r')'
)

_EMOTION_RE = re.compile(
    r'想自杀|自杀|抑郁|抑郁症|诊断|'
    r'\bsuicidal\b|\bsuicide\b|\bdepression\b|\bdepressed\b|\banxiety\b'
)

# Geographic: "我在 <city> 住" / "我家在 <city>" / "I live in <city>"
_GEO_RE = re.compile(
    r'(?:我|我的|\bmy\b|\bI\b).{0,5}(?:在|住|住址|住在|address|live in)'
    r'.{0,40}(?:北京|上海|深圳|广州|杭州|成都|武汉|东京|大阪|'
    r'Beijing|Shanghai|Tokyo|New York|Calgary|Toronto)'
)


def has_first_person(text: str) -> bool:
    return bool(_FPII_RE.search(text or ''))


def has_emotion(text: str) -> bool:
    return bool(_EMOTION_RE.search(text or ''))


def has_geographic(text: str) -> bool:
    return bool(_GEO_RE.search(text or ''))


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify_visibility(
    text: str,
    kind: str | None = None,
    hint: str | None = None,
    admin_allow_commons: bool = True,
    confidence: float = 1.0,
) -> dict:
    """Decide visibility (commons/personal) and report reason.

    Returns {visibility, reason, signals, override_allowed}.

    Logic:
      1. If admin_allow_commons = False → personal (force, R-class 7587)
      2. If hint == 'commons':
           if has_pii(text) BLOCK → personal + signal record
           else → commons
      3. If hint == 'personal' → personal
      4. If hint == 'auto' or None:
           if kind in AUTO_COMMONS_KINDS
              and confidence >= 0.7
              and not (has_pii(text) or has_first_person(text)
                       or has_emotion(text) or has_geographic(text)):
               → commons
           else:
               → personal
    """
    signals = {
        'pii': has_pii(text),
        'first_person': has_first_person(text),
        'emotion': has_emotion(text),
        'geographic': has_geographic(text),
    }
    has_any_block = any(bool(v) for v in signals.values())

    # Layer 1: admin global toggle (highest priority)
    if not admin_allow_commons:
        return {
            'visibility': 'personal',
            'reason': 'admin_disabled_commons_write',
            'signals': signals,
            'override_allowed': True,  # admin can promote later
        }

    # Layer 2: explicit user hint
    if hint == 'commons':
        if has_any_block:
            return {
                'visibility': 'personal',
                'reason': 'hint_commons_blocked_by_signals',
                'signals': signals,
                'override_allowed': False,
            }
        return {
            'visibility': 'commons',
            'reason': 'user_hint_commons',
            'signals': signals,
            'override_allowed': True,
        }
    if hint == 'personal':
        return {
            'visibility': 'personal',
            'reason': 'user_hint_personal',
            'signals': signals,
            'override_allowed': True,
        }

    # Layer 3: kind-driven OR content-driven auto.
    # If LLLM/forge kind classifier already tagged kind=method/recipe/etc,
    # that's a strong signal. But regex fallback often defaults to kind=fact
    # even for method-like content. We add content sniffing: if text
    # contains explicit method/recipe/lesson keywords (CN + EN), classify as
    # AUTO_COMMONS-eligible even when kind=fact.
    _content_kind = None
    if not kind or kind not in AUTO_COMMONS_KINDS:
        if _looks_like_knowledge(text):
            _content_kind = 'content_inferred'
    _effective_kind = kind if (kind and kind in AUTO_COMMONS_KINDS) else _content_kind

    if _effective_kind and confidence >= 0.7 and not has_any_block:
        return {
            'visibility': 'commons',
            'reason': f'kind_{_effective_kind}_auto_promote',
            'signals': signals,
            'override_allowed': True,
        }

    # Default: personal (most likely is private)
    return {
        'visibility': 'personal',
        'reason': 'default_personal',
        'signals': signals,
        'override_allowed': True,
    }


__all__ = [
    'AUTO_COMMONS_KINDS',
    '_looks_like_knowledge',
    'has_pii',
    'has_first_person',
    'has_emotion',
    'has_geographic',
    'classify_visibility',
]