"""migrate_lex_fts_schema.py — 把远古 lex_fts schema 迁移到 trigram + 存储式内容。

**为什么需要 (2026-10-22 发现, 真实 bug)**

`lex_index._init_schema()` 里的 DDL 是:
    CREATE VIRTUAL TABLE IF NOT EXISTS lex_fts USING fts5(content, tokenize="trigram")

但线上 18/23 个 lex db 里的 lex_fts 其实是**远古 schema**:
    CREATE VIRTUAL TABLE lex_fts USING fts5(
        content, content='documents', content_rowid='fact_id',
        tokenize='porter unicode61')

原因: `IF NOT EXISTS` **永远不会修改已存在的表**。2026-09-02 ship 想从
unicode61 换成 trigram, 但迁移对所有已存在的 db 都是 no-op。

两个后果:
1. unicode61 不切中文 -> 整段中文成 1 个 token, 中文 query 基本搜不到
   (实测: documents 41 行含"旺", MATCH "旺" 只有 2 行)
2. `content='documents'` 是**外部内容表**模式 -> 代码里的
   `INSERT INTO lex_fts(rowid, content)` 在这个模式下会报错, 被
   `except Exception: pass` 吞掉 -> **FTS 索引从建表那天起就再没更新过**
   (admin db: 13473 docs vs 13498 fts rows, 内容早已不同步)

本脚本:
  1. 检测每个 db 的 lex_fts DDL 是否含 tokenize="trigram" 且非外部内容表
  2. 不符合 -> DROP 旧表 -> 按当前代码建新表 -> 从 documents 重灌全部内容
  3. 报告每��� db 的迁移前后行数

用法:
    python -m astor_memory.nest.migrate_lex_fts_schema --dry-run
    python -m astor_memory.nest.migrate_lex_fts_schema
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from .lex_index import _lex_db_path


def _detect(conn) -> tuple[bool, str]:
    """Return (needs_migration, reason)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='lex_fts'").fetchone()
    if not row:
        return False, "no lex_fts (fresh db, will be created on next open)"
    ddl = row[0]
    if "content='" in ddl:
        return True, "外部内容表模式 (INSERT 被吞, 索引不再更新)"
    # 只看真正的 tokenize= 子句, 不能在整个 DDL 文本里搜关键词 ——
    # 注释里出现 "unicode61" 会造成误判 (public db 就是这样被 skip 掉的)。
    m = re.search(r'tokenize\s*=\s*[\'"]([^\'"]+)[\'"]', ddl, re.I)
    tok = (m.group(1) if m else "").lower()
    if "unicode61" not in tok:
        return True, f"tokenizer 是 {tok or '未知'} (应为 unicode61)"
    return False, "ok"


def migrate_db(db_path: Path, dry_run: bool = False) -> dict:
    result = {"db": db_path.name, "path": str(db_path)}
    if not db_path.exists():
        result["status"] = "missing"
        return result
    conn = sqlite3.connect(str(db_path))
    try:
        needs, reason = _detect(conn)
        result["needs_migration"] = needs
        result["reason"] = reason
        n_docs = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE tombstoned=0").fetchone()[0]
        try:
            n_fts = conn.execute("SELECT COUNT(*) FROM lex_fts").fetchone()[0]
        except Exception:
            n_fts = 0
        result["docs"] = n_docs
        result["fts_before"] = n_fts
        if not needs:
            result["status"] = "skip"
            return result
        if dry_run:
            result["status"] = "would-migrate"
            return result

        # 1. drop 旧表 (连带它的 shadow tables)
        conn.execute("DROP TABLE IF EXISTS lex_fts")
        # 2. 按当前代码的 DDL 重建 (存储式内容 + unicode61)
        conn.execute(
            "CREATE VIRTUAL TABLE lex_fts USING fts5("
            "  content,"
            "  tokenize=\"unicode61 remove_diacritics 2\""
            ")")
        # 3. 从 documents 重灌 —— 存**分词后的 token 流** (与 index_fact 一致)
        from .lex_index import _tokenize
        rows = conn.execute(
            "SELECT fact_id, content FROM documents WHERE tombstoned=0"
        ).fetchall()
        conn.executemany(
            "INSERT INTO lex_fts(rowid, content) VALUES (?,?)",
            [(fid, " ".join(_tokenize(content))) for fid, content in rows])
        conn.commit()
        n_fts_after = conn.execute("SELECT COUNT(*) FROM lex_fts").fetchone()[0]
        result["fts_after"] = n_fts_after
        result["status"] = "migrated"
        return result
    except Exception as e:
        result["status"] = f"error: {type(e).__name__}: {e}"
        return result
    finally:
        conn.close()


def _all_db_paths() -> list[Path]:
    from .._internal.acl_layout import get_astor_dir
    base = Path(get_astor_dir()) / "lex" / "memory"
    if not base.exists():
        return []
    return sorted(base.glob("astor_lex_*.db"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default="", help="只处理这一个 db 路径")
    args = ap.parse_args()

    paths = [Path(args.db)] if args.db else _all_db_paths()
    if not paths:
        print("no lex db found")
        return 0

    results = []
    for p in paths:
        results.append(migrate_db(p, dry_run=args.dry_run))

    print(f"{'db':<46} {'status':<16} docs  fts_before -> fts_after  reason")
    for r in results:
        print(f"{r['db']:<46} {r['status']:<16} "
              f"{r.get('docs', 0):>5}  {r.get('fts_before', 0):>10} -> "
              f"{r.get('fts_after', r.get('fts_before', 0)):>9}  {r['reason']}")
    mig = sum(1 for r in results if r["status"] == "migrated")
    need = sum(1 for r in results if r.get("needs_migration"))
    print("")
    print(f"migrated {mig} / {len(results)} db (需要迁移的: {need})")
    return 0


if __name__ == "__main__":
    sys.exit(main())