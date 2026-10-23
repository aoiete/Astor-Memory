"""usage_rate 打分逻辑单测 — 不打服务器, 纯函数验证。

关键: 覆盖 WorldMemArena 点名的失败形态 ——
  · 召回到了但排位靠后 (found but not cited)
  · 召回到了但已作废   (found but not usable: invalidated)
  · 召回到了但内容太短 (found but not usable: too_short)
  · 召回到了且可用     (完整链路)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_rate import MIN_USABLE_CHARS, score_one


def _r(content, **kw):
    d = {"content": content}
    d.update(kw)
    return d


def test_found_and_usable():
    """证据在 top-2, 有内容, 未作废 -> fully usable."""
    s = score_one("q", [_r("无关内容占位填充一些长度"), _r("相关性闸门 corr_max 0.85 防隐性加杠杆")],
                  ["corr_max"], top_k=10, cite_k=5)
    assert s["found"] and s["cited"] and s["usable"] and s["actionable"]
    assert s["found_rank"] == 2
    assert s["usable_reason"] == "ok"


def test_found_but_not_cited():
    """证据在第 8 位, cite_k=5 -> 召回到了但下游看不到 (论文说的核心缺口)."""
    results = [_r(f"无关填充内容条目 number {i}") for i in range(7)]
    results.append(_r("相关性闸门 corr_max 0.85 防隐性加杠杆"))
    s = score_one("q", results, ["corr_max"], top_k=10, cite_k=5)
    assert s["found"] and s["found_rank"] == 8
    assert not s["cited"], "排在 cite_k 之外就不该算 cited"
    assert s["usable"], "usable 不依赖 cited — 它问的是证据本身能不能用"


def test_found_but_invalidated():
    """证据已作废 (valid_until 已过) -> 召回到了但用不上."""
    results = [_r("相关性闸门 corr_max 0.85 防隐性加杠杆", valid_until="2020-01-01T00:00:00+00:00")]
    s = score_one("q", results, ["corr_max"], top_k=10, cite_k=5)
    assert s["found"]
    assert not s["usable"]
    assert s["usable_reason"] == "invalidated"


def test_valid_until_future_still_usable():
    """valid_until 在未来 -> 仍然可用."""
    results = [_r("相关性闸门 corr_max 0.85 防隐性加杠杆",
                  valid_until="2099-01-01T00:00:00+00:00")]
    s = score_one("q", results, ["corr_max"], top_k=10, cite_k=5)
    assert s["found"] and s["usable"]
    assert s["usable_reason"] == "ok"


def test_found_but_too_short():
    """证据内容太短 -> 召回到了但撑不起一次决策."""
    results = [_r("corr_max")]
    s = score_one("q", results, ["corr_max"], top_k=10, cite_k=5)
    assert s["found"]
    assert not s["usable"]
    assert s["usable_reason"] == "too_short"


def test_tombstoned_not_usable():
    """tombstoned 的证据不能用."""
    results = [_r("相关性闸门 corr_max 0.85 防隐性加杠杆", tombstoned=1)]
    s = score_one("q", results, ["corr_max"], top_k=10, cite_k=5)
    assert s["found"] and not s["usable"]
    assert s["usable_reason"] == "tombstoned"


def test_not_found():
    s = score_one("q", [_r("完全无关的内容")], ["corr_max"], top_k=10, cite_k=5)
    assert not s["found"] and not s["cited"] and not s["usable"]
    assert s["usable_reason"] == "not_found"


def test_empty_results():
    s = score_one("q", [], ["corr_max"], top_k=10, cite_k=5)
    assert not s["found"]
    assert s["found_rank"] is None


def test_min_usable_chars_boundary():
    """刚好达到 MIN_USABLE_CHARS 应该算 usable."""
    kw = "corr_max"
    content = kw + "x" * (MIN_USABLE_CHARS - len(kw))
    assert len(content) == MIN_USABLE_CHARS
    s = score_one("q", [_r(content)], [kw], top_k=10, cite_k=5)
    assert s["found"] and s["usable"]


def test_no_expected_keywords():
    """没有期望关键词时不崩, 也不该算 found."""
    s = score_one("q", [_r("任意内容")], [], top_k=10, cite_k=5)
    assert not s["found"]
    assert s["usable_reason"] == "not_found"