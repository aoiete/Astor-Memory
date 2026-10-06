# astor_memory.export_memory — universal memory-export to multiple formats.
#
# v1.16.71 (2026-10-06) Ship B: not all agents are Hermes / have MEMORY.md.
# Export the bus to multiple formats so any agent can consume:
#
#   - markdown (Hermes, Claude Code, Cursor, generic chat agents)
#   - json (API-based, RAG, programmatic consumers)
#   - csv (Excel, spreadsheet workflows)
#   - agent_context (system-prompt snippets, ready to paste)
#
# CLI:
#   python -m astor_memory.export_memory --format markdown --user admin --output /tmp/mem.md
#   python -m astor_memory.export_memory --format json --tier source --top 50
#   python -m astor_memory.export_memory --format csv --output /tmp/mem.csv
#   python -m astor_memory.export_memory --format all --top 20 --output /tmp/mem_dir
#
# Dream-tier gate: by default only T1+ facts (importance>=0.7 OR dq>=3).
# --include-all overrides for full extraction.
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _fetch_facts(conn, user_id, tier, include_all, top):
    """Pull facts matching dream-tier gate (importance>=0.7 OR dq>=3) unless --include-all."""
    params = []
    where = ["tombstoned = 0"]
    if user_id:
        where.append("(user_id = ? OR user_id IS NULL)")
        params.append(user_id)
    if tier and tier != "all":
        where.append("tier = ?")
        params.append(tier)
    if not include_all:
        where.append("(importance >= 0.7 OR distinct_queries_hit >= 3)")
    sql = (
        "SELECT id, namespace, content, kind, importance, confidence, "
        "distinct_queries_hit, access_count, last_confirmed_at, tags "
        "FROM memory_canonical WHERE " + " AND ".join(where) + " "
        "ORDER BY importance DESC, distinct_queries_hit DESC, last_confirmed_at DESC"
    )
    if top:
        sql += " LIMIT " + str(int(top))
    rows = conn.execute(sql, params).fetchall()
    cols = ["id","namespace","content","kind","importance","confidence",
            "distinct_queries_hit","access_count","last_confirmed_at","tags"]
    return [dict(zip(cols, r)) for r in rows]


def _format_markdown(facts):
    """Hermes-style MEMORY.md: section per kind, bullet per fact."""
    by_kind = defaultdict(list)
    for f in facts:
        by_kind[f["kind"]].append(f)
    out = ["# Memory Export",
           "",
           "_Generated: " + datetime.now(timezone.utc).isoformat(timespec='seconds') + "Z_",
           "_Total facts: " + str(len(facts)) + "_",
           ""]
    kind_priority = ["user_preference", "rule", "lesson", "profile",
                     "risk_rule", "success_pattern", "failure_pattern", "fact"]
    for kind in kind_priority:
        items = by_kind.pop(kind, [])
        if not items:
            continue
        out.append("## " + kind.replace("_", " ").title() + " (" + str(len(items)) + ")")
        out.append("")
        for f in items[:50]:
            tag = " `[imp=" + f"{f['importance']:.1f}" + " dq=" + str(f["distinct_queries_hit"]) + "]`" if f["distinct_queries_hit"] else ""
            content = f["content"].replace("\n", " ")[:300]
            out.append("- " + content + tag)
        out.append("")
    for kind, items in by_kind.items():
        out.append("## " + kind + " (" + str(len(items)) + ")")
        for f in items[:50]:
            out.append("- " + f["content"][:300])
    return "\n".join(out)


def _format_json(facts):
    """RAG / API / programmatic consumer: structured JSON."""
    return json.dumps(
        {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
         "count": len(facts), "facts": facts},
        indent=2, ensure_ascii=False,
    )


def _format_csv(facts):
    """Excel / spreadsheet workflows: CSV with key columns."""
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=["id","kind","importance","confidence",
                         "distinct_queries_hit","access_count","namespace","content",
                         "last_confirmed_at","tags"],
    )
    writer.writeheader()
    for f in facts:
        row = dict(f)
        row["content"] = row["content"].replace("\n", " ")[:500]
        row["tags"] = row.get("tags") or ""
        writer.writerow(row)
    return out.getvalue()


def _format_agent_context(facts):
    """Ready-to-paste system-prompt snippet (capped at 30 facts)."""
    out = ["# User Memory Context (paste below your system prompt)",
           "",
           "_Generated: " + datetime.now(timezone.utc).isoformat(timespec='seconds') + "Z_",
           "",
           "The following are facts this user has previously confirmed "
           "(importance >= 0.7 or hit by >= 3 distinct queries). "
           "Treat them as durable user context.",
           ""]
    for i, f in enumerate(facts[:30], 1):
        out.append(str(i) + ". **[" + f["kind"] + "]** " + f["content"][:300])
    out.append("")
    out.append("_End of memory context._")
    return "\n".join(out)


_FORMATTERS = {
    "markdown": ("md", _format_markdown),
    "json": ("json", _format_json),
    "csv": ("csv", _format_csv),
    "agent_context": ("txt", _format_agent_context),
}


def main():
    ap = argparse.ArgumentParser(description="Export astor-memory to multiple formats")
    ap.add_argument("--format", choices=list(_FORMATTERS) + ["all"], default="markdown")
    ap.add_argument("--astor-dir", default=None)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--tier", default="all")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--include-all", action="store_true")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    astor_dir = Path(args.astor_dir) if args.astor_dir else Path(
        r"D:/AI/Astor-Memory-Runtime" if sys.platform == "win32" else "~/.astor")
    user_db = astor_dir / "users" / args.user / "memory" / ("astor_bus_" + args.user + ".db")
    pub_db = astor_dir / "public" / "memory" / "astor_bus_public.db"
    if user_db.exists():
        db_path = user_db
    elif pub_db.exists():
        db_path = pub_db
    else:
        print("No astor DB found in " + str(astor_dir), file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    facts = _fetch_facts(conn, args.user if args.user else None, args.tier,
                        args.include_all, args.top)
    conn.close()

    formats = list(_FORMATTERS) if args.format == "all" else [args.format]
    if args.format == "all" and not args.output:
        args.output = str(astor_dir / "exports" /
                          datetime.now().strftime("mem_%Y%m%d_%H%M%S"))
    if args.output and args.format == "all":
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        for fmt in formats:
            ext, fn = _FORMATTERS[fmt]
            p = out_dir / ("memory." + ext)
            p.write_text(fn(facts), encoding="utf-8")
            print("wrote " + str(p))
    elif args.output:
        ext, fn = _FORMATTERS[args.format]
        Path(args.output).write_text(fn(facts), encoding="utf-8")
        print("wrote " + args.output)
    else:
        ext, fn = _FORMATTERS[args.format]
        sys.stdout.write(fn(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())