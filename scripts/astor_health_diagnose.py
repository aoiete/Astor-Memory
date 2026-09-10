"""astor_health_diagnose.py — diagnose dashboard "health" counters.

Reads the same audit_log the dashboard samples, and explains what each counter
means + lists the underlying records.

Two modes:
  --summary        (default)  show counts + diagnosis for each metric
  --embedding       list the embedding_failed records (default first 20)
  --warnings        list audit warnings (default first 20)
  --explain <event> explain a specific audit event type

Usage:
  python scripts/astor_health_diagnose.py
  python scripts/astor_health_diagnose.py --embedding --limit 50
  python scripts/astor_health_diagnose.py --warnings --limit 50
  python scripts/astor_health_diagnose.py --explain embedding_failed
  python scripts/astor_health_diagnose.py --user admin --astor-dir <path>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _open_bus(user: str, astor_dir: str | None) -> tuple[sqlite3.Connection, str]:
    """Open the bus db for the given user. Return (conn, db_path)."""
    if astor_dir:
        base = Path(astor_dir)
    else:
        # Default: sibling layout of Astor-Memory-Runtime
        base = Path(r"D:/AI/Astor-Memory-Runtime")
    db = base / "users" / user / "memory" / f"astor_bus_{user}.db"
    if not db.exists():
        raise FileNotFoundError(f"DB not found: {db}")
    return sqlite3.connect(str(db)), str(db)


def _safe_count(cu, sql: str, *args) -> int:
    try:
        return int(cu.execute(sql, args).fetchone()[0])
    except Exception:
        return 0


def _summarize_embedding_failures(cu) -> dict:
    """Group embedding_failed records by error message + show timeline.

    Re-exported wrapper — actual logic lives in astor_memory.dashboard_data
    so the same code paths back both the CLI and the /v1/health/diagnose
    endpoint.
    """
    from astor_memory.dashboard_data import _summarize_embedding_failures as _impl
    return _impl(cu)


def _summarize_warnings(cu) -> dict:
    """Group audit warnings by event type.

    Re-exported wrapper — see _summarize_embedding_failures note.
    """
    from astor_memory.dashboard_data import _summarize_warnings as _impl
    return _impl(cu)


# Heuristic explanations for common audit events.
# Maps event → (category, what_it_means, severity_label)
EVENT_EXPLAIN: dict[str, tuple[str, str, str]] = {
    "embedding_failed": (
        "embedding",
        "Vector embedding generation failed (typically a transient API or "
        "vector_store connection error). The fact is queued for replay. "
        "Counts > 0 here are normal during outages; check whether they keep "
        "climbing (indicates active failure) or plateau (indicates the queue is draining).",
        "info-or-warn",
    ),
    "forget": (
        "user_action",
        "Operator (or upstream rule) called /v1/forget to remove a fact. "
        "Logged with severity=warning because forgetting is destructive. "
        "NOT an error — this is the audit trail of intentional deletions.",
        "info",
    ),
    "auto_link": (
        "system",
        "Zettelkasten-style auto-linker ran on a new fact. Severity=info. "
        "No action needed.",
        "info",
    ),
    "promote_candidate": (
        "system",
        "Memory_candidate promoted to memory_canonical. Severity=info.",
        "info",
    ),
}


def explain_event(event: str) -> str:
    """Return a human-readable explanation of an audit event type."""
    if event in EVENT_EXPLAIN:
        cat, desc, sev = EVENT_EXPLAIN[event]
        return f"event: {event}\n  category: {cat}\n  severity: {sev}\n  {desc}"
    # Generic fallback
    return (
        f"event: {event}\n  No detailed explanation registered. "
        "See astor_memory/audit_log schema (severity, reason, metadata fields) "
        "for raw context."
    )


def show_summary(user: str, astor_dir: str | None) -> int:
    con, db_path = _open_bus(user, astor_dir)
    cu = con.cursor()
    emb = _summarize_embedding_failures(cu)
    warn = _summarize_warnings(cu)
    total = _safe_count(cu, "SELECT COUNT(*) FROM audit_log")
    con.close()

    print(f"=== Astor Health Diagnosis — user={user}  db={db_path} ===\n")

    # Embedding
    print("--- Embedding Failed ---")
    print(f"Total: {emb['total']}")
    if emb["total"] > 0:
        print("Error breakdown:")
        for err, n in emb["errors"][:5]:
            print(f"  [{n:3d}] {err[:90]}")
        if emb["by_day"]:
            print("Daily trend (last 14):")
            for d, n in emb["by_day"]:
                bar = "█" * min(n, 30)
                print(f"  {d}  {n:3d}  {bar}")
        if emb["no_replay_queued"] == 0:
            print("All queued for replay — queue is draining ✓")
        else:
            print(f"⚠ {emb['no_replay_queued']} NOT queued for replay:")
            for aid, ts, tgt in emb["sample_targets_no_replay"]:
                print(f"    audit_id={aid}  ts={ts}  fact_id={tgt}")
    else:
        print("  None ✓")
    print()

    # Warnings
    print("--- Audit Warnings ---")
    print(f"Total: {warn['total']}")
    if warn["total"] > 0:
        print("By event:")
        for ev, n in warn["by_event"]:
            print(f"  [{n:3d}] {ev}")
        if warn["by_day"]:
            print("Daily trend (last 14):")
            for d, n in warn["by_day"]:
                print(f"  {d}  {n:3d}")
        print("\nSamples:")
        for s in warn["samples"]:
            print(f"  id={s[0]:5d}  {s[1]}  event={s[2]:20s}  target={s[3]!s:6s}  reason={(s[4] or '')[:80]}")
    else:
        print("  None ✓")
    print()

    # Total
    print(f"--- Audit Log Total: {total:,} ---")
    con = sqlite3.connect(db_path)
    cu = con.cursor()
    print("By severity:")
    for r in cu.execute(
        "SELECT severity, COUNT(*) FROM audit_log GROUP BY severity ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {r[0]:10s}  {r[1]}")
    con.close()
    return 0


def show_embedding_records(user: str, astor_dir: str | None, limit: int) -> int:
    con, db_path = _open_bus(user, astor_dir)
    cu = con.cursor()
    print(f"=== Embedding Failed records ({limit}) — user={user} ===\n")
    rows = cu.execute(
        "SELECT id, ts, target_id, metadata FROM audit_log "
        "WHERE event='embedding_failed' ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("  None ✓")
    for r in rows:
        try:
            meta = json.loads(r[3]) if r[3] else {}
        except json.JSONDecodeError:
            meta = {}
        err = meta.get("error", "(no error)")
        replay = meta.get("queued_for_replay", False)
        print(f"  id={r[0]:5d}  {r[1]}  fact_id={r[2]!s:6s}  replay={replay}")
        print(f"    error: {err[:120]}")
    con.close()
    return 0


def show_warning_records(user: str, astor_dir: str | None, limit: int) -> int:
    con, db_path = _open_bus(user, astor_dir)
    cu = con.cursor()
    print(f"=== Audit Warnings ({limit}) — user={user} ===\n")
    rows = cu.execute(
        "SELECT id, ts, event, target_id, reason FROM audit_log "
        "WHERE severity='warning' ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("  None ✓")
    for r in rows:
        print(f"  id={r[0]:5d}  {r[1]}  event={r[2]:18s}  target={r[3]!s:6s}")
        print(f"    reason: {(r[4] or '')[:120]}")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diagnose astor dashboard health counters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--user", default="admin", help="user to diagnose (default admin)")
    ap.add_argument("--astor-dir", help="ASTOR runtime root (default D:/AI/Astor-Memory-Runtime)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--summary", action="store_true", default=True,
                     help="show summary (default)")
    mode.add_argument("--embedding", action="store_true",
                     help="list embedding_failed records")
    mode.add_argument("--warnings", action="store_true",
                     help="list audit warnings")
    mode.add_argument("--explain", metavar="EVENT",
                     help="explain a specific audit event type")
    ap.add_argument("--limit", type=int, default=20,
                     help="limit list output (default 20)")
    args = ap.parse_args()

    if args.explain:
        print(explain_event(args.explain))
        return 0
    if args.embedding:
        return show_embedding_records(args.user, args.astor_dir, args.limit)
    if args.warnings:
        return show_warning_records(args.user, args.astor_dir, args.limit)
    return show_summary(args.user, args.astor_dir)


if __name__ == "__main__":
    sys.exit(main())
