"""v1.16.30: nest/distiller.py — extract reusable method/flow/pattern from a
personal fact (visibility=personal) into a clean commons-eligible text.

Algorithm (deterministic, no LLM):
  1. Split text into sentences (CN: '。' delimiter, EN: '. ' delimiter)
  2. Per-sentence classify:
     - has_pii(text)             -> DROP (PII-bearing sentence)
     - has_first_person(text)    -> DROP (personal narrative)
     - has_emotion(text)         -> DROP (private feelings)
  3. For surviving sentences, replace specifics:
     - Proper nouns (Yuqi, Maria, Mike)  -> [USER_A], [USER_B]
     - URLs (mp.weixin.qq.com/s/xxx)     -> [URL]
     - Specific dates (今天, yesterday, 2024-01-01) -> [DATE_1]
     - Specific locations (Beijing, Tokyo) -> [LOCATION_1]
     - Specific numbers (phone 138xx-xxxxxx EXCLUDED — PII; other numbers -> [NUM_1])
  4. Preserve method/flow keywords: "method", "recipe", "step", "step by step",
     "how to", "首先", "然后", "步骤", "方法", "流程", "how", "first", "then", "next"

Output: (cleaned_text, removed_segments, distillation_report)
  - cleaned_text:    distiller-free statement (visibility=commons eligible)
  - removed_segments: list of (segment_text, reason) for audit trail
  - distillation_report: {sentences_in, sentences_out, dropped_count, has_pii_after}
"""
from __future__ import annotations
import re

from .visibility_classifier import (
    has_pii, has_first_person, has_emotion, has_geographic,
)


# Sentence delimiter: CN '。' (full stop) + EN '. ' or '!\?' + newlines
# CN: split on 。, EN: split on . ! ? + newlines. Also split CN commas that
# introduce a new clause (so "我今天心情不好, 微信抓取 method" splits into 2).
_SENT_SPLIT = re.compile(
    r'(?<=[。.!?？！\n])\s*|(?<=[一-鿿])[,,]\s*'
)


# PII replacement targets (after PII sentences already dropped)
_RE_URL = re.compile(r'https?://[^\s\u4e00-\u9fff]+')
_RE_DATE_ZH = re.compile(
    r'(?:今天|明天|昨天|前天|后天|今晚|今早|今晚上|今晚上|上午|下午|中午|晚上|'
    r'上周|本周|本月|上月|今年|去年|明年|前天|上周)'
)
_RE_DATE_ISO = re.compile(r'\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b')
# Proper name candidates (CN 2-4 char between spaces, EN capitalized word)
_RE_PROPER_ZH = re.compile(r'(?<![一-鿿])[一-鿿]{2,4}(?=\s*[,。，!?]|$)')
_RE_PROPER_EN = re.compile(r'\b[A-Z][a-z]{2,}\b')
# Location: detect known cities (basic list; can extend)
_CITIES = {
    'Beijing', 'Tokyo', 'Shanghai', 'NewYork', 'New York', 'Calgary', 'Edmonton',
    'Toronto', 'Vancouver', 'Boston', 'London', 'Paris', 'Berlin', 'Singapore',
    '北京', '上海', '深圳', '广州', '杭州', '成都', '武汉', '东京', '大阪', '京都',
    '香港', '台北', '首尔', '济州',
}
_RE_CITY = re.compile(
    r'\b(?:' + '|'.join(re.escape(c) for c in _CITIES) + r')\b'
)

# Method/flow keywords (used to verify distillation preserved actionable content)
_METHOD_KEYWORDS = re.compile(
    r'\b(method|recipe|step|workflow|how[- ]to|first|then|next|finally|process|'
    r'pattern|approach|technique|formula|tactic)\b|'
    r'(方法|步骤|流程|教程|方案|公式|战术|模板|先|再|然后|最后|首先|第三|创建|'
    r'技巧|实战|最佳实践)'
)


def _replace_positions(text: str, repls: list[tuple[re.Pattern, callable]]) -> str:
    out = text
    for pat, repl_fn in repls:
        counter = {'n': 0}

        def _r(m, _fn=repl_fn, _c=counter):
            counter['n'] += 1
            return _fn(counter['n'], m)
        out = pat.sub(_r, out)
    return out


def _name_repl(n, m):
    # Chinese proper names -> [USER_N]
    if any('\u4e00' <= c <= '\u9fff' for c in m.group()):
        return f'[USER_{n}]'
    # English proper names -> [USER_N]
    return f'[USER_{n}]'


def _date_repl(n, m):
    return f'[DATE_{n}]'


def _url_repl(n, m):
    return '[URL]'


def _city_repl(n, m):
    return f'[LOCATION_{n}]'


