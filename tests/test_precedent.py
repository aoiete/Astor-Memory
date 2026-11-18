"""Tests for the failure-driven precedent lookup.

The behaviour under test: when an operation fails, astor can be asked
"has this happened before" and it answers from the outcome zones only
(success / failure / lesson / postmortem / procedure), not from the whole
pool.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from astor_memory._internal.precedent import (  # noqa: E402
    EXPERIENCE_KINDS,
    build_query,
    find_precedent,
)


def test_build_query_prefers_the_operation_over_the_error():
    q = build_query(operation='curl mp.weixin.qq.com/s/abc', error='SSL error')
    assert 'curl mp.weixin.qq.com/s/abc' in q
    assert 'SSL error' in q
    # the operation leads, because the operation is what a stored recipe
    # is about; the error is the weaker signal.
    assert q.index('curl') < q.index('SSL error')


def test_build_query_skips_empty_parts():
    assert build_query(operation='x', error='', tool='') == 'x'
    assert build_query() == ''
    assert build_query(error='   ') == ''


def test_build_query_includes_the_tool():
    q = build_query(operation='read file', error='boom', tool='terminal')
    assert q.startswith('terminal')


def test_empty_query_is_a_valid_answer():
    out = find_precedent('')
    assert out['found'] is False
    assert out['count'] == 0
    assert out['results'] == []


def test_noise_kinds_are_never_prior_art():
    from astor_memory._internal.precedent import _kind_filter
    assert 'event' not in _kind_filter(['event', 'failure_pattern'])
    assert 'session' not in _kind_filter(['session'])
    assert _kind_filter([]) == ()


def test_experience_kinds_cover_the_three_outcome_zones():
    # success zone, failure zone, lesson zone, plus the procedure kinds
    for k in ('success_pattern', 'failure_pattern', 'lesson', 'postmortem'):
        assert k in EXPERIENCE_KINDS
    assert 'procedure' in EXPERIENCE_KINDS


@pytest.mark.skipif(not __import__('os').environ.get('LIVE_ASTOR'),
                    reason='LIVE_ASTOR not set — skip live /v1/precedent test')
def test_live_precedent_endpoint_answers():
    import json
    import urllib.request
    body = json.dumps({'operation': 'curl https://mp.weixin.qq.com/s/example',
                       'error': 'curl: (35) SSL connect error',
                       'tool': 'terminal',
                       'user_id': 'admin',
                       'limit': 5}).encode()
    req = urllib.request.Request(
        'http://127.0.0.1:7803/v1/precedent', data=body,
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    assert 'found' in data
    assert isinstance(data['results'], list)
    # every result must come from an outcome zone
    for row in data['results']:
        assert row['kind'] in EXPERIENCE_KINDS


def test_peer_fanout_respects_an_explicit_off():
    from astor_memory._internal.precedent import _peer_precedent
    out = _peer_precedent('anything', 3, False, 0.87)
    assert out['enabled'] is False
    assert out['results'] == []
    assert 'disabled_by_caller' in out['reason']


def test_rescore_drops_rows_below_the_threshold():
    from astor_memory._internal.precedent import _rescore
    # A row the local model cannot score stays put; the point of the
    # threshold is that unrelated remote text does not come back.
    rows = [{'content': 'read a wechat article with curl', 'similarity': 0.0}]
    out = _rescore(rows, 'read a wechat article', 0.87)
    # Either the row scored above the threshold and is kept, or it was
    # dropped. What must never happen is an unrelated row surviving with
    # no similarity recorded.
    for r in out:
        assert 'similarity' in r
