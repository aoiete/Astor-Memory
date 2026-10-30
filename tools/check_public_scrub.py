"""What the scrubber does, and what it does not.

The scrubber in promote_candidate rewrites machine-local paths: a
Windows user directory becomes <home>, a runtime path becomes <root>.
It is a path scrubber, and that is what it is for -- the public tier
should keep the method and lose the machine.

It does NOT remove identifiers. That is a different layer, and a
different job: the PII gate in the write path refuses the write
before it can be promoted (see check_pii_gate.py for the gate and
check_public_leak.py for the whole path end to end).

So a red line here for an identifier is expected and correct. This
script exists to keep the two layers from being confused with each
other -- a scrubber that silently deleted every id would destroy the
knowledge the shared tier is for.

Run with the project interpreter (numpy is a dependency):
    D:/AI/PY-311/Scripts/python.exe tools/check_public_scrub.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONTENT = (
    "排查的时候发现 C:\\Users\\TestUser\\AppData\\Local\\hermes\\config.yaml "
    "里有 D:/AI/users/admin 的配置，微信 user_id 是 "
    "wxid0000abcd1234@im.wechat，Telegram 1234567890，"
    "邮箱 someone@example.invalid，仓库在 git@github.com:aoiete/ASTOR-Memory。"
)

LEAKS = {
    "windows user path": "TestUser",
    "private runtime path": "D:/AI/users",
    "wechat user id": "@im.wechat",
    "telegram chat id": "1234567890",
    "real email": "someone@example.invalid",
}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="astor_scrubtest_")
    os.environ["ASTOR_DIR"] = tmp
    try:
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory.bus import astor_bus

        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        bus = astor_bus(tier="public", user_id=None)

        ev = bus.append_event(namespace="scrubtest", agent_id="t", source="t",
                              action="write", content=CONTENT, metadata={})
        cand = bus.insert_candidate(event_id=ev, namespace="scrubtest",
                                    content=CONTENT, kind="fact",
                                    importance=0.7, metadata={})
        fid = bus.promote_candidate(candidate_id=cand, promoted_by="t",
                                    user_id="admin", tier="public",
                                    scope_type="long_term",
                                    visibility="commons", verdict="settled")

        db = Path(tmp) / "public" / "memory" / "astor_bus_public.db"
        con = sqlite3.connect(str(db))
        stored = con.execute(
            "SELECT content FROM memory_canonical WHERE id=?", (fid,)).fetchone()[0]
        con.close()

        print("stored in public tier:")
        print(" ", stored)
        print()
        leaked = []
        for name, needle in LEAKS.items():
            hit = needle in stored
            if hit:
                leaked.append(name)
            print(f"  {name:22} {'LEAKED' if hit else 'scrubbed'}")
        print()
        if leaked:
            print(f"EXPECTED: {len(leaked)} identifier(s) are not the "
                  f"scrubber's job: {', '.join(leaked)}")
            print("  The PII gate refuses the write before promotion -- "
                  "check_public_leak.py proves that end to end.")
            return 0
        print("no identifier reached the shared tier")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
