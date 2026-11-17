"""Render a procedure memory as an agent skill document.

Why this lives in astor
-----------------------
A procedure memory records *when* something happens, *how to diagnose*
it, *what to do*, and *how to confirm it worked*.  That is the shape of
an agent skill, but a memory row only helps when something recalls it.
A skill document changes behaviour instead.

The renderer is deliberately platform-neutral.  It emits the Agent
Skills convention -- frontmatter with ``name`` and ``description``, then
a markdown body -- and nothing else.  It does not know where skills
live, what a platform calls its tool list, or which agent is going to
load it.  The caller supplies the output directory and any
platform-specific frontmatter.

Traceability
------------
Every rendered document records the facts it came from, with a content
hash per fact.  When a source fact changes or is archived, the document
can be detected as stale instead of quietly rotting -- a stale skill is
worse than no skill, because it is confident and wrong.

Sanitising
----------
``--sanitize`` runs the text through astor's own distiller, which strips
PII, first-person narrative, and replaces names, URLs, dates and
locations with placeholders.  It is opt-in: a skill's diagnostic path
usually *needs* the concrete path and command to be executable, so
sanitising by default would produce a skill nobody can run.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from .procedure import PROCEDURE_PREFIX, split_procedure

__all__ = [
    'render_skill_document', 'render_frontmatter', 'parse_frontmatter',
    'sha256_short', 'skill_body', 'now_iso',
]


def sha256_short(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_frontmatter(text: str) -> dict:
    """Read the frontmatter.  PyYAML does this; do not hand-roll it."""
    try:
        import yaml
    except ImportError:
        return {}
    if not text.startswith('---'):
        return {}
    m = re.search(r'\n---\s*\n', text[3:])
    if not m:
        return {}
    try:
        fm = yaml.safe_load(text[3: m.start() + 3]) or {}
    except Exception:
        return {}
    return fm if isinstance(fm, dict) else {}


def _yaml_dump(data: dict) -> str:
    """Emit YAML that a frontmatter parser reads back unchanged."""
    import yaml
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=False).rstrip('\n')


def render_frontmatter(name: str, description: str,
                        source_facts: list[int],
                        source_hashes: dict,
                        source_confirmed_at: dict,
                        access_count: int = 0,
                        extra: dict | None = None) -> str:
    """The frontmatter block, including the traceability record."""
    meta = {
        'version': '1.0.0',
        'author': 'astor-skill-distill',
        'updated': now_iso(),
        'astor': {
            'source_facts': list(source_facts),
            'source_hashes': {str(k): v for k, v in (source_hashes or {}).items()},
            'source_confirmed_at': {str(k): v for k, v in (source_confirmed_at or {}).items()},
            'distilled_at': now_iso(),
            'access_count_at_distill': int(access_count or 0),
        },
    }
    # Platform-specific fields ride along untouched; the renderer does
    # not know or care what they mean.
    if extra:
        for k, v in extra.items():
            if k in ('name', 'description'):
                continue
            meta[k] = v

    fm = {'name': name, 'description': description, 'metadata': meta}
    return '---\n' + _yaml_dump(fm) + '\n---'


def skill_body(parts: dict, source_facts: list[int],
               source_hashes: dict, source_confirmed_at: dict,
               access_count: int = 0) -> str:
    """The markdown body of the skill document."""
    trigger = (parts.get('trigger') or '').strip()
    lines = [
        '',
        '# %s' % (trigger[:80] or 'Procedure'),
        '',
        '## Overview',
        '',
        'This document was rendered from a procedure memory. The procedure '
        'recorded the situation, the diagnostic path, the action and the '
        'confirmation.',
        '',
        '**Situation**: %s' % (trigger or '(not recorded)'),
        '',
        '## When to Use',
        '',
        '- %s' % (trigger or '(situation not recorded)'),
        '- The diagnostic path below is the recorded one; follow it in order '
        'before improvising.',
        '',
        '## Diagnostic Path',
        '',
    ]
    lines.append(parts.get('analysis') or
                 '_No diagnostic path was recorded for this procedure._')
    lines += ['', '## Action', '']
    lines.append(parts.get('action') or
                 '_No action was recorded for this procedure._')
    lines += [
        '',
        '## Verification Checklist',
        '',
        '- [ ] %s' % (parts.get('verify') or
                      'Confirm the outcome before reporting done.'),
        '',
        '## Provenance',
        '',
    ]
    for fid in source_facts:
        lines.append('- source fact `%s`  hash `%s`  last confirmed `%s`'
                     % (fid, (source_hashes or {}).get(str(fid), '?'),
                        (source_confirmed_at or {}).get(str(fid), '?')))
    lines += [
        '- access count at render time: %s' % int(access_count or 0),
        '- rendered at: %s' % now_iso(),
        '',
        '_When a source fact changes or is archived, this document goes '
        'stale._',
        '',
    ]
    return '\n'.join(lines)


def render_skill_document(proc: dict, name: str, description: str = '',
                          extra_frontmatter: dict | None = None,
                          sanitize: bool = False) -> tuple[str, dict]:
    """Render one procedure into a skill document.

    ``proc`` is a mapping with ``id``, ``content``, ``access_count``,
    ``last_confirmed_at`` and the four ``parts``.

    Returns ``(document, report)``.
    """
    parts = dict(proc.get('parts') or split_procedure(proc.get('content') or ''))
    report: dict = {'sanitized': False, 'removed': []}

    if sanitize:
        # scrub_local_identifiers *replaces* machine-local identifiers;
        # the distiller *drops whole sentences*, which would silently
        # delete a diagnostic step. A skill that lost its steps is
        # worse than one that kept a path.
        try:
            from ..nest.public_scrub import scrub_local_identifiers as _scrub
        except Exception as exc:  # pragma: no cover - import guard
            report['sanitize_error'] = str(exc)
        else:
            for key in ('trigger', 'analysis', 'action', 'verify'):
                if not parts.get(key):
                    continue
                cleaned, changes = _scrub(parts[key])
                if changes:
                    parts[key] = cleaned
                    report['sanitized'] = True
                    report['removed'].extend(changes)

    trigger = (parts.get('trigger') or '').strip()
    desc = description or (
        'Use when %s. Rendered from a procedure memory; follow the steps '
        'and confirm the outcome.' % trigger)

    fid = proc.get('id')
    hashes = {str(fid): sha256_short(proc.get('content') or '')}
    confirmed = {str(fid): proc.get('last_confirmed_at')}

    front = render_frontmatter(
        name=name, description=desc, source_facts=[fid],
        source_hashes=hashes, source_confirmed_at=confirmed,
        access_count=proc.get('access_count') or 0,
        extra=extra_frontmatter)
    body = skill_body(parts, [fid], hashes, confirmed,
                      proc.get('access_count') or 0)
    report['parts'] = parts
    return front + '\n' + body, report
