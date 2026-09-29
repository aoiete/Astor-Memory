"""test_tier_routing.py — v1.16+ tier routing tests.

Verifies that:
- astor_classify_outcome correctly buckets text into success/failure/lesson/neutral
- _pick_tier() routes failure/lesson to PUBLIC (cross-user sharing), not source
- astor_capture_intent() applies the same PUBLIC routing for failure/lesson

These tests guard the v1.16+ Plan "public tier 共享方法/流程/教训" change. If
a future commit reverts tier routing back to source for failure/lesson, all
6 assertions fail and the public-sharing contract is broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.forge.extractor import (
    astor_capture_intent,
    astor_classify_outcome,
    _pick_tier,
)


def _truthy(label: str, got) -> None:
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _eq(label: str, got, expected) -> None:
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got!r}\n    expected: {expected!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== astor_classify_outcome buckets ===")

    # (1) success keyword in text
    _eq("astor_classify_outcome('成功了 xxx')", astor_classify_outcome("今天搞定了 astor 写入"), "success")

    # (2) failure keyword in text
    _eq("astor_classify_outcome('失败了 xxx')", astor_classify_outcome("astor 召回失败了 重启也没用"), "failure")

    # (3) lesson keyword (error + fix pair)
    _eq("astor_classify_outcome('教训 xxx')", astor_classify_outcome("崩溃了，因为 astor 没嵌向量"), "lesson")

    print("\n=== _pick_tier routes failure/lesson to PUBLIC ===")

    # (4) failure → public (v1.16+: was source)
    _eq("_pick_tier('failure', 0.5)", _pick_tier("failure", 0.5), "public")

    # (5) lesson → public (v1.16+: was source)
    _eq("_pick_tier('lesson', 0.9)", _pick_tier("lesson", 0.9), "public")

    print("\n=== astor_capture_intent applies same PUBLIC routing ===")

    # (6) failure outcome via the bridge → public tier
    res = astor_capture_intent(
        text="操作 moomoo 流程：1. 打开客户端 2. 登录",
        actor="test_tier_routing",
    )
    # success outcome → public (always was)
    _eq("astor_capture_intent(success text) tier", res.get("tier"), "public")

    # Force a failure outcome by hitting failure keywords.
    res_fail = astor_capture_intent(
        text="这次搞砸了，召回返回空，崩溃了 因为 lesson 没自动注入",
        actor="test_tier_routing",
    )
    _eq("astor_capture_intent(failure/lesson text) tier", res_fail.get("tier"), "public")

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())