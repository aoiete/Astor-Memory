"""astor_importance_audit.py — boost importance for R-class / rule content.

Many astor facts default to importance=0.5. This tool scans all facts,
detects "rule-like" content (R-class, locked, 教训, lesson, etc.), and
proposes importance boosts so the classifier can find them.

Output:
- Markdown report of proposed boosts (default: --dry-run)
- JSON sidecar for downstream pipeline

Usage:
  python astor_importance_audit.py --user admin --tier private [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# Importance boosts by content signal (signal → target importance)
BOOST_SIGNALS: list[tuple[re.Pattern, float, str]] = [
    # Hard rules / R-class canonical
    (re.compile(r"^[\s【]*R-class\s+(canonical|locked)", re.IGNORECASE), 0.95, "R-class canonical/locked"),
    (re.compile(r"\bR(\d{3,4})\s*\(locked\s+\d{4}-\d{2}-\d{2}"), 0.95, "R### (locked YYYY-MM-DD)"),
    (re.compile(r"\[LESSON\]|\[教训\]"), 0.85, "[LESSON]/[教训] prefix"),
    (re.compile(r"(教训|takeaway|经验教训)"), 0.7, "教训/takeaway"),
    # User preferences (strong signal)
    (re.compile(r"用户(从不|总是|经常|不要|应该)"), 0.7, "user habit statement"),
    # Iron rules
    (re.compile(r"Iron\s*Rule\s*\d+", re.IGNORECASE), 0.85, "Iron Rule N"),
    (re.compile(r"跨账户铁律|Cross-Account|acc_id\s+must"), 0.85, "cross-account iron rule"),
    # Tombstoned (audit/recall prevention)
    (re.compile(r"\b(tombstone|撤回.*silent drop|撤回.*错判)"), 0.8, "tombstone audit fact"),
    # Verified lock patterns
    (re.compile(r"\(verified\)\s*$", re.IGNORECASE), 0.7, "ends with (verified)"),
    (re.compile(r"\(locked\s+\d{4}-\d{2}-\d{2}"), 0.9, "(locked YYYY-MM-DD)"),
]


def suggest_boost(content: str) -> tuple[float | None, str | None]:
    """Return (target_importance, rationale) for first matching signal."""
    for pat, target, rationale in BOOST_SIGNALS:
        if pat.search(content or ""):
            return target, rationale
    return None, None


def _resolve_db(tier: str, user_id: str | None) -> Path:
    import os
    astor_dir = os.environ.get("ASTOR_DIR", r"D:\AI\Astor-Memory-Runtime")
    base = Path(astor_dir)
    if tier == "public":
        return base / "public" / "memory" / "astor_bus_public.db"
    if tier == "source":
        return base / "source" / "memory" / "astor_bus_source.db"
    if tier == "private":
        if not user_id:
            raise ValueError("private tier requires --user")
        return base / "users" / user_id / "memory" / f"astor_bus_{user_id}.db"
    raise ValueError(f"unknown tier: {tier}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--tier", default="private", choices=["public", "source", "private"])
    ap.add_argument("--dry-run", action="store_true", help="show plan, do not update DB")
    ap.add_argument("--out", help="output JSON file (default stdout)")
    ap.add_argument("--report", help="output markdown report file")
    ap.add_argument("--db", help="override DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else _resolve_db(args.tier, args.user)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # Pull all active facts
    rows = con.execute("""
        SELECT id, content, importance, kind
        FROM memory_canonical
        WHERE tombstoned=0
        ORDER BY id
    """).fetchall()

    # Find boost candidates
    boosts = []
    for r in rows:
        target, rationale = suggest_boost(r["content"] or "")
        if target is None:
            continue
        current = r["importance"] or 0.0
        if current < target:  # only boost, don't downgrade
            boosts.append({
                "id": r["id"],
                "current_importance": round(current, 3),
                "new_importance": target,
                "kind": r["kind"],
                "rationale": rationale,
                "snippet": (r["content"] or "")[:120].replace("\n", " "),
            })

    # Distribution summary
    from collections import Counter
    rationale_counts = Counter(b["rationale"] for b in boosts)
    by_target = Counter(b["new_importance"] for b in boosts)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db_path),
        "facts_scanned": len(rows),
        "boosts_proposed": len(boosts),
        "rationale_distribution": dict(rationale_counts.most_common()),
        "target_distribution": dict(by_target.most_common()),
        "boosts": boosts,
    }

    out_str = json.dumps(result, indent=1, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"JSON report written to {args.out}", file=sys.stderr)
    else:
        print(out_str)

    # Markdown report
    md_lines = [
        f"# Astor Importance Audit — {args.user} ({args.tier} tier)",
        "",
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Summary",
        "",
        f"- **Facts scanned**: {len(rows):,}",
        f"- **Boosts proposed**: {len(boosts):,}",
        f"- **Avg boost**: +{sum(b['new_importance'] - b['current_importance'] for b in boosts) / len(boosts):.3f}" if boosts else "- (none)",
        "",
        "## Rationale distribution",
        "",
    ]
    for rationale, n in rationale_counts.most_common():
        md_lines.append(f"- `{rationale}`: {n}")
    md_lines.append("")
    md_lines.append("## Target distribution")
    md_lines.append("")
    for target, n in by_target.most_common():
        md_lines.append(f"- importance={target}: {n}")
    md_lines.append("")
    md_lines.append(f"## Boosts (first 30)")
    md_lines.append("")
    for b in boosts[:30]:
        md_lines.append(f"- **[#{b['id']}]** {b['current_importance']:.2f} → {b['new_importance']:.2f} ({b['rationale']})")
        md_lines.append(f"  > {b['snippet']}")
    md_lines.append("")

    md_str = "\n".join(md_lines)
    if args.report:
        Path(args.report).write_text(md_str, encoding="utf-8")
        print(f"Markdown report written to {args.report}", file=sys.stderr)

    # Apply if not dry-run
    if not args.dry_run and boosts:
        print(f"\nApplying {len(boosts)} importance boosts...", file=sys.stderr)
        for b in boosts:
            con.execute(
                "UPDATE memory_canonical SET importance=? WHERE id=?",
                (b["new_importance"], b["id"])
            )
        con.commit()
        print("Done.", file=sys.stderr)

    con.close()


if __name__ == "__main__":
    main()
