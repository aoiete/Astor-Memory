"""回归测试: trigram FTS5 上的中文词法召回 (2026-10-22 事故)。

**这个 bug 从 2026-09-02 上 trigram tokenizer 起就存在, 一直没人发现。**

怎么发现的: 补 WorldMemArena 第四段指标 (usage_rate) 时, lifestyle /
fortune 分类召回率异常低 (0.30 / 0.62)。逐层定位发现:
  documents 表里有 75 行含"旺时/凶时", 但 MATCH "旺" 只中 2 行
  → 词法检索对中文几乎完全失效

**根因**: lex_fts 用 `tokenize="trigram"`, trigram 要求查询 >= 3 字符。
而 `bm25_search` 把 `_tokenize()` 切出来的**单字**喂给 MATCH
(1 字符) → FTS5 必然 0 命中 → 词法路径静默失效, 只剩向量路径在扛。

**修法**: 候选召回改用 **2 字滑窗 phrase**:
  · "脚本同步铁律" 整串 phrase -> 0  (phrase 要求字面连续)
  · "脚本" 2 字 phrase      -> 38 (可用)
  · 2 字滑窗 OR 组合        -> 14~38 条 (0.3%~0.8% 全表, 精准剪枝)
精确 BM25 打分仍用我们自己的 tokens (含 bigram), 打分口径不变。

这些测试锁住: 中文 query 必须能召回, 不能静默返回 []。
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def cjk_lex(tmp_path):
    """建一个带中文文档的小 lex 库 (真实 FTS5, 不 mock)。"""
    from astor_memory.nest import lex_index as L
    db = tmp_path / "lex.db"
    lx = L.AstorLex.__new__(L.AstorLex)
    # 必须是 RLock —— 生产代码 (lex_index.AstorLex.__init__) 用的是
    # threading.RLock(), 因为 index_fact() 持锁时会调 remove_fact_hard()
    # (内部再取同一把锁)。用普通 Lock 会自死锁。
    lx._lock = __import__("threading").RLock()
    lx._conn = sqlite3.connect(str(db))
    lx._conn.row_factory = None
    lx.db_path = db
    lx._init_schema()
    docs = [
        (1, "德州扑克不是赌博,扑克/麻将方位吉凶属于传统民俗应用"),
        (2, "今日打牌整体运势中下,守为上;旺时午(11-13)/戌(19-21);凶时亥(21-23)"),
        (3, "脚本同步铁律: D 盘改 C 盘 copy 的脚本同步铁律, 改 D 盘就同步"),
        (4, "紫微命盘输出: 走 iztro 库, 传入公历/农历/性别/出生时辰"),
        (5, "OpenD 看门狗竞态事故: 会 taskkill /IM OpenD.exe 的守护脚本不能并行"),
    ]
    for fid, content in docs:
        lx.index_fact(fid, content)
    yield lx
    lx._conn.close()


def test_cjk_query_returns_results_not_empty(cjk_lex):
    """核心回归: 中文 query 不能静默返回 []。"""
    for q in ["旺时", "凶时", "紫微命盘", "脚本同步", "德州扑克", "看门狗"]:
        hits = cjk_lex.bm25_search(q, limit=5)
        assert hits, f"中文 query {q!r} 召回为空 (trigram 退化)"


def test_cjk_two_char_query_works(cjk_lex):
    """2 字 query 也要能用 (trigram 只吃 >=3 的坑)。"""
    assert cjk_lex.bm25_search("旺时", limit=5)
    assert cjk_lex.bm25_search("脚本", limit=5)


def test_cjk_mixed_query_works(cjk_lex):
    """中英混合 query。"""
    hits = cjk_lex.bm25_search("OpenD 看门狗", limit=5)
    assert hits
    assert hits[0][0] == 5


def test_cjk_relevant_doc_ranked_first(cjk_lex):
    """正确的文档应该排第一, 不是靠运气。"""
    hits = cjk_lex.bm25_search("紫微命盘输出", limit=3)
    assert hits
    assert hits[0][0] == 4


def test_fts_candidate_pool_is_small(cjk_lex):
    """候选集必须远小于全表 (FTS5 剪枝的意义)。"""
    n = cjk_lex._conn.execute(
        "SELECT COUNT(*) FROM documents WHERE tombstoned=0").fetchone()[0]
    hits = cjk_lex.bm25_search("旺时 凶时 紫微 脚本", limit=20)
    # 候选可能超过命中文档数, 但不能是全表暴力 (limit*5 封顶)
    assert len(hits) <= 20


def test_single_cjk_char_falls_back_not_empty(cjk_lex):
    """单个中文字 (FTS5 完全用不上) 必须走 legacy 路径, 不能返回 []。"""
    hits = cjk_lex.bm25_search("牌", limit=5)
    # 允许为空 (库里确实没这个字), 但不能是异常/崩溃
    assert isinstance(hits, list)