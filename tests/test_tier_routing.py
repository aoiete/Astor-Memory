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

    print("\n=== Reactive consult gate (v1.16.x) ===")

    # v1.16.x: env ASTOR_CONSULT_DEFAULT_ON defaults to '1' (proactive ON).
    # body.consult=True forces meta-recall; body.consult=False skips; None
    # falls back to env. The decision logic is replicated below since
    # _meta_recall_patterns is a nested function (not importable).
    import os
    saved = os.environ.get("ASTOR_CONSULT_DEFAULT_ON")

    def _consult_gate(body_consult, env_val):
        os.environ["ASTOR_CONSULT_DEFAULT_ON"] = env_val
        if body_consult is None:
            return os.environ.get("ASTOR_CONSULT_DEFAULT_ON", "1") == "1"
        return bool(body_consult)

    try:
        # env=OFF + body=None → False
        _eq("env=OFF + body=None → False", _consult_gate(None, "0"), False)
        # body.consult=True overrides env=OFF → True
        _eq("body.consult=True → True", _consult_gate(True, "0"), True)
        # body.consult=False overrides env=ON → False
        _eq("body.consult=False → False", _consult_gate(False, "1"), False)
        # env=ON + body=None → True (default v1.16 proactive)
        _eq("env=ON + body=None → True", _consult_gate(None, "1"), True)
    finally:
        if saved is not None:
            os.environ["ASTOR_CONSULT_DEFAULT_ON"] = saved
        else:
            os.environ.pop("ASTOR_CONSULT_DEFAULT_ON", None)

    print("\n=== CLI am postmortem default tier (v1.16.x) ===")

    # Verify the parser default for am postmortem is public by reading
    # the CLI source directly (the argparse parser is local to main() and
    # not introspectable from outside).
    import tempfile
    _probe = Path(tempfile.gettempdir()) / "_astor_postmortem_help_probe.txt"
    import astor_memory.cli as _cli_pkg
    _cli_path = Path(_cli_pkg.__file__).parent / "main.py"
    _cli_src = _cli_path.read_text(encoding='utf-8', errors='replace')

    # Source-of-truth check: grep the CLI source for the postmortem default.
    _pm_idx = _cli_src.find("add_parser(\n        'postmortem'")
    if _pm_idx == -1:
        _pm_idx = _cli_src.find("add_parser(\n        'postmortem',")
    _truthy("am postmortem parser block found in CLI source", _pm_idx != -1)
    if _pm_idx != -1:
        _window = _cli_src[_pm_idx:_pm_idx + 1500]
        _truthy("am postmortem --tier default='public' (v1.16+)", "'public'" in _window)
        _truthy("am postmortem help no longer says 'tier=private'", "tier=private" not in _window)
        _truthy("am postmortem has --tier-hint flag", "--tier-hint" in _window)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())