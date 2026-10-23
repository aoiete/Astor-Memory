"""hygiene_rules.py — 内容质量规则 (生产 + 清理工具共用).

**为什么要有这个模块 (2026-10-23)**

HaluMem (arXiv 2511.03506, EMNLP 2026) 提出了 FMR (False Memory
Resistance, 抗虚构记忆率) 这个指标 —— 专门测"AI 提了但用户没确认的内容
有没有被存成事实"。拿这个口径实测 astor:

    public 层 kind='fact' 719 条里 409 条 (56.9%) 是 e2e 测试残留 /
    marker 探针 / 模板变体, 而且 access_count 高达 300-500, 正在主动
    污染召回。典型: "Maria 完成了 e2e_test_marker_xxx 测试"、
    **"Maria 确认了发布日期"** (一个虚构用户的确认被存成了事实)。

根因不是"写错了", 是**测试写入没有隔离** —— e2e 探针直接打生产库的
public 层, 于是垃圾一路堆到召回结果里。

**两条防线**:
1. `tests/memory_hygiene.py` 用这里的规则做**清理** (已存在的 debris)
2. `server.py` 的 /v1/write 用这里的规则做**拦截** (防新的进来)

规则写成内容模式而不是前缀, 因为真实形态是
"Maria 启动了 e2e_correction_xxx_1790957879 项目" —— 探针在**中间**,
startswith 抓不到。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------
# 已知测试探针 / 调试残渣的前缀 (内容模式抓不到这些)
# ---------------------------------------------------------------------
# 这些是 astor 自己的测试和调试写进知识库的, 永远不该出现在任何 tier 里。
# 2026-10-23: 从 tests/memory_hygiene.py 搬到这里, 因为写入口拦截
# (server.py /v1/write) 也必须认得它们 —— 只在清理工具里认得的话,
# "instrumentation probe" 这类前缀型残渣还是会继续进 public 层。
PROBE_PREFIXES = (
    "astor write path instrumentation probe",
    "e2e_test_marker",
    "e2e_correction",
    "auto fork-",
    "middleware-warn-test",
    "LEAKPROBE",
    "VERIFY1",
    "demo probe from AstorClient",
    "profile debug print",
    "profile loop sub-stages",
    "post-restart tunnel test",
    "apple banana cherry",
)


# ---------------------------------------------------------------------
# 测试残留 / 调试探针 (内容模式)
# ---------------------------------------------------------------------
TEST_RESIDUE_PATTERNS = (
    # e2e 测试轮次: "Maria 启动了 e2e_correction_<id>_<ts> 项目"
    re.compile(r"e2e_correction_\w+_\d+"),
    re.compile(r"e2e_test_marker_\w+"),
    # 通用测试/调试 marker
    re.compile(r"\bmarker[-\s]?(test|fact|distill|publish|visibility)", re.I),
    # "marker-<任意串>" —— 覆盖 marker-final / marker-q1w2e3 / marker-methodtest
    re.compile(r"\bmarker[-\s][a-z0-9]", re.I),
    # astor e2e 场景里虚构的人名 "Maria" —— 只匹配 e2e 叙述, 不能匹配
    # 真实提到 Maria 的条目 (例如 "Maria 是用户的同事")。
    re.compile(r"^Maria\b.*(测试|test|项目|启动|完成|marker|e2e|新发布|发布)", re.I),
    re.compile(r"\bmethod curl (test|UA)", re.I),
    re.compile(r"^(async test|profile test|poc|curl test|debug_timing)\b", re.I),
    re.compile(r"^profile (timing|debug)\b", re.I),
    re.compile(r"^async (final )?verify\b", re.I),
    re.compile(r"\bhook_audit_test_", re.I),
    # "她/他 启动了 ... 项目" —— e2e 场景脚本的叙述残留
    re.compile(r"^她?他?们?实际上?没有?启动[了]?\s*e2e_", re.I),
    re.compile(r"^她?他?们?完成了\s+e2e_", re.I),
    # 时间戳型一次性事实: "..._<10位时间戳>" 结尾且带测试动词
    re.compile(r"\b(test|probe|debug|mock)\b.*\b\d{10}\b", re.I),
)


def is_test_residue(text: str) -> bool:
    """True if the text looks like a test/debug artefact rather than knowledge.

    Checks BOTH the known probe prefixes and the content patterns. The
    prefix list matters because "astor write path instrumentation probe
    <hash>" only matches at the start of the string, while the content
    patterns catch residues with the marker in the middle ("Maria 启动了
    e2e_correction_xxx_1790957879 项目").

    Kept deliberately conservative: a false positive would quarantine a real
    user fact, which is worse than leaving some debris behind. The pattern
    set is verified by a self-test (bad samples must be caught, good
    samples must not be) in tests/test_hygiene_rules.py.
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(t.startswith(p) for p in PROBE_PREFIXES):
        return True
    return any(p.search(t) for p in TEST_RESIDUE_PATTERNS)


__all__ = ["PROBE_PREFIXES", "TEST_RESIDUE_PATTERNS", "is_test_residue"]
