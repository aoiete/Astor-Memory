"""Tombstone unambiguous junk facts (test probes, session noise, stale status).

Conservative: only soft-delete (tombstoned=1) facts matching explicit junk
patterns. Everything borderline stays. Backup via sqlite backup API first.

Usage:
    python tombstone_junk_facts.py            # dry-run
    python tombstone_junk_facts.py --apply
"""
import argparse
import os
import re
import sqlite3
import time

ASTOR_DIR = os.environ.get("ASTOR_DIR", r"D:/AI/Astor-Memory-Runtime")

JUNK_PATTERNS = [
    ("session工具记录", r"^session 中使用工具"),
    ("session outcome碎片", r"^session outcome:"),
    ("测试探针", r"(?i)(spam test|smoke test|admin test|VERIFY_PROBE|PRIVATE_PROBE|private-test|path probe|hook smoke|机制验证测试)"),
    ("ACL测试fixture", r"(?i)(my stocks went down|tired and sad about my work|^[a-z-]+ own (v\d+|note|private)$)"),
    ("过期状态快照", r"(?i)(^astor.{0,30}(alive|server 在 :\d+)|\d+ facts / \d+ events|^\d+ embeds / \d+ facts)"),
]
COMPILED = [(name, re.compile(p)) for name, p in JUNK_PATTERNS]


def find_bus_dbs(root):
    pub = os.path.join(root, "public", "memory", "astor_bus_public.db")
    if os.path.exists(pub):
        yield ("public", pub)
    users = os.path.join(root, "users")
    if os.path.isdir(users):
        for u in sorted(os.listdir(users)):
            p = os.path.join(users, u, "memory", f"astor_bus_{u}.db")
            if os.path.exists(p):
                yield (u, p)


def classify(content):
    c = (content or "").strip()
    for name, pat in COMPILED:
        if pat.search(c):
            return name
    return None


def backup_db(path):
    bak = f"{path}.bak-pre-junk-tombstone-{time.strftime('%Y%m%d_%H%M%S')}"
    src = sqlite3.connect(path)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    return bak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] ASTOR_DIR={ASTOR_DIR}")
    grand = {}
    for label, path in find_bus_dbs(ASTOR_DIR):
        if args.apply:
            print(f"  backup: {backup_db(path)}")
        db = sqlite3.connect(path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        rows = db.execute(
            "SELECT id, content FROM memory_canonical WHERE tombstoned=0"
        ).fetchall()
        hits = {}
        for fid, content in rows:
            cat = classify(content)
            if cat:
                hits.setdefault(cat, []).append(fid)
        if args.apply:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for cat, ids in hits.items():
                for fid in ids:
                    db.execute(
                        "UPDATE memory_canonical SET tombstoned=1, "
                        "tombstoned_at=? WHERE id=? AND tombstoned=0",
                        (now, fid),
                    )
            db.commit()
        db.close()
        if hits:
            print(f"  {label}: " + ", ".join(f"{k}={len(v)}" for k, v in hits.items()))
            for k, v in hits.items():
                grand[k] = grand.get(k, 0) + len(v)
    print(f"[{mode}] TOTAL: " + (", ".join(f"{k}={v}" for k, v in grand.items()) or "none"))
    print(f"[{mode}] grand total = {sum(grand.values())}")
    if not args.apply:
        print("dry-run only — pass --apply to tombstone")


if __name__ == "__main__":
    main()
