"""v1.17.39: rendering a procedure memory as an agent skill document.

The point of these tests is the boundary: the renderer must produce a
usable document without knowing anything about the platform that will
load it.  If a future change hardcodes a skills directory or a
platform-specific frontmatter key into the renderer, that is a
regression.
"""

import re

import pytest

from astor_memory._internal import skill_doc


def _proc(content: str, fact_id: int = 42, access: int = 7,
          confirmed: str = '2026-11-17T07:04:15.093950+00:00') -> dict:
    from astor_memory._internal.procedure import split_procedure
    return {
        'id': fact_id,
        'content': content,
        'access_count': access,
        'last_confirmed_at': confirmed,
        'parts': split_procedure(content),
    }


SAMPLE = ('[PROCEDURE] dashboard 登录不上 '
          '分析路径: 1) curl 公网 /login; 2) curl 本机 providers '
          '操作: 隧道缺失就建服务 '
          '验证: 公网 /login 返回 200')


# --- document shape ----------------------------------------------------

def test_document_has_frontmatter_and_body():
    doc, _ = skill_doc.render_skill_document(_proc(SAMPLE), name='triage')
    assert doc.startswith('---')
    m = re.search(r'\n---\s*\n', doc[3:])
    assert m
    body = doc[m.end():]
    assert body.strip()


def test_frontmatter_round_trips_through_yaml():
    import yaml
    doc, _ = skill_doc.render_skill_document(_proc(SAMPLE), name='triage')
    m = re.search(r'\n---\s*\n', doc[3:])
    fm = yaml.safe_load(doc[3:m.start() + 3])
    assert fm['name'] == 'triage'
    assert 'Use when' in fm['description']
    assert len(fm['description']) <= 1024
    assert fm['metadata']['astor']['source_facts'] == [42]


def test_traceability_is_recorded():
    doc, _ = skill_doc.render_skill_document(_proc(SAMPLE), name='triage')
    fm = skill_doc.parse_frontmatter(doc)
    astor_meta = fm['metadata']['astor']
    assert astor_meta['source_facts'] == [42]
    assert astor_meta['source_hashes']['42'] == skill_doc.sha256_short(SAMPLE)
    assert astor_meta['source_confirmed_at']['42'].startswith('2026-11-17')
    assert astor_meta['access_count_at_distill'] == 7


def test_extra_frontmatter_is_merged_but_cannot_override_identity():
    doc, _ = skill_doc.render_skill_document(
        _proc(SAMPLE), name='triage',
        extra_frontmatter={'allowed-tools': 'terminal file',
                           'name': 'hijacked',
                           'description': 'hijacked'})
    fm = skill_doc.parse_frontmatter(doc)
    assert fm['metadata']['allowed-tools'] == 'terminal file'
    assert fm['name'] == 'triage'
    assert 'hijacked' not in fm['description']


def test_body_carries_the_four_sections():
    doc, _ = skill_doc.render_skill_document(_proc(SAMPLE), name='triage')
    for heading in ('## Overview', '## When to Use', '## Diagnostic Path',
                    '## Action', '## Verification Checklist', '## Provenance'):
        assert heading in doc


def test_missing_sections_are_marked_not_invented():
    doc, _ = skill_doc.render_skill_document(
        _proc('[PROCEDURE] 只有触发条件'), name='t')
    assert '_No diagnostic path was recorded' in doc
    assert '_No action was recorded' in doc


# --- the boundary ------------------------------------------------------

def test_renderer_does_not_mention_any_platform():
    """astor must not hardcode another agent's layout."""
    import pathlib
    src = pathlib.Path(skill_doc.__file__).read_text(encoding='utf-8')
    for banned in ('hermes', 'Hermes', 'HERMES', 'claude', 'cursor',
                   'allowed-tools', 'metadata.hermes', '.hermes/skills'):
        assert banned not in src, 'renderer leaks platform detail: %s' % banned


def test_renderer_has_no_absolute_paths():
    import pathlib
    src = pathlib.Path(skill_doc.__file__).read_text(encoding='utf-8')
    for banned in ('D:\\\\AI', 'C:\\\\Users', '/Users/', 'someuser'):
        assert banned not in src, 'renderer leaks a host path: %s' % banned


# --- sanitising --------------------------------------------------------

def test_sanitize_replaces_machine_local_paths():
    text = ('[PROCEDURE] 修机器 '
            '分析路径: 读 C:\\Users\\someuser\\AppData\\Local\\hermes\\config.yaml '
            '操作: 跑命令 '
            '验证: 好了')
    doc, report = skill_doc.render_skill_document(
        _proc(text), name='t', sanitize=True)
    assert report['sanitized'] is True
    assert 'someuser' not in doc
    assert 'AppData' in doc          # the useful part survives


def test_sanitize_keeps_the_steps():
    """A dropped sentence would leave an unusable skill."""
    text = ('[PROCEDURE] 修机器 '
            '分析路径: 读 C:\\Users\\someuser\\x.yaml '
            '操作: 跑命令 '
            '验证: 好了')
    doc, _ = skill_doc.render_skill_document(_proc(text), name='t', sanitize=True)
    assert '跑命令' in doc
    assert '## Verification Checklist' in doc
    assert '好了' in doc


def test_sanitize_is_opt_in():
    text = ('[PROCEDURE] 修机器 分析路径: 读 C:\\Users\\someuser\\x.yaml '
            '操作: 跑命令 验证: 好了')
    doc, report = skill_doc.render_skill_document(_proc(text), name='t')
    assert report['sanitized'] is False
    assert 'someuser' in doc


# --- staleness ---------------------------------------------------------

def test_stale_detection_needs_the_source_hash():
    from astor_memory._internal.skill_doc import sha256_short
    assert sha256_short('a') != sha256_short('b')
    assert len(sha256_short('a')) == 16
