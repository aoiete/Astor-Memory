"""Refresh the embeddings of the facts whose content was scrubbed.

The scrub rewrote seventeen facts in place: the identifiers became
placeholders, the lessons stayed. Their embeddings were computed from
the old text, so the vector still represents the version with the
identifiers in it -- the semantic index is a little out of date.

`am reembed` only picks up facts that have no embedding at all, so it
will not touch these. The old vectors have to go first.

This deletes the embeddings for exactly the facts the scrub changed,
and prints what it removed. `am reembed` then fills them back in.

Run with the project interpreter:
    D:/AI/PY-311/Scripts/python.exe tools/refresh_scrubbed_embeddings.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUNTIME = Path(r"D:\AI\Astor-Memory-Runtime")
BUS = RUNTIME / "public" / "memory" / "astor_bus_public.db"
NEST = RUNTIME / "public" / "memory" / "astor_nest_public.db"


def main() -> int:
    # the facts whose content still carries an identifier are the ones
    # the scrub did not reach; the ones it reached are now clean. The
    # scrub wrote a marker into the content, so the clean set is found
    # by comparing against the backup taken before the scrub.
    backup = RUNTIME / "archive" / "pre-public-scrub-20261030" / "astor_bus_public.db"
    if not backup.exists():
        print(f"  no pre-scrub backup at {backup}")
        return 1

    old = sqlite3.connect(str(backup))
    new = sqlite3.connect(str(BUS))
    changed = []
    for fid, content in old.execute(
            "SELECT id, content FROM memory_canonical WHERE tombstoned = 0"):
        row = new.execute(
            "SELECT content FROM memory_canonical WHERE id = ? AND tombstoned = 0",
            (fid,)).fetchone()
        if row and row[0] != content:
            changed.append(fid)
    old.close()

    print(f"  {len(changed)} facts were changed by the scrub")

    if not NEST.exists():
        print(f"  no nest db at {NEST}")
        return 1
    nest = sqlite3.connect(str(NEST))
    try:
        present = [f for f in changed if nest.execute(
            "SELECT 1 FROM embeddings WHERE fact_id = ?", (f,)).fetchone()]
        print(f"  {len(present)} of them have an embedding")
        for fid in present:
            nest.execute("DELETE FROM embeddings WHERE fact_id = ?", (fid,))
        nest.commit()
        print(f"  removed {len(present)} stale embeddings")
        print("  now run:  am reembed")
    finally:
        nest.close()
    new.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
