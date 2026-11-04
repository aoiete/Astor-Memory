"""v1.17.31: first-person rule C — plural pronouns need a personal marker.

The 10 locked cases in test_visibility_classifier.py are permanent and
stay untouched. This file covers the new behaviour.

Note the style: pytest functions, not a `main()` script. A script-style
test file is not collected by pytest, so its cases never run — the
locked file next to this one had exactly that problem until v1.17.31.
"""
import os
import sys

import pytest

sys.path.insert(0, os.environ.get('ASTOR_TEST_SRC', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from astor_memory.nest.visibility_classifier import (
    classify_visibility, has_first_person, has_pii,
)

# (description, text, kind, expected_visibility)
CASES = [
    # plural pronoun, no personal marker -> shareable method note
    ('plural alone: 我们 + method note',
     '我们把请求超时设成 3 秒, 端到端延迟从 800ms 降到 120ms。',
     'method', 'commons'),
    ('plural alone: our + method note',
     'our approach to the scanner, verified on the holdout split.',
     'method', 'commons'),
    ('plural alone: our + own',
     'our method notes live in docs/design/ and are versioned with the code.',
     'method', 'commons'),
    ('plural alone: we + past tense',
     'we lowered the timeout from 800ms to 120ms and the tail latency followed.',
     'method', 'commons'),
    ('plural alone: (中文) 结果',
     '我们把结果写进 ALPHA_FINDINGS_2026-11-02.md, 便于下个 session 直接读。',
     'lesson', 'commons'),

    # bare "home" is a path word, not a place
    ('bare home: path placeholder',
     'HERMES_PY=<home>/.venv/Scripts/python.exe',
     'method', 'commons'),
    ('bare home: directory',
     'the home directory holds the venv',
     'method', 'commons'),

    # singular forms still block
    ('singular 我 still blocks',
     '我发明了 X 方法来解决这个问题',
     'method', 'personal'),
    ('singular 我的 still blocks',
     '我的账户余额是 120000',
     'method', 'personal'),
    ('my still blocks',
     'my salary is high',
     'method', 'personal'),

    # plural + personal marker still blocks
    ('plural + financial marker',
     'our bank account balance is 50000',
     'method', 'personal'),
    ('plural + family marker',
     'our family',
     'method', 'personal'),
    ('plural + address marker',
     'our home address is 12 Some Road',
     'method', 'personal'),
    ('plural + financial marker (CN)',
     '我们的家庭住址在某某区',
     'method', 'personal'),
    ('plural + user stance',
     '用户主张「什么情况我们都能算,没有拒绝选项」',
     'lesson', 'personal'),

    # "my home" is personal even though bare "home" is not
    ('my home blocks',
     'my home directory is not where the venv lives',
     'method', 'personal'),
]


@pytest.mark.parametrize('desc,text,kind,expected', CASES,
                         ids=[c[0] for c in CASES])
def test_visibility_rule_c(desc, text, kind, expected):
    r = classify_visibility(text=text, kind=kind, hint=None,
                            admin_allow_commons=True)
    assert r['visibility'] == expected, (
        f'{desc}: got {r["visibility"]!r}, want {expected!r} '
        f'(reason={r["reason"]}, signals={r["signals"]})')


@pytest.mark.parametrize('text,expected', [
    ('our approach to the scanner, verified on the holdout.', False),
    ('our bank account balance is 50000', True),
    ('the home directory holds the venv', False),
    ('my home directory', True),
    ('我今天心情不好想自己静静', True),
], ids=[
    'plural without marker -> False',
    'plural with marker -> True',
    'bare home -> False',
    'my home -> True',
    'singular -> True',
])
def test_has_first_person(text, expected):
    assert has_first_person(text) is expected


@pytest.mark.parametrize('text', [
    '用 US.SPY 跟 US.TQQQ 对比点差。',
    '对比 QQQI 与 SQQQ 的费率',
], ids=['TQQQ', 'QQQI/SQQQ'])
def test_ticker_is_not_pii(text):
    """A ticker that contains QQ is an ETF, not a chat id."""
    assert has_pii(text) == []


def test_qq_id_is_still_pii():
    assert has_pii('我的QQ号是 12345')
