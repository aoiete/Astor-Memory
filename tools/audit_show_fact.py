"""What exactly is in the facts that carry a session id?

The public tier is shared. A fact there that names a specific session,
a bot id or a message id is private data in a shared place -- even
when the lesson around it is worth keeping.

This prints the whole fact, so the reading is a judgement rather than a
pattern count.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from astor_memory.config import get_default_astor_dir

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = get_default_astor_dir() / 'public' / 'memory' / 'astor_bus_public.db'


def main() -> int:
    ids = [int(x) for x in sys.argv[1:]] or [8332, 7853, 7934]
    con = sqlite3.connect(str(DB))
    for fid in ids:
        row = con.execute(
            "SELECT id, content, kind, visibility, created_at FROM memory_canonical "
            "WHERE id = ? AND tombstoned = 0", (fid,)).fetchone()
        con.close()
        if not row:
            print(f"=== fact {fid}: not found ===\n")
            continue
        fid, content, kind, vis, created = row
        print(f"=== fact {fid}  kind={kind}  visibility={vis}  created={created} ===")
        print(content)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
