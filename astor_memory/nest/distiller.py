"""v1.16.30: nest/distiller.py — extract reusable method/flow/pattern from a
personal fact (visibility=personal) into a clean commons-eligible text.

v1.16.33: STATE-CONST PROTECTION (article 红线一). Files paths, version
numbers, env-var names, function calls, hex hashes, UUIDs are extracted
BEFORE any sentence-split / redaction so they survive distillation verbatim.
Article quote: "路径、ID、版本号、约束条件、待办状态 — 这些必须在压缩前
flush 到持久层，或者干脆常驻不压".

Algorithm (deterministic, no LLM):
  1. Extract state-const tokens (paths, versions, hashes, etc.) — preserved.
  2. Split text into sentences (CN: '。' delimiter, EN: '. ' delimiter).
     Skip-period when followed by digit (so v2.1.3 stays intact).
  3. Per-sentence classify:
     - has_pii(text)             -> DROP (PII-bearing sentence)
     - has_first_person(text)    -> DROP (personal narrative)
     - has_emotion(text)         -> DROP (private feelings)
  4. For surviving sentences, replace specifics:
     - Proper nouns (<NAME_A>, <NAME_B>, <NAME_C>)  -> [USER_A], [USER_B]
     - URLs (mp.weixin.qq.com/s/xxx)     -> [URL]
     - Specific dates (今天, yesterday, 2024-01-01) -> [DATE_1]
     - Specific locations (Beijing, Tokyo) -> [LOCATION_1]
  5. Re-append state-const tokens at end as [STATE:N]=token summary.
  6. Preserve method/flow keywords: "method", "recipe", "step", "step by step",
     "首先", "然后", "步骤", "方法", "流程".

Output: (cleaned_text, removed_segments, distillation_report)
  - cleaned_text:    distiller-free statement (visibility=commons eligible)
  - removed_segments: list of (segment_text, reason) for audit trail
  - distillation_report: {sentences_in, sentences_out, dropped_count, has_pii_after,
                          has_first_person_after, method_keywords_present,
                          state_const_count, state_const_preserved}
"""
from __future__ import annotations
import re

from .visibility_classifier import (
    has_pii, has_first_person, has_emotion, has_geographic,
)


# Sentence delimiter:
#   - CN-style terminator (。！？) always splits.
#   - ASCII .!? only when NOT followed by digit (so v2.1.3 stays intact).
#   - ASCII newline.
#   - CN comma BETWEEN Chinese chars (clause separator).
#     Two cases:
#     (1) ASCII comma followed by space + Chinese char: "我今天心情不好, 微信"
#         -> split before the Chinese char.
#     (2) CN comma between two Chinese chars (no space): "我,他说" -> split.
# The state-const block is appended to the cleaned text and is carried
# across the final sentence split whole, so the split never has to
# understand a path.
_STATE_BLOCK_MARK = '\n\n[STATE-CONST] '

