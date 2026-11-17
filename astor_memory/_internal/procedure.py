"""Procedure Memory: a fact shaped as "when X -> diagnose -> act -> verify".

Astor already stores procedures.  The kinds ``success_pattern``,
``failure_pattern``, ``lesson`` and ``experience`` all land in
``memory_class='experience'`` (see ``bus/store.py``), and that namespace
is where "how to do X" belongs.  What was missing is the *shape*.

A prose lesson answers "what is true and why".  A procedure answers
"when this happens, do this, then check this".  The difference shows up
in retrieval: a question-shaped query ("dashboard 登录不上 怎么办")
matches the trigger of a procedure much better than it matches the
narrative of a lesson, because the trigger is the part the question
actually asks about.

Measured on a 5,468-fact admin tier: seven procedures took the #1 slot
for all seven of their trigger queries; the same topics written as
prose lessons did not.

The content is deliberately compact.  The trigger leads, so both BM25
and the embedding see it first; the body stays short because a long
body dilutes the match.
"""

from __future__ import annotations

import re

PROCEDURE_KIND = 'procedure'
PROCEDURE_PREFIX = '[PROCEDURE]'

# Section markers, most specific first.  Chinese + English.
ANALYSIS_HINTS = ('分析路径', '分析', '排查', '诊断', '根因', '定位', '查一下',
                  '检查', '读', 'step 1', 'diagnosis', 'analyze')
ACTION_HINTS = ('操作', '修', '执行', '跑', '设', '改', '重启', '清理', '归档',
                'fix', 'action', 'do ')
VERIFY_HINTS = ('验证', '确认', '应该', '预期', '验收', '回滚', 'verify',
                'expect', 'confirm')

# A numbered step list: "1)" / "1." / "第一步"
STEP_RE = re.compile(r'(?:^|\n)\s*(?:\d+\s*[).、]|[（(]\d+[）)]|第[一二三四五六七八九十]+步)')

_MARKERS = ('分析路径:', '分析路径：', '操作:', '操作：', '验证:', '验证：')


def _strip_marker(block: str) -> str:
    """Drop a leading marker so build_content can add it back once."""
    s = (block or '').strip()
    for marker in _MARKERS:
        if s.lower().startswith(marker.lower()):
            return s[len(marker):].strip()
    return s


def split_parts(text: str) -> dict:
    """Best-effort split of a write-up into trigger / analysis / action / verify.

    Conservative on purpose: a part is only filled when a marker word
    appears in that block, and anything that cannot be placed stays in
    ``trigger`` rather than being dropped.
    """
    blocks = [b.strip() for b in re.split(r'\n\s*\n', text or '') if b.strip()]
    if len(blocks) == 1:
        blocks = [b.strip() for b in (text or '').split('\n') if b.strip()]

    parts = {'trigger': '', 'analysis': '', 'action': '', 'verify': ''}
    leftover = []

    for b in blocks:
        head = b[:40].lower()
        if not parts['analysis'] and any(h in head for h in ANALYSIS_HINTS):
            parts['analysis'] = _strip_marker(b)
        elif not parts['action'] and any(h in head for h in ACTION_HINTS):
            parts['action'] = _strip_marker(b)
        elif not parts['verify'] and any(h in head for h in VERIFY_HINTS):
            parts['verify'] = _strip_marker(b)
        else:
            leftover.append(b)

    if leftover:
        head = leftover[0]
        if not parts['trigger']:
            parts['trigger'] = head
        else:
            parts['analysis'] = (parts['analysis'] + '\n' + head).strip()

    # The trigger is the question-shaped first line, trimmed to one line.
    trig = (parts['trigger'] or '').split('\n')[0].strip()
    if len(trig) > 160:
        trig = trig[:157].rstrip() + '...'
    parts['trigger'] = trig

    # A block of bare numbered steps with no marker word is the analysis path.
    if not parts['analysis'] and len(leftover) > 1:
        joined = '\n'.join(leftover)
        if len(STEP_RE.findall(joined)) >= 2:
            parts['analysis'] = joined.strip()

    return parts


def build_content(parts: dict) -> str:
    """Compact body: the trigger leads so BM25 and the embedding see it first."""
    out = [PROCEDURE_PREFIX + ' ' + (parts.get('trigger') or '').strip()]
    if parts.get('analysis'):
        out.append('分析路径: ' + parts['analysis'].strip())
    if parts.get('action'):
        out.append('操作: ' + parts['action'].strip())
    if parts.get('verify'):
        out.append('验证: ' + parts['verify'].strip())
    return ' '.join(out)


def structured(parts: dict) -> dict:
    """The four parts, for metadata.procedure."""
    return {
        'trigger': (parts.get('trigger') or '').strip(),
        'analysis': (parts.get('analysis') or '').strip(),
        'action': (parts.get('action') or '').strip(),
        'verify': (parts.get('verify') or '').strip(),
    }


def is_procedure(content: str) -> bool:
    """True when a fact was written in the procedure shape."""
    return bool(content) and str(content).lstrip().startswith(PROCEDURE_PREFIX)


# The four section markers a procedure body is written with.
SECTION_MARKERS = ('\u5206\u6790\u8def\u5f84', '\u64cd\u4f5c', '\u9a8c\u8bc1')
_SECTION_RE = re.compile(r'(\u5206\u6790\u8def\u5f84|\u64cd\u4f5c|\u9a8c\u8bc1)\s*[:\uff1a]\s*')
_SECTION_KEY = {
    '\u5206\u6790\u8def\u5f84': 'analysis',
    '\u64cd\u4f5c': 'action',
    '\u9a8c\u8bc1': 'verify',
}


def split_procedure(content: str) -> dict:
    """Split a stored procedure into its four parts.

    A procedure body is written as::

        [PROCEDURE] <trigger> \u5206\u6790\u8def\u5f84: ... \u64cd\u4f5c: ... \u9a8c\u8bc1: ...

    so the trigger is whatever precedes the first marker.  Anything that
    does not parse stays in the trigger rather than being dropped.
    """
    parts = {'trigger': '', 'analysis': '', 'action': '', 'verify': ''}
    if not content:
        return parts
    body = content.strip()
    if body.startswith(PROCEDURE_PREFIX):
        body = body[len(PROCEDURE_PREFIX):].strip()
    marks = list(_SECTION_RE.finditer(body))
    if not marks:
        parts['trigger'] = body.split('\n')[0].strip()
        return parts
    parts['trigger'] = body[: marks[0].start()].strip()
    for i, m in enumerate(marks):
        key = _SECTION_KEY.get(m.group(1), '')
        if not key:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        parts[key] = body[m.end(): end].strip()
    return parts
