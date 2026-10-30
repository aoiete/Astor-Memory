"""What does the PII gate actually catch?

has_pii is the gate that decides whether a fact may be published to
the shared tier. This probes it with the identifier shapes that
matter -- a messaging user id, a bare chat id, a real email -- next to
the shapes it is known to handle (paths).

If a shape comes back empty, it is not on the deny list, and a fact
carrying it can reach the public tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.visibility_classifier import (  # noqa: E402
    classify_visibility, has_pii)

SAMPLES = {
    "wechat user id": "微信 user_id 是 wxid0000abcd1234@im.wechat",
    "bare chat id": "Telegram 1234567890",
    "real email": "邮箱 someone@example.invalid",
    "windows path": "C:\\Users\\TestUser\\AppData\\Local\\hermes\\config.yaml",
    "phone": "打 13800138000 找我",
    "id card": "身份证 11010119900307231X",
}


def main() -> int:
    print("has_pii:")
    for name, s in SAMPLES.items():
        hits = has_pii(s)
        print(f"  {name:18} -> {hits if hits else 'NOT DETECTED'}")

    print()
    payload = (
        "排查的时候发现 C:\\Users\\TestUser\\AppData\\Local\\hermes\\config.yaml "
        "里有 wxid0000abcd1234@im.wechat，Telegram 1234567890，"
        "邮箱 someone@example.invalid"
    )
    r = classify_visibility(payload, kind="fact", hint="commons")
    print("classify_visibility(kind=fact, hint=commons):")
    print(f"  visibility       : {r['visibility']}")
    print(f"  reason           : {r['reason']}")
    print(f"  override_allowed : {r.get('override_allowed')}")
    for k, v in r["signals"].items():
        if v:
            print(f"  signal {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
