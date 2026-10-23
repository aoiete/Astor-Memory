"""test_shared_handles.py — 共享的 bus/nest 句柄不能在请求结束时被关掉。

2026-10-23 实测: 并发读 120 次, 94 次失败, 客户端看到 WinError 10054。
链条是:

    请求 A 结束 -> teardown_request -> nest.close()
      -> 关掉进程级 singleton 的 SQLite 连接 (请求 B 正在用)
      -> B 的查询炸 "Cannot operate on a closed database"
      -> B 500 -> 看门狗读健康端点拿到非 200 -> 判定"不健康"
      -> 看门狗杀掉健康的服务 -> 客户端看到 10054/10061

astor_bus() / astor_nest() 早就是 per (tier, user, path) 的 singleton,
所以 teardown 关的不是"这个请求自己的连接", 而是**整个进程共享的那一个**。
v1.13.2 加这个 hook 是为了防"每请求一个 WAL 写连接"—— 那在工厂不缓存
实例的年代成立; 工厂改成 singleton 之后, 关它只剩坏处。

这两个测试锁住修法: teardown 只关非共享句柄。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SRC = os.path.join(os.path.dirname(__file__), '..', 'astor_memory', 'server.py')


def _teardown_source():
    with open(SRC, encoding='utf-8') as f:
        return f.read()


def test_teardown_does_not_close_shared_handles():
    src = _teardown_source()
    assert "_shared" in src, "teardown 必须区分共享句柄"
    # 两个循环都要有共享判断
    assert src.count("getattr(bus, '_shared', False)") >= 1
    assert src.count("getattr(nest, '_shared', False)") >= 1


def test_bus_and_nest_mark_shared_instances():
    for rel, needle in (
        (os.path.join('astor_memory', 'bus', 'store.py'), "inst._shared = True"),
        (os.path.join('astor_memory', 'nest', 'vector_store.py'), "inst._shared = True"),
    ):
        path = os.path.join(os.path.dirname(__file__), '..', rel)
        with open(path, encoding='utf-8') as f:
            src = f.read()
        assert needle in src, f"{rel} 必须在返回 singleton 时标记 _shared"
        assert "self._shared = False" in src, f"{rel} 必须有默认 _shared = False"


def test_shared_flag_is_set_on_the_cached_path_too():
    """缓存命中时也要标记 —— 否则第二次请求拿到的实例没标记, 仍会被关。"""
    path = os.path.join(os.path.dirname(__file__), '..',
                        'astor_memory', 'nest', 'vector_store.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    # astor_nest() 里, 无论新建还是命中缓存, 都要在 return 之前标记
    start = src.find("def astor_nest(")
    end = src.find("def astor_nest_for(", start)
    assert start != -1 and end != -1, "找不到 astor_nest()"
    body = src[start:end]
    assert "inst._shared = True" in body, \
        "astor_nest() 必须标记 _shared (新建和命中缓存都要)"


def test_nest_reopen_does_not_close_old_connection():
    """_reopen 不能关掉旧连接 —— 别的线程可能正拿着它。"""
    path = os.path.join(os.path.dirname(__file__), '..',
                        'astor_memory', 'nest', 'vector_store.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r"def _reopen\(.*?\n(?:.*\n)*?\n    def ", src)
    assert m, "找不到 _reopen()"
    body = m.group(0)
    assert "self._conn.close()" not in body, \
        "_reopen 不应该关旧连接 (会打断正在使用它的线程)"
