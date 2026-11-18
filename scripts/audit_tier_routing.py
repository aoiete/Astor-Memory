"""audit_tier_routing.py — 审计 astor public/private 分层路由是否正确。

只读。检查四件事:
  1. tier 列与物理所在库是否一致 (有没有写错库)
  2. public 库里有没有 private 数据 (PII 泄漏)
  3. visibility 列 (commons/personal) 与实际内容是否一致
  4. 跨库重复: 同一 stable_id / source_hash 出现在多层 = 复制泄漏

用法:
    python audit_tier_routing.py
    python audit_tier_routing.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

RUNTIME = Path(r"D:/AI/Astor-Memory-Runtime")

# The classifier astor itself uses to decide commons vs personal. A
# hand-written regex reports false positives that change the verdict:
# `\bus\b` matches "US 日内" (the US market) and "US ETF", and "我们" in
# a method note is a narrative pronoun that v1.17.31 explicitly counts
# as shareable. The system's own classifier is the only authority.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from astor_memory.nest.visibility_classifier import has_first_person
    from astor_memory.nest.public_scrub import _is_generic_slot
except ImportError:  # pragma: no cover
    has_first_person = None
    _is_generic_slot = None

# High-confidence PII: email / phone / key.
#
# The bare long-number rule is deliberately absent: a Windows process
# start time is 12-13 digits and looks exactly like a phone number.
# A real phone number needs a Chinese mobile prefix (1[3-9] then 9
# digits) and a credential value needs a word in front of it, so both
# are anchored rather than bare.
PII = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|"
    r"(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"\b(?:sk|pk|ghp|xox[baprs])-[A-Za-z0-9]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"(?:api[_-]?key|secret|token|password|passwd|密码|密钥|私钥)"
    r"\s*(?:[:=]|是|为)\s*['\"]?[A-Za-z0-9_\-\.]{8,}",
    re.I)
# Platform account ids (Telegram chat id / weixin openid / discord id).
ACCOUNT_ID = re.compile(
    r"\b\d{15,20}\b|"                       # telegram chat_id / discord snowflake
    r"o[A-Za-z0-9_-]{20,}@im\.wechat|"      # weixin openid
    r"@im\.bot\b",
    re.I)

PRIVATE_MARKERS = ("private", "source")


def node_user_names() -> set[str]:
    """The user ids this node hosts -- a name in the shared tier is
    private data whether or not a pronoun is around it."""
    try:
        from astor_memory._internal.acl_layout import list_user_ids
        return {u for u in list_user_ids() if u}
    except Exception:
        return set()


def handle_leaks(text: str, names: set[str]) -> list[str]:
    """User handles named in the text, as whole words.

    A name glued to another word is part of that word, not a reference
    to a person -- the same boundary rule the public scrub applies. A
    slot name ("admin", "_current") is excluded for the same reason the
    scrub excludes it: it names a system role, and every node has one.
    """
    if not names:
        return []
    out = []
    for n in names:
        if len(n) < 3:
            continue
        if _is_generic_slot is not None and _is_generic_slot(n):
            continue
        if re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', text, re.I):
            out.append(n)
    return out


def dbs() -> list[tuple[str, Path]]:
    out = []
    for tier_dir in ("public", "private", "source"):
        p = RUNTIME / tier_dir / "memory"
        if p.is_dir():
            for db in sorted(p.glob("astor_bus_*.db")):
                out.append((db.stem.replace("astor_bus_", ""), db))
    users = RUNTIME / "users"
    if users.is_dir():
        for u in sorted(users.iterdir()):
            p = u / "memory"
            if p.is_dir():
                for db in sorted(p.glob("astor_bus_*.db")):
                    out.append((f"users/{u.name}", db))
    return out


def ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def has_table(con: sqlite3.Connection, name: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def cols_of(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def audit() -> dict:
    report: dict = {"runtime": str(RUNTIME), "dbs": [], "problems": []}
    for label, db in dbs():
        try:
            con = ro(db)
        except sqlite3.Error as e:
            report["problems"].append(f"{label}: cannot open ({e})")
            continue
        try:
            # Not every .db under the runtime is a bus -- nest/lex/embeddings
            # sidecars live here too, and they have no memory_canonical.
            if not has_table(con, "memory_canonical"):
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                report["dbs"].append({
                    "db": label, "path": str(db), "facts": 0,
                    "skipped": "no memory_canonical (sidecar db)",
                    "tables": tables[:12],
                })
                continue
            cc = cols_of(con, "memory_canonical")
            missing = [c for c in ("visibility", "tier", "memory_class",
                                   "stable_id", "source_hash") if c not in cc]
            n = con.execute(
                "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0").fetchone()[0]
            tiers = Counter(r[0] for r in con.execute(
                "SELECT tier FROM memory_canonical WHERE tombstoned=0")) if "tier" in cc else {}
            vis = (Counter(r[0] for r in con.execute(
                "SELECT COALESCE(visibility,'<NULL>') FROM memory_canonical WHERE tombstoned=0"))
                if "visibility" in cc else {})
            classes = (Counter(r[0] for r in con.execute(
                "SELECT COALESCE(memory_class,'<NULL>') FROM memory_canonical WHERE tombstoned=0"))
                if "memory_class" in cc else {})
            kinds = Counter(r[0] for r in con.execute(
                "SELECT kind FROM memory_canonical WHERE tombstoned=0"))
            users = Counter(r[0] for r in con.execute(
                "SELECT COALESCE(user_id,'<NULL>') FROM memory_canonical WHERE tombstoned=0"))
            entry = {
                "db": label, "path": str(db), "facts": n,
                "missing_columns": missing,
                "tier_column": dict(tiers),
                "visibility": dict(vis),
                "memory_class": dict(classes),
                "kind": dict(kinds),
                "user_id": dict(users),
            }
            if missing:
                report["problems"].append(
                    f"{label}: memory_canonical missing columns {missing} "
                    f"(schema migration not applied to this db)")
            report["dbs"].append(entry)

            # --- 泄漏检测: 只对 public 层做 ---
            if label == "public":
                leaks = {"first_person": [], "pii": [], "account_id": [],
                         "user_handle": []}
                handles = node_user_names()
                vis_expr = "visibility" if "visibility" in cc else "NULL AS visibility"
                for r in con.execute(
                        f"SELECT id, kind, {vis_expr}, content FROM memory_canonical "
                        "WHERE tombstoned = 0 AND content IS NOT NULL"):
                    c = r["content"]
                    if has_first_person is not None and has_first_person(c):
                        leaks["first_person"].append(
                            {"id": r["id"], "kind": r["kind"], "vis": r["visibility"],
                             "excerpt": c[:150]})
                    names = handle_leaks(c, handles)
                    if names:
                        leaks["user_handle"].append(
                            {"id": r["id"], "kind": r["kind"], "vis": r["visibility"],
                             "names": names, "excerpt": c[:150]})
                    if PII.search(c):
                        leaks["pii"].append(
                            {"id": r["id"], "kind": r["kind"], "vis": r["visibility"],
                             "excerpt": c[:150]})
                    elif ACCOUNT_ID.search(c):
                        leaks["account_id"].append(
                            {"id": r["id"], "kind": r["kind"], "vis": r["visibility"],
                             "excerpt": c[:150]})
                entry["leaks"] = {k: len(v) for k, v in leaks.items()}
                entry["leak_samples"] = {
                    k: v[:5] for k, v in leaks.items() if v}
                # personal visibility 出现在 public = 分层自相矛盾
                if "visibility" in cc:
                    personal_in_public = con.execute(
                        "SELECT COUNT(*) FROM memory_canonical "
                        "WHERE tombstoned=0 AND visibility='personal'").fetchone()[0]
                    entry["personal_vis_rows_in_public"] = personal_in_public
        finally:
            con.close()

    # --- 跨库重复检测 (stable_id / source_hash) ---
    ids: dict[str, list[str]] = {}
    hashes: dict[str, list[str]] = {}
    for label, db in dbs():
        try:
            con = ro(db)
        except sqlite3.Error:
            continue
        try:
            if not has_table(con, "memory_canonical"):
                continue
            cc = cols_of(con, "memory_canonical")
            sel = [c for c in ("stable_id", "source_hash") if c in cc]
            if not sel:
                continue
            for r in con.execute(
                    f"SELECT {', '.join(sel)} FROM memory_canonical "
                    "WHERE tombstoned=0 AND (" +
                    " OR ".join(f"{c} IS NOT NULL" for c in sel) + ")"):
                for c in sel:
                    val = r[c]
                    if not val:
                        continue
                    if c == "stable_id":
                        ids.setdefault(val, []).append(label)
                    else:
                        hashes.setdefault(val, []).append(label)
        finally:
            con.close()
    dup_ids = {k: v for k, v in ids.items() if len(set(v)) > 1}
    dup_hashes = {k: v for k, v in hashes.items() if len(set(v)) > 1}
    report["cross_db_duplicates"] = {
        "stable_id": len(dup_ids),
        "source_hash": len(dup_hashes),
        "examples": {k: sorted(set(v)) for k, v in list(dup_ids.items())[:5]},
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rep = audit()
    print("=" * 72)
    print("ASTOR TIER ROUTING AUDIT")
    print(f"runtime = {rep['runtime']}")
    print("=" * 72)
    for d in rep["dbs"]:
        print(f"\n--- {d['db']}  ({d['facts']} facts) ---")
        if d.get("skipped"):
            print(f"    path    = {d['path']}")
            print(f"    SKIPPED = {d['skipped']}")
            continue
        print(f"    path       = {d['path']}")
        print(f"    tier col   = {d['tier_column']}")
        print(f"    visibility = {d['visibility']}")
        print(f"    user_id    = {d['user_id']}")
        if d.get("missing_columns"):
            print(f"    !! missing columns = {d['missing_columns']}")
        if "leaks" in d:
            print(f"    LEAKS      = {d['leaks']}")
            if d.get("personal_vis_rows_in_public"):
                print(f"    !! personal-visibility rows in public = "
                      f"{d['personal_vis_rows_in_public']}")
    print(f"\n--- cross-db duplicates ---")
    print(f"    {rep['cross_db_duplicates']}")
    if rep["problems"]:
        print(f"\n--- problems ---")
        for p in rep["problems"]:
            print(f"    {p}")

    if args.json:
        Path(args.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\nsaved: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