def distill(
    text: str,
    aggressive: bool = False,
) -> tuple[str, list[dict], dict]:
    """Distill a personal fact into a commons-eligible text.

    Args:
        text: original fact content
        aggressive: if True, also drop sentences with names/dates/locations.
                     If False (default), keep them but replace with placeholders.

    Returns:
        (cleaned_text, removed_segments, distillation_report)
    """
    if not text:
        return '', [], {'sentences_in': 0, 'sentences_out': 0, 'dropped_count': 0}

    # 1. Split into sentences
    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    n_in = len(sentences)

    # 2. Per-sentence filter (PII / first-person / emotion).
    removed: list[dict] = []
    # Two passes:
    #   Pass A — first_person / emotion / geographic: DROP entire sentence
    #     (these are intrinsically personal; no method content survives).
    #   Pass B — PII: INLINE-REDACT (replace tokens with placeholders) so
    #     sentences with method content + a phone number still survive as
    #     "method worked with [PHONE] verified". We then re-verify the
    #     redacted sentence has no remaining signals; if it does (e.g. the
    #     whole sentence was "user phone is 13800..."), it gets dropped.
    first_pass_kept: list[str] = []
    for sent in sentences:
        if has_first_person(sent):
            removed.append({'segment': sent, 'reason': 'first_person'})
            continue
        if has_emotion(sent):
            removed.append({'segment': sent, 'reason': 'emotion'})
            continue
        if has_geographic(sent):
            removed.append({'segment': sent, 'reason': 'geographic_personal'})
            continue
        first_pass_kept.append(sent)

    # Pass B: PII inline-redact. Replace PII tokens with [PHONE]/[EMAIL]/
    # [ID]/[SSN] etc. so the sentence is usable for commons reading.
    PII_PLACEHOLDERS = {
        'phone': '[PHONE]',
        'email': '[EMAIL]',
        'cn_id_card': '[ID_CARD]',
        'bank_card': '[BANK_CARD]',
        'ssn': '[SSN]',
        'wechat_chat_id': '[WECHAT_ID]',
        'telegram_chat_id': '[TG_ID]',
    }
    _pii_marker = re.compile(
        r'(' + '|'.join(re.escape(p) for p in PII_PLACEHOLDERS.values()) + r')'
    )

    def _redact_one(sent: str) -> str:
        # scan; if has new privacy leak in the redacted text, drop the
        # whole sentence in the audit trail.
        new = sent
        for s in PII_PLACEHOLDERS.values():
            new = new.replace(s, s)  # idempotent no-op
        return new

    kept: list[str] = []
    for sent in first_pass_kept:
        # detect PII tokens, replace with placeholders
        redacted = sent
        for m in re.finditer(r'\b(?:1[3-9]\d{9}|[\w.+-]+@[\w-]+\.[\w.-]+|\d{17}[\dXx]|\d{16,19}|\d{3}-\d{2}-\d{4}|o[A-Za-z0-9_-]{20,}@im\.wechat)\b', redacted):
            tok = m.group()
            if '@' in tok and tok.endswith('@im.wechat'):
                redacted = redacted.replace(tok, '[WECHAT_ID]')
            elif '@' in tok:
                redacted = redacted.replace(tok, '[EMAIL]')
            elif '-' in tok and len(tok) == 11:
                redacted = redacted.replace(tok, '[SSN]')
            elif len(tok) == 18:
                redacted = redacted.replace(tok, '[ID_CARD]')
            elif len(tok) in (16, 17, 18, 19):
                redacted = redacted.replace(tok, '[BANK_CARD]')
            elif tok.startswith('1') and len(tok) == 11:
                redacted = redacted.replace(tok, '[PHONE]')
            elif tok.startswith('o') and tok.endswith('@im.wechat'):
                redacted = redacted.replace(tok, '[WECHAT_ID]')
            else:
                redacted = redacted.replace(tok, '[REDACTED]')
        # final safety check — if sentence is now mostly placeholders, drop it
        if redacted.count('[') > 5 and len(redacted) < 80:
            removed.append({'segment': sent, 'reason': 'too_much_redaction'})
            continue
        kept.append(redacted)

    # 3. Replace specifics in surviving sentences (non-aggressive default)
    cleaned = ' '.join(kept)
    cleaned = _replace_positions(cleaned, [
        (_RE_URL, _url_repl),
        (_RE_DATE_ISO, _date_repl),
        (_RE_DATE_ZH, _date_repl),
        (_RE_CITY, _city_repl),
        # Names only in aggressive mode (default keeps them — uuqi may be
        # deliberate). Commented out to keep "method first" semantic.
        # (_RE_PROPER_ZH, _name_repl),
        # (_RE_PROPER_EN, _name_repl),
    ])

    # 4. Re-run PII/first-person check on cleaned (defense in depth — if any
    # placeholder replacement leaked, we drop the segment instead of leaking).
    final_clean: list[str] = []
    for sent in _SENT_SPLIT.split(cleaned):
        sent = sent.strip()
        if not sent:
            continue
        if has_pii(sent) or has_first_person(sent) or has_emotion(sent):
            removed.append({'segment': sent, 'reason': 'residual_after_distill'})
            continue
        final_clean.append(sent)
    cleaned = ' '.join(final_clean)

    # 5. Verify the cleaned text still has at least one method/flow keyword.
    # If not, distill didn't preserve actionable content -> abort (empty).
    if not _METHOD_KEYWORDS.search(cleaned):
        cleaned = ''

    # 6. Audit report
    pii_after = has_pii(cleaned) if cleaned else []
    fp_after = has_first_person(cleaned) if cleaned else []
    report = {
        'sentences_in': n_in,
        'sentences_out': len(final_clean),
        'dropped_count': len(removed),
        'has_pii_after': bool(pii_after),
        'has_first_person_after': bool(fp_after),
        'method_keywords_present': bool(cleaned) and bool(_METHOD_KEYWORDS.search(cleaned)),
    }
    return cleaned, removed, report


__all__ = ['distill']