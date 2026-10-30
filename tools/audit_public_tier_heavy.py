"""Show the context of the heavy PII matches in the shared tier.

A match count says how many; this says which. The interesting shapes
are the ones that look like credentials: a card number, an AWS key, a
high-entropy token. Most turns out to be a version string or a hash
in an example, but "most" is not a reason to skip the check.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.memory_defense import scan_facts  # noqa: E402

DB = Path(r"D:\AI\Astor-Memory-Runtime\public\memory\astor_bus_public.db")
HEAVY = {"credit_card", "aws_secret_key", "high_entropy_token", "ipv4",
         "discord_snowflake"}


def main() -> int:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT id, content, kind FROM memory_canonical WHERE tombstoned = 0"
    ).fetchall()
    con.close()

    for fid, content, kind in rows:
        content = content or ""
        ms = [m for m in scan_facts(content) if m.name in HEAVY]
        if not ms:
            continue
        print(f"=== fact {fid} ({kind}) ===")
        for m in ms:
            st = max(0, m.start - 85)
            print(f"  [{m.name}] {m.snippet!r}")
            print(f"     ...{content[st:m.end + 65]}...")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
