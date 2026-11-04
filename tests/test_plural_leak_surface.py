"""The adversarial set and the positive set, as regression tests.

v1.17.32: the plural-pronoun rule shipped in v1.17.31 leaked 34 of 35
adversarial private sentences. This file pins both directions:
the adversarial set must stay out of the shared tier, and the positive
set of method notes must stay in it.

A rule that blocks everything passes the adversarial set alone. Both
sets run here, every time.
"""
import os
import sys

import pytest

sys.path.insert(0, os.environ.get('ASTOR_TEST_SRC', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from astor_memory.nest.visibility_classifier import (
    classify_visibility, has_first_person, has_pii,
)

# private content that uses only a plural pronoun and no marker word
ADVERSARIAL = [
    # credentials and keys
    "我们的 API 密钥是 ghp_9f2a7c3e1b8d4f6a0c5e2b7d1f9a3c6e",
    "our API key is ghp_9f2a7c3e1b8d4f6a0c5e2b7d1f9a3c6e",
    "我们的私钥放在 D 盘那个目录里, 别弄丢",
    "our private key lives in the same folder as the cert",
    "我们的 token 每 30 天轮换一次",
    "our access token rotates every 30 days",
    # money and business figures
    "我们的营收去年做到 1200 万, 今年砍到 800 万",
    "our revenue dropped from 12M to 8M last year",
    "我们的估值是 2.3 亿, 下一轮要谈 3 亿",
    "our valuation is 230M and the next round is 300M",
    "我们的仓位在 9 月底全部清空过, 那是我们的底线",
    "our positions were fully closed in late September, that was our floor",
    "我们的净值在 6 月亏了 22%",
    "our net value fell 22% in June",
    # people and relationships
    "our kid starts school this year and needs two pickups a day",
    "我们家的猫上周做了一次手术, 花了 3400",
    "our cat had surgery last week, it cost 3400",
    # places of residence
    "我们在温哥华租了个公寓, 房东是本地人",
    "we rent an apartment in Vancouver and the landlord is local",
    "我们搬到了温哥华, 这边的雨跟卡尔加里完全不一样",
    "we moved to Vancouver, the rain here is nothing like Calgary",
    # plans and undisclosed intent
    "我们的旅行计划是下个月去北海道, 已经订好票了",
    "our travel plan is Hokkaido next month, tickets are booked",
    "我们打算明年把公司卖掉, 尽调已经开始了",
    "we plan to sell the company next year, due diligence has started",
    "我们的未发布产品下个月上线, 别外传",
    "our unreleased product ships next month, keep it quiet",
    "我们的客户名单里有三个是军工背景的, 别写进文档",
    "our client list has three defence contractors, keep it out of docs",
    "our supplier quotes 30% below market",
    # identity and role
    "我们在这个系统里是管理员, 权限比谁都大",
    "we are the administrator here, we have the widest permissions",
    "our split is that I handle trading and she handles risk",
    # health
    "我们俩都确诊过焦虑症, 医生说要吃药",
    "we were both diagnosed with anxiety and put on medication",
]

# shareable method notes that mention a user or a team
POSITIVE = [
    "当用户说 hi 的时候要回 pong, 这是 webhook 的标准做法。",
    "用户说找不到订单时, 先查订单表再查支付表。",
    "如果用户要求退款, 按流程走财务审批。",
    "用户偏好深色模式, 记住这个设置。",
    "我们把请求超时设成 3 秒, 端到端延迟从 800ms 降到 120ms。",
    "我们把结果写进 ALPHA_FINDINGS_2026-11-02.md, 便于下个 session 直接读。",
    "our approach to the scanner, verified on the holdout split.",
    "our method notes live in docs/design/ and are versioned with the code.",
    "we lowered the timeout from 800ms to 120ms and the tail latency followed.",
    "我们的目标是找出未来 60 日内爆发且能守住的股票。",
    "我们的做法是先做 holdout 再上线, 顺序不能反。",
    "our rule is: never promote a fact before the backfill runs.",
    "如果我们的分类器把复数人称误判成私人内容, 共享层就会变薄。",
    "our key rotation is automated now, no manual config change.",
    "我们跟上游确认过, 这个字段可以为空。",
    "our holdout shows the scanner beat the pool by 18% per period.",
]

# A known trade-off, measured rather than guessed. "密钥" has to stay in
# the marker list: dropping it releases "our private key lives in the
# same folder as the cert" and "我们的私钥放在 D 盘那个目录里", and a
# leak is worse than a method note that stays private. Dropping it from
# the marker list was measured: two leaks for one false block.
#
# The English spelling is not affected -- "our key rotation" names no
# marker word, so it stays shareable.
KNOWN_BLOCKED = [
    "我们把密钥轮换做成了自动化, 不用手动改配置。",
]


@pytest.mark.parametrize('text', ADVERSARIAL,
                         ids=[f'a{i}' for i in range(len(ADVERSARIAL))])
def test_adversarial_stays_private(text):
    r = classify_visibility(text=text, kind="lesson", hint=None,
                            admin_allow_commons=True)
    assert r['visibility'] == 'personal', (
        f'leaked to the shared tier: {text!r} '
        f'(reason={r["reason"]}, signals={r["signals"]})')


@pytest.mark.parametrize('text', POSITIVE,
                         ids=[f'p{i}' for i in range(len(POSITIVE))])
def test_method_notes_stay_shareable(text):
    r = classify_visibility(text=text, kind="lesson", hint=None,
                            admin_allow_commons=True)
    assert r['visibility'] == 'commons', (
        f'blocked from the shared tier: {text!r} '
        f'(reason={r["reason"]}, signals={r["signals"]})')


@pytest.mark.parametrize('text', KNOWN_BLOCKED,
                         ids=[f'k{i}' for i in range(len(KNOWN_BLOCKED))])
def test_known_trade_off(text):
    """A documented false block: it stays personal on purpose.

    If this ever becomes shareable, the rule got sharper and the case
    belongs in POSITIVE -- move it, don't just relax the assertion.
    """
    r = classify_visibility(text=text, kind="lesson", hint=None,
                            admin_allow_commons=True)
    assert r['visibility'] == 'personal'


# --------------------------------------------------------------------------
# the pieces, so a failure names the mechanism and not just a sentence
# --------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    # "we" was missing from the plural branch, so the marker list was never
    # consulted for an English sentence
    ('we live in Vancouver', True),
    ('we rent an apartment in Vancouver', True),
    ('we lowered the timeout from 800ms to 120ms', False),
    ('our approach to the scanner', False),
    # "I" was missing from the singular branch
    ('our split is that I handle trading', True),
    ('I verified this method', True),
    # but a capital I used as a list marker is not a pronoun
    ('R-class I - astor reembed must use the admin CLI', False),
    ('Part I of the migration', False),
    ('step I: open the database', False),
    ('Ship I live verify on production', False),
    # ... while a capitalised word in front of a real pronoun stays a pronoun
    ('While I understand your question, here is the answer', True),
    ('Would I trust these results if I saw the raw data', True),
], ids=[
    'we + residence -> True',
    'we + rent -> True',
    'we + process -> False',
    'our + process -> False',
    'I as pronoun -> True',
    'I alone -> True',
    'R-class I -> False',
    'Part I -> False',
    'step I -> False',
    'Ship I -> False',
    'While I -> True',
    'Would I -> True',
])
def test_first_person_mechanics(text, expected):
    assert has_first_person(text) is expected


@pytest.mark.parametrize('text', [
    'our API key is ghp_9f2a7c3e1b8d4f6a0c5e2b7d1f9a3c6e',
    'AKIA0123456789ABCDEF',
    'sk-abcdefghijklmnopqrstuvwxyz',
    '-----BEGIN RSA PRIVATE KEY-----',
    'token: abcdef1234567890',
], ids=['ghp', 'AKIA', 'sk', 'pem', 'token='])
def test_credential_value_is_pii(text):
    """A credential is a value, not a topic."""
    assert has_pii(text)


def test_credential_topic_is_not_pii():
    """Talking about key rotation is not leaking a key."""
    assert has_pii('我们把密钥轮换做成了自动化') == []
    assert has_pii('our key rotation is automated now') == []
