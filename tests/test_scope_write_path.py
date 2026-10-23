"""test_scope_write_path.py — v1.16.92 端到端: 写入路径真的按证据分级。

只测 server 的 /v1/write -> memory_canonical.scope_type 这条链。
隔离的 ASTOR_DIR (conftest 强制), 不碰生产库。

为什么要有这个: classify_scope 的单元测试只能证明"分类函数本身对",
证明不了"写入路径真的调用了它"。中间任何一环没接上, 单元测试照样全绿
—— 这正是 astor 之前 scope_type 100% long_term 而没有任何测试发现的原因。
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope="module")
def app_client():
    """起一个隔离的 server 实例。"""
    from astor_memory.server import create_app
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _find_facts(db_dir: Path, needle: str):
    """在隔离目录里找出所有含 needle 的 fact。"""
    out = []
    for db in db_dir.rglob("*.db"):
        try:
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id, content, kind, scope_type, expires_at, importance "
                "FROM memory_canonical WHERE content LIKE ?",
                (f"%{needle}%",)).fetchall()
            c.close()
            for r in rows:
                out.append((db, dict(r)))
        except sqlite3.Error:
            continue
    return out


def _write(client, text, kind="fact", importance=0.5, tier="public",
           user="admin", **extra):
    # 注意: /v1/write **忽略** body['facts'], 一律从 text 抽取;
    # 调用方的 kind 是 body 顶层的参数 (server.py:3114 明确 "an explicit
    # caller kind wins")。importance 同理是顶层的。
    body = {
        'text': text,
        'kind': kind,
        'importance': importance,
        'confidence': 0.6,
        'tier': tier,
        'user_id': user,
    }
    body.update(extra)
    return client.post('/v1/write', json=body)


def test_unverified_fact_lands_in_short_term(app_client, tmp_path):
    """neutral 的 fact (importance 0.5) 应该落 short_term 并带 expires_at。"""
    marker = "scope_probe_unverified_9f3a"
    r = _write(app_client, marker, kind="fact", importance=0.5)
    assert r.status_code in (200, 201), f"写入失败: {r.status_code} {r.get_data(as_text=True)[:300]}"
    rows = _find_facts(Path(os.environ['ASTOR_DIR']), marker)
    assert rows, "找不到刚写入的 fact"
    db, row = rows[0]
    assert row['scope_type'] == 'short_term', \
        f"未验证的观察应落 short_term, 实际 {row['scope_type']}"
    assert row['expires_at'], "short_term 必须带 expires_at"


def test_lesson_lands_in_long_term(app_client):
    """kind=lesson 是提炼过的类别, 应落 long_term 且无 expires_at。"""
    marker = "scope_probe_lesson_7c21"
    r = _write(app_client, marker, kind="lesson", importance=0.5)
    assert r.status_code in (200, 201)
    rows = _find_facts(Path(os.environ['ASTOR_DIR']), marker)
    assert rows
    db, row = rows[0]
    assert row['scope_type'] == 'long_term', \
        f"lesson 应落 long_term, 实际 {row['scope_type']}"
    assert not row['expires_at'], "long_term 不应有 expires_at"


def test_high_importance_lands_in_long_term(app_client):
    """importance >= 0.7 是抽取器"不是 neutral"的判决, 应落 long_term。"""
    marker = "scope_probe_importance_4b88"
    r = _write(app_client, marker, kind="fact", importance=0.85)
    assert r.status_code in (200, 201)
    rows = _find_facts(Path(os.environ['ASTOR_DIR']), marker)
    assert rows
    db, row = rows[0]
    assert row['scope_type'] == 'long_term', \
        f"importance 0.85 应落 long_term, 实际 {row['scope_type']}"


def test_caller_can_force_scope(app_client):
    """调用方显式传 scope 时必须被尊重, 不能被策略覆盖。"""
    marker = "scope_probe_explicit_1a55"
    r = _write(app_client, marker, kind="fact", importance=0.5, scope="long_term")
    assert r.status_code in (200, 201)
    rows = _find_facts(Path(os.environ['ASTOR_DIR']), marker)
    assert rows
    db, row = rows[0]
    assert row['scope_type'] == 'long_term'
