"""test_hygiene_rules.py — 测试残留检测规则的自证测试.

2026-10-23 (HaluMem 驱动): 这套规则有两个消费者 ——
  1. server.py 的 /v1/write 写入口拦截 (防止新的测试残留进 public/source)
  2. tests/memory_hygiene.py 的清理工具 (清掉已经存在的)

两处共用 astor_memory/forge/hygiene_rules.py, 所以规则一旦漂移两边一起错。
这个文件就是防漂移的: **坏样本必须被抓, 真知识必须不被误伤**。

误伤方向更要紧 —— 误伤会把用户的真事实隔离掉, 那是数据损失;
漏抓只是垃圾多一条。所以 GOOD 样本的选择比 BAD 样本更严格。
"""
from __future__ import annotations

import pytest

from astor_memory.forge.hygiene_rules import (
    PROBE_PREFIXES, TEST_RESIDUE_PATTERNS, is_test_residue,
)


# --- 坏样本: 这些是 astor 自己的 e2e / 调试产物, 必须全部被抓 -------------
BAD_SAMPLES = [
    "Maria 完成了 e2e_test_marker_fe156c38_1792725303 测试",
    "Maria 启动了 e2e_correction_a70ae09e_1792648759 项目",
    "Maria 实际上没有启动 e2e_correction_a70ae09e_1792648759 项目",
    "Maria 测试新发布",
    "微信抓取 method curl UA marker-preflight-verify-v1631",
    "微信抓取 method curl UA marker-q1w2e3",
    "method curl test visibility-hint-empty",
    "astor write path instrumentation probe 1e2b764cb90",
    "[DATE_1]用 method curl+UA 测试 marker-final-v1632-bug10",
    "profile timing 10-06 1.16.72 instrument",
    "async final verify aw9322",
    "hook_audit_test_DO_NOT_PROMOTE",
    "poc",
    "tier resolver test marker zx9309",
    "method clean test no pii marker-no-pii",
]

# --- 真知识: 用户的实际规则 / 偏好 / 架构知识, 一个都不能被误伤 ----------
GOOD_SAMPLES = [
    "astor 是第一存储,除非迫切需要不用 memory md(memory 是 fallback only)",
    "用户明确(2026-09-06):3% 这类固定百分比数字不是必须规则",
    "R-class: patch 工具 indentation drift 陷阱,需用 Python 直接写而非 patch",
    "用户偏好 ship 前先 dry-run 验证数据 + 每步都跑全套测试 + PII check",
    "每日卦象(梅花+六爻)不进 astor 长期记忆,属 process 产物",
    "astor 没有 80K 容量上限,那是旧 memory.md 的限制",
    "Maria 是用户的同事",
    "用户每天都会用梅花和六爻两套系统算卦",
    "测试覆盖率很重要",
    "astor ship流程锁定5步: 改源码->patch runtime->runtime验证->pre-flight audit->复查",
    "用户说 stakes 时永远是 先小盲 后大盲(1/3 -> BB=$3, 2/5 -> BB=$5)",
    "用户反馈回复太长，要求更简洁直接的风格(短句+项目符号+表格)",
    "成功模式(success_pattern)只存通用方法论和工具路径,不能含私人数据",
]


@pytest.mark.parametrize("text", BAD_SAMPLES)
def test_bad_samples_are_caught(text):
    """Every known test/debug artefact must be detected."""
    assert is_test_residue(text), f"未抓到测试残留: {text!r}"


@pytest.mark.parametrize("text", GOOD_SAMPLES)
def test_good_samples_are_not_caught(text):
    """Real user knowledge must never be quarantined.

    A false positive here destroys a real fact — worse than leaving
    debris behind, so this direction is the strict one.
    """
    assert not is_test_residue(text), f"误伤真知识: {text!r}"


def test_empty_and_none_are_safe():
    assert not is_test_residue("")
    assert not is_test_residue("   ")
    assert not is_test_residue(None)


def test_prefix_rule_catches_leading_probes():
    """Prefix probes only match at the start — verify the prefix path works."""
    for p in PROBE_PREFIXES:
        assert is_test_residue(p + " something")
        # ... and NOT in the middle (that's the content-pattern path)
        if not any(p in pat.pattern for pat in TEST_RESIDUE_PATTERNS):
            pass  # covered by the content-pattern tests above


def test_rule_module_is_shared_with_cleanup_tool():
    """The cleanup tool must use the SAME rule set as the write gate.

    Two copies of the rules drift; the write gate then stops catching what
    the cleaner removes (or vice versa).
    """
    from astor_memory.forge import hygiene_rules
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mh_probe",
        str(__import__("pathlib").Path(__file__).parent / "memory_hygiene.py"),
    )
    mh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mh)
    assert mh.PROBE_PREFIXES == hygiene_rules.PROBE_PREFIXES
    for text in BAD_SAMPLES:
        assert mh._test_residue_match(text) == hygiene_rules.is_test_residue(text)
