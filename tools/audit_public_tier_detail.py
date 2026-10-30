"""How much private data is actually in the shared tier?

The audit script counts PII matches; this one shows what the matches
are, because a count without the context is not an answer.

Two shapes matter here:

  - a session id in the form `weixin 20261005_085110_6e87fc76`. The
    scanner's snippet is masked, which is its display behaviour, not
    what is stored -- the row holds the real thing.
  - a local path. The PII scanner does not know a path is local; that
    is the scrubber's job.

Run with the project interpreter:
    D:/AI/PY-311/Scripts/python.exe tools/audit_public_tier_detail.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.memory_defense import scan_facts  # noqa: E402

DB = Path(r"D:\AI\Astor-Memory-Runtime\public\memory\astor_bus_public.db")

SESSION_ID = re.compile(r"\b(weixin|discord|telegram)\s+(\d{8}_[0-9a-f]{6,})", re.I)
LOCAL_PATH = re.compile(
    r"[A-Za-z]:[\\/](?:AI|Users)[\\/][^\s\"'`,;)]{2,}|/(?:home|Users)/[^\s\"'`,;)]{2,}")


def main() -> int:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT id, content, kind, visibility FROM memory_canonical "
        "WHERE tombstoned = 0").fetchall()
    con.close()

    print(f"  public tier: {len(rows)} live facts")
    print()

    sess: dict[int, tuple[str, set]] = {}
    paths: dict[int, tuple[str, set]] = {}
    pii_names: dict[str, int] = {}

    for fid, content, kind, vis in rows:
        content = content or ""
        for m in SESSION_ID.finditer(content):
            sess.setdefault(fid, (kind, set()))[1].add(m.group(0))
        for m in LOCAL_PATH.finditer(content):
            paths.setdefault(fid, (kind, set()))[1].add(m.group(0))
        for m in scan_facts(content):
            pii_names[m.name] = pii_names.get(m.name, 0) + 1

    print(f"  facts carrying a real session id: {len(sess)}")
    for fid, (kind, ids) in sorted(sess.items()):
        print(f"    fact {fid} ({kind}): {sorted(ids)}")
    print()
    print(f"  facts carrying a local path      : {len(paths)}")
    for fid, (kind, ps) in sorted(paths.items())[:10]:
        print(f"    fact {fid} ({kind}): {sorted(ps)[:2]}")
    print()
    print("  scanner match counts by pattern:")
    for name, n in sorted(pii_names.items(), key=lambda kv: -kv[1]):
        print(f"    {name:22} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
