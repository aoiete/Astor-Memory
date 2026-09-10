"""astor_initial_report.py — Memmy-style "初见报告" for any astor user.

Outputs a markdown summary:
  - Top N high-importance facts (grouped by namespace + kind)
  - Last M events
  - Coverage stats (active facts, namespaces, recent activity)

Usage:
  python astor_initial_report.py [--user admin] [--tier private] [--top-facts 20] [--last-events 15] [--days 30] [--out REPORT.md]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ANSI colors (skip on Windows non-tty)
try:
    import colorama
except ImportError:
    colorama = None


def _resolve_db(tier: str, user_id: str | None) -> Path:
    """Resolve astor bus DB path from ASTOR_DIR + tier + user."""
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


def _format_ts(ts: str) -> str:
    if not ts:
        return "(no ts)"
    try:
        # Handle invalid second=65 (which ISO accepts loosely but renders ugly)
        if "." in ts:
            # Normalize microsecond overflow
            base, _, frac = ts.partition(".")
            frac_clean = "".join(c for c in frac if c.isdigit())[:6]
            if frac_clean:
                # Try parse then reformat
                dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
                frac_val = int(frac_clean[:6].ljust(6, "0"))
                if frac_val >= 1_000_000:
                    # Roll over (e.g. 999999us → 1s extra)
                    from datetime import timedelta
                    dt = dt + timedelta(seconds=frac_val // 1_000_000)
                    frac_val = frac_val % 1_000_000
                ts_clean = base + "." + f"{frac_val:06d}"
                if base.endswith("Z"):
                    ts_clean = ts_clean[:-1] + "+00:00" if frac_val else base
                dt = datetime.fromisoformat(ts_clean)
            else:
                dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days > 30:
            return dt.strftime("%Y-%m-%d")
        if delta.days > 0:
            return f"{delta.days}d ago ({dt.strftime('%Y-%m-%d')})"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        return f"{minutes}m ago" if minutes > 0 else "just now"
    except Exception:
        return ts[:19]


def build_report(
    user_id: str = "admin",
    tier: str = "private",
    top_facts: int = 20,
    last_events: int = 15,
    days: int = 30,
    db_path: Path | None = None,
) -> str:
    """Build the markdown report. Returns a single string."""
    if db_path is None:
        db_path = _resolve_db(tier, user_id)
    if not db_path.exists():
        return f"# Astor Initial Report\n\nDB not found: {db_path}\n"

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # 1) Coverage stats
    total_active = con.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0"
    ).fetchone()[0]
    total_tombstoned = con.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=1"
    ).fetchone()[0]
    by_kind = con.execute("""
        SELECT kind, COUNT(*) AS n
        FROM memory_canonical
        WHERE tombstoned=0
        GROUP BY kind
        ORDER BY n DESC
    """).fetchall()
    by_namespace = con.execute("""
        SELECT namespace, COUNT(*) AS n
        FROM memory_canonical
        WHERE tombstoned=0
        GROUP BY namespace
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent_count = con.execute("""
        SELECT COUNT(*) FROM memory_canonical
        WHERE tombstoned=0 AND promoted_at >= ?
    """, (since,)).fetchone()[0]

    # 2) Top facts (by importance)
    top = con.execute("""
        SELECT id, namespace, kind, importance, content, promoted_at
        FROM memory_canonical
        WHERE tombstoned=0
        ORDER BY importance DESC, promoted_at DESC
        LIMIT ?
    """, (top_facts,)).fetchall()

    # 3) Recent events
    events = con.execute("""
        SELECT id, ts, action, content, source
        FROM events
        ORDER BY id DESC
        LIMIT ?
    """, (last_events,)).fetchall()

    # 4) Group top facts by kind for cleaner display
    by_kind_groups: dict[str, list] = {}
    for row in top:
        by_kind_groups.setdefault(row["kind"] or "fact", []).append(row)

    # Render markdown
    out = []
    out.append(f"# Astor Initial Report — {user_id} ({tier} tier)")
    out.append("")
    out.append(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    if len(db_path.parts) > 3:
        rel = db_path.relative_to(db_path.parents[3])
        out.append(f"_DB: `{rel}`_")
    else:
        out.append(f"_DB: `{db_path}`_")
    out.append("")

    out.append("## Coverage")
    out.append("")
    out.append(f"- **Active facts**: {total_active:,}")
    out.append(f"- **Tombstoned facts**: {total_tombstoned:,}")
    out.append(f"- **Facts promoted in last {days}d**: {recent_count:,}")
    if total_active:
        out.append(f"- **Top kinds**: " + ", ".join(
            f"`{r['kind']}`={r['n']}" for r in by_kind[:5]
        ))
        out.append(f"- **Top namespaces**: " + ", ".join(
            f"`{r['namespace']}`={r['n']}" for r in by_namespace[:5]
        ))
    out.append("")

    out.append(f"## Top {top_facts} Facts (by importance)")
    out.append("")
    for kind, rows in by_kind_groups.items():
        out.append(f"### kind=`{kind}` ({len(rows)})")
        out.append("")
        for r in rows:
            snippet = r["content"].replace("\n", " ").strip()
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            out.append(f"- **[#{r['id']}]** `{_format_ts(r['promoted_at'])}` imp={r['importance']:.2f}")
            out.append(f"  > {snippet}")
        out.append("")

    out.append(f"## Last {last_events} Events")
    out.append("")
    for e in events:
        snippet = (e["content"] or "").replace("\n", " ").strip()
        if len(snippet) > 150:
            snippet = snippet[:147] + "..."
        out.append(f"- **[evt #{e['id']}]** `{_format_ts(e['ts'])}` {e['action']} `src={e['source']}`")
        if snippet:
            out.append(f"  > {snippet}")
    out.append("")

    out.append("---")
    out.append("_Generated by `scripts/astor_initial_report.py` (Memmy-style onboarding) — ship 2026-09-09_")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--tier", default="private", choices=["public", "source", "private"])
    ap.add_argument("--top-facts", type=int, default=20)
    ap.add_argument("--last-events", type=int, default=15)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", help="Output file (default stdout)")
    ap.add_argument("--db", help="Override DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None
    report = build_report(
        user_id=args.user,
        tier=args.tier,
        top_facts=args.top_facts,
        last_events=args.last_events,
        days=args.days,
        db_path=db_path,
    )
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
