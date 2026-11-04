"""v1.16.29: visibility_classifier — decide whether a fact is `commons` (shared)
or `personal` (user-only) based on content signals.

Three layers (designer intent preserved from user feedback):
  1. admin toggle — bot-binding.db user_meta.allow_commons_write (if 0,
     everything is forced personal, user never learns about the switch)
  2. visibility_hint — caller-passed explicit override ('commons'/'personal'/'auto')
  3. kind-driven auto — AUTO_COMMONS_KINDS + clean content + confidence

The classifier NEVER marks a fact `commons` if it contains:
  - PII (phone, email, SSN, ID card, bank card, geographic, chat_id)
  - private first-person content (see `has_first_person` below: singular
    forms always, plural forms only when a personal marker is nearby)
  - emotion/health keywords (suicide/depression/诊断/药物)
  - explicit user hint = 'personal'

It marks `commons` ONLY when:
  - kind in AUTO_COMMONS_KINDS (method/recipe/lesson/success_pattern/
    failure_pattern/mental_model/knowledge_page/flow)
  - admin_global_toggle = 1
  - hint != 'personal'
  - no PII / first-person / emotion signals

v1.17.31: the first-person signal was split. A plural pronoun alone is
method narration, not private content, so it blocks only when a personal
marker is in the same text.
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
# "QQ" must not match inside a ticker: TQQQ, QQQI, SQQQ are ETFs. The
# guard is "not a letter on either side" rather than a word boundary,
# because "QQ号" has no boundary between QQ and 号 but is still a QQ id.
_PII_KEYWORDS_ZH = re.compile(
    r'身份证|银行卡|社保|住址|户口|手机号|微信号|邮箱|账号|'
    r'(?<![A-Za-z])QQ(?![A-Za-z])'
)
_PII_KEYWORDS_EN = re.compile(
    r'SSN|social security|address|phone number|credit card|'
    r'bank account|passport|driver.{0,3}license'
)

# v1.17.15: a chat id needs its platform, but the platform name alone
# is not a signal -- "wechat.html" is a filename, and the distiller
# would drop the whole sentence about it. So the platform name and the
# digits have to be adjacent. The generic keyword tables above stay
# free of platform names for the same reason.
_RE_NAMED_CHAT_ID = re.compile(
    r'(?:telegram|discord|wechat|slack|whatsapp|微信|电报|飞书)'
    r'\s*(?:chat[_\s-]?id|user[_\s-]?id)?\s*[:=]?\s*\d{6,}',
    re.I,
)

# An identifier that names itself. "message_id=7519050235899700000" is
# the operator's message, "bot_id=8263b17ef000" is their account, and
# neither is a method. The key has to be there -- a bare 19-digit
# number is indistinguishable from a hash, and a bare hex string from
# a checksum -- so this matches the key/value form only.
#
# The value has to be long enough to be an id: 8+ digits, or 8+ hex
# characters for the platform-generated ids that are not numeric.
_RE_KEYED_ID = re.compile(
    r'\b(?:message|msg|chat|session|conversation|bot|account|user|'
    r'thread|group|channel)[_\s-]?id\s*[:=]\s*'
    r'(?:[0-9a-f]{8,}|[0-9]{8,})',
    re.I,
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
        # v1.17.15: the window was 6 characters, which cut "telegram
        # <chat_id>" down to "egram <chat_id>" -- the keyword was
        # truncated past its own word boundary, so even a keyword table
        # containing "telegram" would not have matched. 24 characters
        # covers a platform name plus the word before it.
        start = max(0, m.start() - 24)
        end = min(len(text), m.end() + 24)
        ctx = text[start:end]
        if _PII_KEYWORDS_ZH.search(ctx) or _PII_KEYWORDS_EN.search(ctx):
            out.append({'name': 'telegram_chat_id', 'severity': 'redact', 'sample': m.group()})
    # The platform name has to be next to the digits. This is what
    # catches "deliver=origin->telegram <chat_id>" without catching
    # "wechat.html", which is a filename and not an identifier.
    for m in _RE_NAMED_CHAT_ID.finditer(text):
        out.append({'name': 'telegram_chat_id', 'severity': 'redact', 'sample': m.group()})
    for m in _RE_KEYED_ID.finditer(text):
        out.append({'name': 'keyed_identifier', 'severity': 'redact', 'sample': m.group()})
    # v1.17.32: a credential is a value, not a topic. "our key rotation is
    # automated" is a method note and does not match; "our API key is
    # ghp_..." does.
    for m in _RE_CREDENTIAL_VALUE.finditer(text):
        out.append({'name': 'credential_value', 'severity': 'redact',
                    'sample': m.group()[:24]})
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
# v1.17.32: the plural branch never fired for English -- "we" was missing
# from it, so the marker list was never consulted for an English sentence
# and every one of them leaked. "I" was missing from the singular branch
# for the same reason. Both are added.
_FPII_SINGULAR_RE = re.compile(
    r'我(?!们|自己)|我的|我自己|家里|'
    r'\bmy\b|\bmine\b|\bme\b'
)
_FPII_PLURAL_RE = re.compile(
    r'我们|\bour\b|\bus\b|\bwe\b'
)
_FPII_HOME_RE = re.compile(
    r'\bmy home\b|\bhome address\b'
)
# A standalone capital I is a pronoun -- except where it is a list marker
# ("R-class I", "Part I", "step I", "Ship I"). The preceding word decides.
#
# The list is not extended by guessing. Measured over the runtime: of the
# 197 standalone "I" occurrences, 12 follow a word on this list and 15
# follow a capitalised word -- of those 15, twelve are "Ship I" and the
# rest are real pronouns ("While I understand", "Would I trust", "As I
# mentioned"). A capitalised-prefix rule would therefore swallow real
# pronouns, so only measured words belong here. A new label word means
# adding it here.
_RE_ENGLISH_I = re.compile(r'\bI\b')
_SECTION_MARKER_WORDS = frozenset({
    'class', 'part', 'step', 'section', 'phase', 'stage', 'level', 'type',
    'item', 'note', 'lesson', 'r', 'r-class', 'version', 'no', 'rule',
    'gate', 'appendix', 'chapter', 'ship',
})

# Personal markers -- what makes a plural pronoun personal. A possessive
# plural ("我们的", "our") is not on this list: it is how method notes
# are written. The private nouns it usually sits next to are.
_FPII_MARKER_RE = re.compile(
    r'家人|family|'
    r'账户|余额|存款|工资|收入|资产|信用卡|贷款|投资组合|持仓|'
    r'老婆|老公|女朋友|男朋友|父母|父亲|母亲|爸爸|妈妈|孩子|女儿|儿子|'
    r'住址|地址|手机号|微信号|邮箱|身份证|银行卡|社保|户口|'
    r'用户主张|用户希望|用户偏好|用户要求|用户规定|用户不满|用户批评|'
    r'用户明确|用户强调|用户声明|用户拒绝|用户纠正|用户说|本人|'
    r'\baddress\b|\bpassword\b|\baccount\b|\bsalary\b|\bincome\b|'
    r'\bwife\b|\bhusband\b|\bdaughter\b|\bson\b'
)

# v1.17.32: six structural axes, one regex each so each can be measured
# on its own -- see tools/check_plural_axes.py.

# Axis 1: private assets -- a key, a figure, a client, a product.
_FPII_MARKER_ASSET_RE = re.compile(
    r'密钥|私钥|口令|凭据|凭证|营收|估值|净值|仓位|持股|客户名单|客户群|'
    r'供应商|报价|未发布|尽调|股东|股份|行程|旅行计划|确诊|处方|病历|'
    r'管理员|权限|分工|租金|房贷|'
    r'\bsecret\b|\btoken\b|\bapi key\b|\bprivate key\b|\bcredential\b|'
    r'\brevenue\b|\bvaluation\b|\bnet value\b|\bportfolio\b|\bholdings?\b|'
    r'\bpositions?\b|\bclient list\b|\bsupplier\b|\bquote[sd]?\b|'
    r'\bunreleased\b|\bproprietary\b|\bconfidential\b|\bdo not share\b|'
    r'\bdiagnosis\b|\bprescription\b|\brent\b|\bmortgage\b|'
    r'\badministrator\b|\bpermissions?\b'
)

# Axis 2: residence -- where a speaker lives.
_FPII_MARKER_RESIDENCE_RE = re.compile(
    r'(?:我们|咱|we)\s*(?:搬|住|租|定居|落脚|移居|安家)|'
    r'\bwe\s+(?:moved|relocated|live|rent|stay)|'
    r'房东|landlord|房租|租金'
)

# Axis 3: family members and pets.
_FPII_MARKER_FAMILY_RE = re.compile(
    r'猫|狗|宠物|宝宝|婴儿|'
    r'\bkids?\b|\bchild(?:ren)?\b|\bbaby\b|\bpets?\b|'
    r'\bcat\b|\bdog\b|\bnewborn\b'
)

# Axis 4: medical events.
_FPII_MARKER_MEDICAL_RE = re.compile(
    r'手术|住院|化验|处方|复查|病假|'
    r'\bsurgery\b|\bhospitali[sz]ed\b|\bprescription\b|'
    r'\bchemotherapy\b|\bclinic\b|\bstroke\b'
)

# Axis 5: travel.
_FPII_MARKER_TRAVEL_RE = re.compile(
    r'旅行|旅游|行程|机票|酒店|度假|出发地|签证|'
    r'\btravel\b|\btrip\b|\bvacation\b|\bflight\b|\bitinerary\b|\btickets?\b'
)

# Axis 6: undisclosed business intent.
_FPII_MARKER_INTENT_RE = re.compile(
    r'(?:打算|计划|准备|考虑)\s*[^\n]{0,6}'
    r'(?:卖掉|出售|并购|收购|融资|上市|裁员|关停|合并|退市)|'
    r'\b(?:plan|intend|going)\s+to\s+(?:sell|merge|acquire|raise|buy|IPO|'
    r'lay\s+off|shut\s+down)\b'
)

_FPII_MARKER_AXES = (
    _FPII_MARKER_ASSET_RE,
    _FPII_MARKER_RESIDENCE_RE,
    _FPII_MARKER_FAMILY_RE,
    _FPII_MARKER_MEDICAL_RE,
    _FPII_MARKER_TRAVEL_RE,
    _FPII_MARKER_INTENT_RE,
)

# A credential in the text, not a word about credentials. This is the PII
# layer's job: it is a value, not a topic. "our key rotation is automated"
# is a method note; "our API key is ghp_..." is a secret.
_RE_CREDENTIAL_VALUE = re.compile(
    r'(?:api[_-]?key|secret|token|password|passwd|pwd|私钥|密钥|密码|口令)'
    r'\s*(?:[:=]|是|为|\bis\b)\s*[\'"]?[A-Za-z0-9_\-\.]{8,}'
    r'|\bgh[pousr]_[A-Za-z0-9]{16,}'
    r'|\bsk-[A-Za-z0-9]{16,}'
    r'|\bAKIA[0-9A-Z]{12,}'
    r'|\bxox[baprs]-[A-Za-z0-9-]{10,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'|\bAIza[0-9A-Za-z_\-]{30,}'
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


def _has_english_pronoun_i(text: str) -> bool:
    """A standalone capital I that is a pronoun, not a list marker."""
    for m in _RE_ENGLISH_I.finditer(text or ''):
        before = re.findall(r'([A-Za-z-]+)\s*$', text[:m.start()])
        if before and before[-1].lower() in _SECTION_MARKER_WORDS:
            continue
        return True
    return False


def has_first_person(text: str) -> bool:
    """True when the text reads as private first-person content.

    Singular forms always count. A plural pronoun counts only when a
    personal marker is nearby, so "our approach to the scanner" is a
    method note and "our account balance is 120000" is not.
    """
    t = text or ''
    if (_FPII_SINGULAR_RE.search(t) or _FPII_HOME_RE.search(t)
            or _has_english_pronoun_i(t)):
        return True
    if _FPII_PLURAL_RE.search(t):
        if _FPII_MARKER_RE.search(t):
            return True
        for _axis in _FPII_MARKER_AXES:
            if _axis.search(t):
                return True
    return False


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
        # v1.17.15: the kind whitelist is the design's rule for what may
        # be shared -- AUTO_COMMONS_KINDS is lessons, methods, workflows,
        # models and patterns. An explicit hint must not promote a kind
        # that is not shareable by design: three `fact` rows reached the
        # public tier this way, and the public tier is what every peer
        # can read. `am mine-public` enforces the same whitelist when it
        # promotes, so the write path has to agree with it.
        if kind and kind not in AUTO_COMMONS_KINDS:
            return {
                'visibility': 'personal',
                'reason': 'kind_not_shareable',
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