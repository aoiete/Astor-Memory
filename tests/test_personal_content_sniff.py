"""test_personal_content_sniff.py — v1.16.x Layer 2 personal content sniff tests.

Verifies that detect_personal_content correctly categorizes non-PII personal
content patterns that the 45-pattern PII scanner misses. Layer 2 is warn-only
(never blocks writes); tests cover categorization accuracy on representative
samples covering each of the 4 categories: name_chinese / location /
relationship / financial.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.memory_defense import detect_personal_content


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
    print("=== Layer 2 personal content sniff (v1.16.x) ===")

    # (1) name_chinese: 2-4 char Chinese name + event verb
    _truthy("(1) name_chinese detected", "name_chinese" in detect_personal_content("今天张三家发生 X"))

    # (2) location: "我在 / 我去 / 来自" + place name
    _truthy("(2) location detected", "location" in detect_personal_content("我在杭州"))

    # (3) relationship: "我女朋友 / 我男朋友 / 老师 / 老板" etc.
    _truthy("(3) relationship detected", "relationship" in detect_personal_content("我女朋友叫 X"))

    # (4) financial: "账户 / 余额 / 工资 / 信用卡 / 贷款" etc.
    _truthy("(4) financial detected", "financial" in detect_personal_content("账户余额 50 万"))

    # (5) Clean (no personal content) → empty list — behavior/pattern text safe
    _eq("(5) clean behavior text", detect_personal_content("操作 moomoo 流程：1. 打开 2. 登录 3. 下单"), [])

    # (6) Empty string → empty list (no crash)
    _eq("(6) empty string", detect_personal_content(""), [])

    # Bonus: Multiple categories simultaneously
    res = detect_personal_content("我女朋友叫小李，她在杭州工作，账户余额 50 万")
    _truthy("(bonus) multi-category: relationship", "relationship" in res)
    _truthy("(bonus) multi-category: location", "location" in res)
    _truthy("(bonus) multi-category: financial", "financial" in res)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())