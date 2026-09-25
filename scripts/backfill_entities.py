"""Backfill entities_json for facts with empty entities (v1.14.21 extractor).

Usage:
    python backfill_entities.py            # dry-run (default) — report only
    python backfill_entities.py --apply    # write entities_json in place

Env:
    ASTOR_DIR  runtime root (default D:/AI/Astor-Memory-Runtime)

Safety:
    - sqlite3 backup API snapshot of each bus DB before any write
    - busy_timeout 5s; short per-fact transactions (server stays live)
    - only touches rows where entities_json IS NULL/''/'[]' and tombstoned=0
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from astor_memory.forge.extractor import extract_entities  # noqa: E402

ASTOR_DIR = os.environ.get("ASTOR_DIR", r"D:/AI/Astor-Memory-Runtime")


def find_bus_dbs(root):
    """Yield (label, path) for every bus db: public + users/*/memory."""
    pub = os.path.join(root, "public", "memory", "astor_bus_public.db")
    if os.path.exists(pub):
        yield ("public", pub)
    users = os.path.join(root, "users")
    if os.path.isdir(users):
        for u in sorted(os.listdir(users)):
            p = os.path.join(users, u, "memory", f"astor_bus_{u}.db")
            if os.path.exists(p):
                yield (u, p)


def backup_db(path):
    """Consistent snapshot via sqlite backup API (WAL-safe)."""
    bak = f"{path}.bak-pre-entity-backfill-{time.strftime('%Y%m%d_%H%M%S')}"
    src = sqlite3.connect(path)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    return bak


def process(label, path, apply):
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    rows = db.execute(
        "SELECT id, content FROM memory_canonical "
        "WHERE tombstoned=0 AND (entities_json IS NULL OR entities_json='' "
        "OR entities_json='[]')"
    ).fetchall()
    would_fill = 0
    filled = 0
    for fid, content in rows:
        ents = extract_entities(content or "", fact_id=fid)
        if not ents:
            continue
        would_fill += 1
        if apply:
            db.execute(
                "UPDATE memory_canonical SET entities_json=? WHERE id=?",
                (json.dumps(ents, ensure_ascii=False), fid),
            )
            db.commit()
            filled += 1
    db.close()
    return len(rows), would_fill, filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] ASTOR_DIR={ASTOR_DIR}")
    grand_empty = grand_fill = 0
    for label, path in find_bus_dbs(ASTOR_DIR):
        if args.apply:
            bak = backup_db(path)
            print(f"  backup: {bak}")
        empty, would_fill, filled = process(label, path, args.apply)
        grand_empty += empty
        grand_fill += would_fill
        verb = "filled" if args.apply else "would fill"
        print(f"  {label}: empty={empty} {verb}={filled if args.apply else would_fill}")
    print(f"[{mode}] total empty={grand_empty} fillable={grand_fill}")
    if not args.apply:
        print("dry-run only — pass --apply to write")


if __name__ == "__main__":
    main()
