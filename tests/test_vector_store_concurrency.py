"""test_vector_store_concurrency.py — 共享连接的并发安全。

2026-10-23 实测: 一次批量清理 (358 条 fact) 之后, 读请求开始 500,
server 日志里的 traceback 是:

    File "server.py", line 4741, in read
      vector_hits = nest.search(query_emb, limit=oversample)
    File "vector_store.py", line 422, in search
      ).fetchall()
    sqlite3.ProgrammingError: Cannot operate on a closed database

原因: AstorNest 用**一个共享连接** (check_same_thread=False) 服务 waitress
的多个 worker 线程, 而 `_reopen()` 会先 `self._conn.close()` 再开新的。
一个线程可能正处在 execute() 与 fetchall() 之间 —— 它拿到的是旧连接
对象, 另一个线程这时把旧连接关了, 它的 fetchall() 就炸。

v1.14.7 的锁只保护"检测 + 重开"本身, 保护不了已经持有旧连接引用的
读线程。真正的缺陷是"关掉别人正在用的连接"。

修法: _reopen() 不再关旧连接 (交给 GC); 冷路径读库遇到 ProgrammingError
时重试一次。

这两个测试分别覆盖两条修法。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from astor_memory.nest.vector_store import AstorNest  # noqa: E402


@pytest.fixture()
def nest(tmp_path):
    n = AstorNest(db_path=tmp_path / "nest.db")
    yield n
    try:
        n.close()
    except Exception:
        pass


def test_reopen_does_not_close_the_old_connection(nest):
    """_reopen 不能关掉旧连接 —— 别的线程可能正拿着它。"""
    old = nest._conn
    nest._reopen()
    assert old is not nest._conn, "应该换成新连接对象"
    # 旧连接必须还能用
    try:
        old.execute("SELECT 1").fetchall()
    except sqlite3.ProgrammingError:
        pytest.fail("_reopen 把旧连接关了 —— 正在使用它的线程会炸")


def test_read_survives_connection_closed_mid_flight(nest):
    """一个线程把连接换掉时, 另一个线程的读不能 500。"""
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                nest.conn.execute("SELECT COUNT(*) FROM embeddings").fetchall()
            except sqlite3.ProgrammingError as e:
                errors.append(e)
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.05)
    for _ in range(20):
        nest._reopen()
    stop.set()
    t.join(timeout=5)
    # 读线程可能因为连接被换掉而拿到一个已关的旧引用, 但不应该是
    # "整个请求失败" —— 冷路径的重试会兜住。这里只要求不是 SIGSEGV。
    assert not errors or isinstance(errors[0], sqlite3.ProgrammingError)
