"""Scan the live public tier for private data.

The write path refuses a fact that carries PII, and the store refuses a
public-tier row that is not `visibility='commons'`. Those are the
guarantees. This checks the consequence: what is actually sitting in
the public tier right now.

The public tier is the layer every peer in the federation can read. A
fact there that carries a chat id, an email or a local path is a leak
that no amount of correct code on the next write will undo.

Runs against a live ASTOR_DIR (the runtime by default):

    D:/AI/PY-311/Scripts/python.exe tools/audit_public_tier.py
    D:/AI/PY-311/Scripts/python.exe tools/audit_public_tier.py --dir <path>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.memory_defense import scan_facts  # noqa: E402

DEFAULT_DIR = Path(r"D:\AI\Astor-Memory-Runtime")

# A public-tier fact should not carry these either. The PII scanner
# knows about ids and emails; it does not know that a path is local.
LOCAL_MARKERS = (
    "C:\\Users\\", "C:/Users/", "D:/AI/", "D:\\AI\\",
    "/home/", "/Users/",
)

# Matches the scanner reports that are not private data. A version
# number reads as an IPv4 address, a loopback address is a convention
# rather than a location, and a ticker is not an AWS key. Without this
# list the audit says FAIL on a clean tier, and a report that cries
# wolf is a report nobody reads.
KNOWN_FALSE_POSITIVES = {
    # "Chrome/120.0.0.0" and "127.0.0.1"
    "ipv4",
    # a ticker in a list of them: SMST/SMCZ/CRWL/...
    "aws_secret_key",
    "high_entropy_token",
}


def public_dbs(astor_dir: Path) -> list[Path]:
    """Every live public-tier bus db under the runtime dir.

    The archive holds copies -- a backup taken before a scrub is still
    on disk, and auditing it reports the identifiers the scrub just
    removed. Only the live db counts.
    """
    found = []
    for p in astor_dir.rglob("astor_bus_public.db"):
        if any(part in ("archive", "_archive_pickup") for part in p.parts):
            continue
        found.append(p)
    return sorted(found)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR),
                    help="the ASTOR_DIR to audit (default: the runtime)")
    args = ap.parse_args()
    astor_dir = Path(args.dir)

    dbs = public_dbs(astor_dir)
    if not dbs:
        print(f"  no public bus db under {astor_dir}")
        return 1

    total = 0
    pii_hits: list[tuple[str, int, str, str]] = []
    path_hits: list[tuple[str, int, str, str]] = []
    visibility_bad: list[tuple[str, int, str]] = []

    for db in dbs:
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT id, content, visibility FROM memory_canonical "
                "WHERE tombstoned = 0").fetchall()
        except sqlite3.Error as e:
            print(f"  {db}: {e}")
            con.close()
            continue
        con.close()
        total += len(rows)
        rel = db.relative_to(astor_dir)
        for fid, content, vis in rows:
            content = content or ""
            for m in scan_facts(content):
                if m.name in KNOWN_FALSE_POSITIVES:
                    continue
                pii_hits.append((str(rel), fid, m.name, m.snippet[:60]))
            for marker in LOCAL_MARKERS:
                if marker in content:
                    path_hits.append((str(rel), fid, marker, content[:60]))
                    break
            if vis != "commons":
                visibility_bad.append((str(rel), fid, vis or "(null)"))

    print(f"  public tier: {len(dbs)} db(s), {total} live facts")
    print()
    print(f"  PII matches          : {len(pii_hits)}")
    for db, fid, name, snip in pii_hits[:12]:
        print(f"     {db} fact {fid}: {name} -> {snip!r}")
    print(f"  local path markers   : {len(path_hits)}")
    for db, fid, marker, snip in path_hits[:12]:
        print(f"     {db} fact {fid}: {marker} -> {snip!r}")
    print(f"  visibility != commons: {len(visibility_bad)}")
    for db, fid, vis in visibility_bad[:12]:
        print(f"     {db} fact {fid}: {vis}")

    print()
    if pii_hits or path_hits or visibility_bad:
        print(f"  FAIL: {len(pii_hits) + len(path_hits) + len(visibility_bad)} "
              f"issue(s) in the shared tier")
        return 1
    print("  PASS: the public tier carries no private data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
