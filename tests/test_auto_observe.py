"""Phase E tests for ``astor_auto_observe`` server-side auto-memory hook."""

from __future__ import annotations

from astor_memory.forge.extractor import (
    astor_auto_observe,
    astor_should_skip_auto_observe,
)


def test_short_text_skipped():
    res = astor_auto_observe("hi there", agent_id="astor_memory_mcp", user_id="admin")
    assert res["observed"] is False
    assert res["skipped_reason"] == "too_short"


def test_empty_text_skipped():
    res = astor_auto_observe("   ", agent_id="astor_memory_mcp", user_id="admin")
    assert res["observed"] is False
    assert res["skipped_reason"] == "empty"


def test_greeting_prefix_skipped():
    res = astor_auto_observe(
        "你好世界，打牌运势极佳，今天给我大吉，但是其他事务需要格外注意，避开黄颜色色",
        agent_id="astor_memory_mcp",
        user_id="admin",
    )
    assert res["observed"] is False
    assert res["skipped_reason"] == "noise_prefix"


def test_remember_intent_observed_public():
    res = astor_auto_observe(
        "remember this: user prefers dark mode for terminal sessions",
        agent_id="astor_memory_mcp",
        user_id="admin",
    )
    assert res["observed"] is True
    assert res["tier"] == "public"
    assert res["outcome"] in ("success", "neutral")
    assert res["importance"] >= 0.5


def test_error_report_observed_source():
    res = astor_auto_observe(
        "搞砸了，astor_memory /v1/read 在空 query 时崩溃了",
        agent_id="astor_memory_mcp",
        user_id="admin",
    )
    assert res["observed"] is True
    assert res["tier"] == "source"
    assert res["outcome"] == "failure"
    assert res["importance"] >= 0.7


def test_pure_question_skipped():
    res = astor_auto_observe(
        "ask: what is the meaning of life?",
        agent_id="astor_memory_mcp",
        user_id="admin",
    )
    assert res["observed"] is False
    assert res["skipped_reason"] in ("low_signal", "noise_prefix")


def test_long_preference_observed_public():
    res = astor_auto_observe(
        "用户在 2026-09-15 多次表达：深度工作时段不要 IM 弹窗；邮件每 2 小时看一次即可；电话只接 starred；会议限定 30 分钟。",
        agent_id="astor_memory_mcp",
        user_id="admin",
    )
    assert res["observed"] is True
    assert res["tier"] == "public"


def test_disabled_agent_skipped():
    res = astor_auto_observe(
        "remember this for later",
        agent_id="",
        user_id="admin",
    )
    assert res["observed"] is False
    assert res["skipped_reason"] == "no_agent_id"


def test_unknown_agent_skipped():
    res = astor_auto_observe(
        "remember this for later",
        agent_id=None,
        user_id="admin",
    )
    assert res["observed"] is False
    assert res["skipped_reason"] == "no_agent_id"


def test_should_skip_helper():
    assert astor_should_skip_auto_observe("") == (True, "empty")
    assert astor_should_skip_auto_observe("   ") == (True, "empty")
    assert astor_should_skip_auto_observe("hi")[0] is True
    assert astor_should_skip_auto_observe("嗯")[0] is True
    assert astor_should_skip_auto_observe("Greetings team, please review my new feature flag rollout plan.")[0] is False
    assert astor_should_skip_auto_observe(
        "今天下午我跟 EvoX 一起调试了 astor_memory 的 auto_observe hook，"
        "调试过程发现 noise filter 需要识别中文问候词比如你好，"
        "整体流程已经跑通"
    )[0] is False


def test_build_recall_query_strips_noise_prefix():
    from astor_memory.forge.extractor import build_recall_query
    assert build_recall_query("") == ""
    assert build_recall_query("   ") == ""
    # Noise prefixes are stripped
    assert not build_recall_query("嗯 我喜欢打牌").startswith("嗯")
    assert not build_recall_query("ok 这个怎么搞").startswith("ok")
    assert not build_recall_query("hi 今晚有空吗").startswith("hi")
    # Normal text passes through
    q = build_recall_query("你今晚打牌运势怎么样")
    assert q.startswith("你今晚打牌运势")
    # Long text clamped at max_chars
    long_text = "a " * 500
    q = build_recall_query(long_text, max_chars=100)
    assert len(q) <= 100
    assert not q.endswith(" ")  # trimmed on word boundary


def test_format_recall_block_includes_content():
    from astor_memory.forge.extractor import format_recall_as_system_prompt_block
    results = [
        {"content": "用户偏好 dark mode", "confidence": 0.85, "kind": "fact", "importance": 0.7},
        {"content": "上次运势: 木", "confidence": 0.72, "kind": "fact", "importance": 0.6},
    ]
    block = format_recall_as_system_prompt_block(results)
    assert "dark mode" in block
    assert "上次运势" in block
    assert "conf=0.85" in block
    assert "kind=fact" in block
    assert "Astor" in block  # pre-block label


def test_format_recall_block_handles_empty():
    from astor_memory.forge.extractor import format_recall_as_system_prompt_block
    assert format_recall_as_system_prompt_block([]) == ""
    # Skips items with empty content
    results = [{"content": "", "confidence": 0.5}, {"content": None, "confidence": 0.5}]
    assert format_recall_as_system_prompt_block(results) == ""
    # max_items caps output
    results = [{"content": f"fact {i}"} for i in range(20)]
    block = format_recall_as_system_prompt_block(results, max_items=3)
    assert block.count("\n") >= 3  # header + 3 items
    assert "fact 19" not in block  # beyond max_items