_SENT_SPLIT = re.compile(
    r'(?<=[。！？？！])\s*'
    r'|(?<=[.!?])(?!\d)\s*'
    r'|\n+'
    r'|(?<=[一-鿿]),\s+(?=[一-鿿])'
    r'|(?<=[一-鿿])，(?=[一-鿿])'
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


# v1.16.33: STATE-CONST PROTECTION (article 红线一).
# These patterns MUST survive distillation verbatim — they are the parts
# an Agent's downstream action will literally depend on. Per article
# line "路径、ID、版本号、约束条件、待办状态 — 这些必须在压缩前 flush 到
# 持久层，或者干脆常驻不压". Token-level pruning (LLMLingua) destroys
# these because they're "highly predictable" — distiller must NOT.
_STATE_CONST_PATTERNS = [
    # file paths with line numbers: src/api/v2/handler.py:142 OR C:\path\file.py:42
    # Match drive+absolute, root-relative, or relative (no leading /)
    #
    # v1.17.5: the leading placeholder is part of the path. The shared
    # tier scrubs a machine-local path to "<home>/AppData/...", and a
    # pattern that starts at the first slash captures only the tail of
    # it -- the placeholder, which is the part that says "this was
    # somebody's machine", is what gets dropped.
    # v1.17.8: a placeholder is <anything without a space or a
    # bracket> -- the operator's own placeholders are not always
    # ASCII. "<临时目录>/wechat.html" used to lose the placeholder
    # because the pattern only allowed [a-z-], and the path that
    # survived started at the slash, so the fact read as though the
    # temp directory were part of the filesystem rather than a
    # stand-in for one.
    #
    # A leading dot is allowed: a dotfile (".tmp_x.html") is a file
    # like any other, and the old pattern could not start on one.
    # The extension list carries the web assets -- a method that
    # writes a .html file is the most common kind there is, and it
    # was not in the list at all.
    re.compile(r'(?:<[^<>\s]{1,24}>)?(?:[A-Za-z]:[/\\])?(?:[/\\])?(?:\.[\w-]+|[\w][\w./\\-]*)?\.(?:py|js|ts|tsx|jsx|java|go|rs|c|cpp|h|hpp|md|txt|json|yaml|yml|toml|sh|sql|html|htm|xml|csv|log|ini|cfg|conf|css)(?::\d{1,5})?(?=\b|[\s.,])'),
    # URL paths (preserve the path even when hostname is replaced)
    re.compile(r'(?<!\w)(?:<[^<>\s]{1,24}>)?/[a-zA-Z.][\w./\-]{2,200}'),
    # A bare domain: "mp.weixin.qq.com" says which site a method is
    # for, and without this the sentence splitter cut it into
    # "mp. weixin. qq. com" -- a domain nobody can use. The last
    # label has to be alphabetic, so a version number does not match,
    # and the leading lookbehind keeps a path from being read as a
    # domain.
    re.compile(r'(?<![\w@./-])(?:[a-z0-9-]+\.)+[a-z]{2,24}(?![\w-])'),
    # semantic version
    re.compile(r'\bv?\d+\.\d+\.\d+(?:[-+][\w.]+)?\b'),
    # HTTP status codes (200-599)
    re.compile(r'\b(?:[1-5]\d{2})\b(?=\s+(?:status|code|error|response))', re.I),
    # function/method calls: foo.bar() or Foo.bar()
    re.compile(r'\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,3}\(\)'),
    # env-var like names: caps WITH an underscore. The comment said
    # "3+ caps + underscore" and the pattern did not ask for one, so
    # every ordinary capitalised word -- STATE, CONST, PII, ASCII --
    # became a state constant. A word like that in the body was then
    # replaced a second time, because the placeholder it had already
    # put there held the same letters.
    re.compile(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b'),
    # SHA / hex hashes 8+ chars
    re.compile(r'\b[0-9a-f]{8,}\b'),
    # UUID
    re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I),
    # bare key=value pairs (preserve the value verbatim)
    re.compile(r'\b\w{1,20}=\S+'),
]


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


def _extract_state_const(text: str) -> list[dict]:
    """v1.16.33: extract state-const tokens from original text.

    These survive distillation as a [STATE-CONST] summary block at the
    end of the cleaned text. Tokens are deduped by exact text match; PII
    tokens (e.g. phone fragments, im.wechat, im.bot) get priority via redact path,
    not via this extractor.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for pat in _STATE_CONST_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group()
            if tok in seen:
                continue
            seen.add(tok)
            out.append({
                'token': tok,
                'placeholder': f'[STATE:{len(out)+1}]',
            })
    return out


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

    # v1.16.33: extract state-const BEFORE sentence split. The split
    # regex has to be permissive for periods after digits (v2.1.3) which
    # means file paths like "src/api/v2/handler.py:142" get fragmented
    # otherwise. Extracting them whole up front avoids that.
    state_extractions = _extract_state_const(text)
    # v1.17.8: protect the tokens in the body BEFORE the sentence
    # split. The split cuts on a period, so "wechat.html" became
    # "wechat. html" -- the method was broken in the body while the
    # STATE-CONST block held the intact copy. The body is what a
    # reader reads; a token in it has to survive the split.
    _body_src = text
    # One pass, longest token first. Replacing them one after
    # another let a later token match the letters inside a
    # placeholder an earlier one had already put there: the
    # placeholder for "wechat.html" is "[STATE:1]", and a token
    # "STATE" turned it into "[[STATE:5]:1]". One pass cannot do
    # that, because it never looks at what it just wrote.
    if state_extractions:
        import re as _re_body
        _by_text = {}
        for _e in state_extractions:
            _tok = _e.get('token') or ''
            if _tok and _tok not in _by_text:
                _by_text[_tok] = _e['placeholder']
        if _by_text:
            _rx = _re_body.compile('|'.join(
                _re_body.escape(t) for t in
                sorted(_by_text, key=len, reverse=True)))
            _body_src = _rx.sub(lambda m: _by_text[m.group(0)], _body_src)
    sentences = [s.strip() for s in _SENT_SPLIT.split(_body_src) if s.strip()]
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
    #     whole sentence was "user phone is <PHONE>"), it gets dropped.
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

    # v1.16.33: re-append state-const tokens at end of cleaned. Since
    # sentence split + per-sentence filter may have dropped the original
    # sentence carrying the state token, we can no longer rely on
    # position-based insertion. Instead, summarize all preserved state
    # tokens as a single [STATE-CONST] block at the end.
    if state_extractions:
        # The block is carried across the final split untouched (see
        # below), so the tokens go in verbatim.
        #
        # v1.17.5: they used to go in with their dots and dashes
        # rewritten to full-width, so that the sentence split would not
        # cut a path in half. Nothing restored them afterwards, and the
        # shared tier ended up with "hermes－agent" and "file．py" in
        # it -- a path that no longer resolves anywhere. The block is
        # now kept out of the split instead of being mangled to fit it.
        state_tokens = []
        for e in state_extractions[:20]:
            state_tokens.append(f'{e["placeholder"]}={e["token"]}')
        state_block = _STATE_BLOCK_MARK + ', '.join(state_tokens)
        if cleaned:
            cleaned = cleaned + '\n\n' + state_block
        else:
            # all sentences filtered (e.g. pure-personal text with state tokens);
            # preserve state tokens alone as a single-line commons fact
            cleaned = state_block

    # 4. Re-run PII/first-person check on cleaned (defense in depth — if any
    # placeholder replacement leaked, we drop the segment instead of leaking).
    final_clean: list[str] = []
    # v1.17.5: the state-const block is split off before the final
    # pass. A token holds ASCII dots -- "scripts/handler.py" -- and the
    # split regex cuts on those, so a block that went through the split
    # came out in pieces. It is privacy-checked here as one unit and
    # then carried across whole.
    _body, _sep, _state_block = cleaned.partition(_STATE_BLOCK_MARK)
    for sent in _SENT_SPLIT.split(_body):
        sent = sent.strip()
        if not sent:
            continue
        # v1.17.8: put the token back now that the split is done. The
        # split ran on the placeholder-protected body, so the token
        # came through it whole; the reader gets the real path
        # instead of a reference to a reference. The PII check below
        # runs on the restored text, so a token that carries
        # something private is still caught.
        # One pass again, and the placeholder is used as it is. The
        # old code built a doubled bracket out of it -- "[" +
        # "[STATE:1]" + "]" -- which never matches anything, so the
        # token that had been inside brackets never came back.
        if state_extractions:
            import re as _re_restore
            _back = {}
            for _e in state_extractions:
                _ph = _e.get('placeholder') or ''
                _tok = _e.get('token') or ''
                if _ph and _tok and _ph not in _back:
                    _back[_ph] = _tok
            if _back:
                _rx2 = _re_restore.compile('|'.join(
                    _re_restore.escape(ph) for ph in
                    sorted(_back, key=len, reverse=True)))
                sent = _rx2.sub(lambda m: _back[m.group(0)], sent)
        if has_pii(sent) or has_first_person(sent) or has_emotion(sent):
            removed.append({'segment': sent, 'reason': 'residual_after_distill'})
            continue
        final_clean.append(sent)
    cleaned = ' '.join(final_clean)
    if _sep:
        cleaned = (cleaned + _sep + _state_block).strip()

    # 5. Verify the cleaned text still has at least one method/flow keyword.
    # If not, distill didn't preserve actionable content -> abort (empty).
    # Exception: STATE-CONST block alone is acceptable if user explicitly
    # wrote state-only text.
    has_method = _METHOD_KEYWORDS.search(cleaned)
    has_state = '[STATE-CONST]' in cleaned
    if not has_method and not has_state:
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
        'method_keywords_present': bool(cleaned) and bool(has_method),
        'state_const_count': len(state_extractions),
        'state_const_preserved': bool(state_extractions),  # True if any kept
    }
    return cleaned, removed, report



def clear_tool_results(
    text: str,
    keep_recent_n: int = 3,
) -> tuple[str, list[dict]]:
    """v1.16.33: tool result clearing (article "工具结果清除 tool result clearing").

    Article quote: "ROI 最高的一招, 也是最少人用的一招。一条 tool_result
    在被消费之后, Agent 为什么还要再看一遍原始 JSON?"

    For fact content that contains embedded tool_result blocks (JSON payloads),
    keep only the most recent N. Older ones get replaced with a compact
    reference placeholder so downstream readers can re-fetch if needed.

    Args:
        text: input fact content (may contain multiple tool_result blocks)
        keep_recent_n: number of most-recent tool_results to keep verbatim

    Returns:
        (cleaned_text, replaced_blocks) — replaced_blocks is the audit log
        of what was dropped, with each entry: {tool_call_id, fetched_at, size_before}
    """
    # Find tool_result blocks: assume they are JSON-encoded within the text.
    # Tag pattern: __tool_result_<id>__ ... __/tool_result__
    pattern = re.compile(
        r'__tool_result_([\w\-]+)__(.*?)__/tool_result__',
        re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) <= keep_recent_n:
        return text, []

    # Drop all but the last N. Note: matches[:-0] is [] (Python slice
    # semantics), so we special-case 0 to mean "drop all".
    to_drop = matches if keep_recent_n == 0 else matches[:-keep_recent_n]
    replaced = []
    cleaned = text
    for m in reversed(to_drop):
        block_id = m.group(1)
        block_content = m.group(2)
        replaced.append({
            'tool_call_id': block_id,
            'size_before': len(block_content),
            'placeholder': f'[TOOL_RESULT_CLEARED:{block_id}]',
        })
        cleaned = cleaned[:m.start()] + f'[TOOL_RESULT_CLEARED:{block_id}]' + cleaned[m.end():]
    return cleaned, replaced



__all__ = ['distill', '_extract_state_const', '_STATE_CONST_PATTERNS', 'clear_tool_results']
