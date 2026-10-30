"""Does a private identifier survive distillation into the shared tier?

The leak was found in the live public tier, not in a test: a lesson
fact carried "[STATE:6]=7519050235899700000" and "[STATE:8]=8263b17ef000"
-- a message id and a bot account id. The key that said what they were
("message_id=", "bot_id=") had been stripped, so nothing downstream
could recognise them.

The extractor runs before the sentence filter, so a token is already
gone from the body by the time the PII pass sees it. The context is
therefore read at extraction time, while the original text still has
the key.

This feeds the real source text through the distiller and checks what
comes out the other side.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.distiller import distill  # noqa: E402

# The shape of the live fact that leaked, with the values replaced by
# ones that are just as identifiable in form.
SOURCE = (
    "LESSON [R-class locked 2026-10-23] fortune-daily-push-0900mdt 连续失败，"
    "根因是 iLink 会话 TTL ~48h 而非代码 bug。 事实链: "
    "- 10-22 09:01:11 成功 (message_id=7519050235899700000) "
    "- 10-24 09:01:19 失败 exit 2, ret=-2 prepare failed "
    "相关脚本 scripts/fortune_daily_push_weixin.py, "
    "配置项 gateway/platforms/weixin.py, "
    "bot_id=8263b17ef000, cron job_id=6aca84e6bd81"
)

# A cron job id names a job on THIS machine -- no peer can act on it,
# so it is private in the shared tier too. The rule catches it because
# the source text said "job_id=".
PRIVATE = ["7519050235899700000", "8263b17ef000", "6aca84e6bd81"]
PUBLIC = ["fortune_daily_push_weixin.py", "gateway/platforms/weixin.py",
          "TTL", "ret=-2"]


def main() -> int:
    cleaned, removed, report = distill(SOURCE)
    print("  distilled:")
    print("   ", cleaned)
    print()
    print("  report:", {k: v for k, v in report.items()
                         if k in ("sentences_in", "sentences_out",
                                  "dropped_count", "state_const_count",
                                  "state_const_preserved")})
    print()

    bad = False
    for v in PRIVATE:
        gone = v not in cleaned
        print(f"    private {v:24} {'REDACTED' if gone else 'STILL THERE'}")
        bad = bad or not gone
    for v in PUBLIC:
        kept = v in cleaned
        print(f"    public  {v:24} {'KEPT' if kept else 'LOST'}")
        bad = bad or not kept

    print()
    print("  FAIL" if bad else "  PASS: the method survives, the ids do not")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
