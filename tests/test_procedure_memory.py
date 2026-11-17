"""v1.17.38: Procedure Memory + machine-noise recognition.

The regression that matters most here is the negative one: the noise
predicate must not take a real lesson with it.  A wide prefix match
would.
"""

import pytest

from astor_memory._internal import noise as _noise
from astor_memory._internal import procedure as _proc


# --- procedure shape ---------------------------------------------------

def test_split_parts_finds_all_four_sections():
    text = (
        "hermes update 升级失败 process tree is still alive\n"
        "分析路径: sc queryex 拿服务主进程 PID; 读 registry 看 AppKillProcessTree\n"
        "操作: 设 AppKillProcessTree=1\n"
        "验证: sc queryex 显示 STOPPED 且无残留 pythonw\n"
    )
    parts = _proc.split_parts(text)
    assert 'process tree is still alive' in parts['trigger']
    assert 'AppKillProcessTree' in parts['analysis']
    assert 'AppKillProcessTree=1' in parts['action']
    assert 'STOPPED' in parts['verify']


def test_split_parts_does_not_repeat_the_marker():
    text = "触发条件\n分析路径: 先看日志\n操作: 改配置\n验证: 看指标"
    parts = _proc.split_parts(text)
    assert not parts['analysis'].startswith('分析路径')
    assert _proc.build_content(parts).count('分析路径') == 1


def test_build_content_leads_with_the_trigger():
    parts = {'trigger': 'dashboard 登录不上', 'analysis': 'a', 'action': 'b', 'verify': 'c'}
    content = _proc.build_content(parts)
    assert content.startswith('[PROCEDURE] dashboard 登录不上')
    assert '分析路径: a' in content
    assert '操作: b' in content
    assert '验证: c' in content


def test_build_content_omits_absent_sections():
    content = _proc.build_content({'trigger': 'x', 'analysis': '', 'action': '', 'verify': ''})
    assert content == '[PROCEDURE] x'


def test_split_parts_keeps_unplaceable_text():
    parts = _proc.split_parts('just a line with no markers at all')
    assert parts['trigger'].startswith('just a line')


def test_is_procedure():
    assert _proc.is_procedure('[PROCEDURE] x')
    assert not _proc.is_procedure('a normal lesson')


# --- kind mapping ------------------------------------------------------

def test_procedure_kind_maps_to_experience():
    from astor_memory.bus.store import _derive_memory_class
    assert _derive_memory_class('procedure', None, None) == 'experience'
    assert _derive_memory_class('experience', None, None) == 'experience'


# --- noise predicate: the negatives are the point ----------------------

@pytest.mark.parametrize('content,kind', [
    # a genuine lesson that quotes a command token
    ("[LESSON] LESSON [2026-10-07, shipped] astor-bus-healthcheck 重启窗口期误报修复",
     'lesson'),
    ("[LESSON] R-class (2026-11-13, 补完 hermes update 失败根因): NSSM 的 AppKillProcessTree 未设置",
     'lesson'),
    ("[LESSON] 千万要避免 C盘backup 脚本爆炸导致 baseline 被杀", 'lesson'),
    # a real lesson about a command, phrased as prose
    ("[LESSON] 发送微信消息的正确路径是 scripts/send_to_platform.py weixin --to <chat_id>",
     'lesson'),
    # a maker_pocket rule (not a snapshot)
    ("maker_pocket 的 state machine 是 IDLE->ARMED->ENTERED->CLOSE->IDLE", 'lesson'),
])
def test_real_lessons_are_not_noise(content, kind):
    matched = _matches(content, kind)
    assert not matched, f'false positive: {content[:60]}'


@pytest.mark.parametrize('content,kind', [
    ("maker_pocket {\"state\": \"ENTERED\", \"entry_price\": 80000}", 'event'),
    ('maker_pocket X: {"state": "ARMED"}', 'fact'),
    ('middleware-warn-test: 42 rows', 'lesson'),
    ('ZZBUGPROBE ok', 'lesson'),
    ('[LESSON] hermes cron list 2>&1 | grep -B5 "error" | head -80', 'lesson'),
    ('[LESSON] tail -10 "C:/logs/x.log" 2>&1 | tail -8', 'lesson'),
])
def test_machine_noise_is_noise(content, kind):
    assert _matches(content, kind), f'false negative: {content[:60]}'


def _matches(content: str, kind: str) -> bool:
    """Run the real predicate against real SQLite."""
    import sqlite3
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE memory_canonical (kind TEXT, content TEXT)')
    con.execute('INSERT INTO memory_canonical VALUES (?,?)', (kind, content))
    row = con.execute(
        'SELECT COUNT(*) FROM memory_canonical WHERE (' + _noise.noise_sql() + ')'
    ).fetchone()
    con.close()
    return bool(row[0])


def test_noise_sql_is_balanced():
    sql = _noise.noise_sql()
    assert sql.count('(') == sql.count(')')
    assert 'session-hygiene' not in sql, \
        'session-hygiene ticks carry real conclusions; they are not noise'


def test_label_of():
    assert 'snapshot' in _noise.label_of('maker_pocket {"a":1}', 'event')
    assert 'test' in _noise.label_of('middleware-warn-test x', 'lesson')
    assert 'command' in _noise.label_of('[LESSON] grep -r foo', 'lesson')
