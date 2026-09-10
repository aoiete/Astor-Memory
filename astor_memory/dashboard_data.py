"""dashboard_data.py — Aggregate dashboard data for /v1/dashboard endpoint.

Aggregates 6 dimensions into a single JSON-ready dict:
1. Hero totals (users / facts / active / tombstoned / last_event / trend_status)
2. Eval harness trend (hit_rate / mrr / p95 latency, 30d window + variants)
3. Per-user fact breakdown (top 16)
4. 30d growth (admin daily promoted)
5. Top keywords + 5 recent facts (admin only — only user with data)
6. Health (embedding_failed / audit_warnings)

Pure Python stdlib + sqlite3. Zero external deps. Safe to import from server.

Usage:
    from astor_memory.dashboard_data import build_dashboard_payload
    payload = build_dashboard_payload(astor_dir="D:/AI/Astor-Memory-Runtime")
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _stats(seq) -> dict | None:
    """min/max/mean/n for a numeric sequence, ignoring None."""
    s = [x for x in seq if x is not None]
    if not s:
        return None
    return {
        "min": round(min(s), 4),
        "max": round(max(s), 4),
        "mean": round(sum(s) / len(s), 4),
        "n": len(s),
    }


def _safe_count(cu, sql: str) -> int:
    try:
        return int(cu.execute(sql).fetchone()[0])
    except Exception:
        return 0


def _summarize_embedding_failures(cu) -> dict:
    """Group embedding_failed records by error message + show timeline."""
    from collections import Counter
    import json as _json
    rows = cu.execute(
        "SELECT id, ts, target_id, metadata FROM audit_log "
        "WHERE event='embedding_failed' ORDER BY ts DESC"
    ).fetchall()
    errors: Counter = Counter()
    timeline: Counter = Counter()
    targets_with_no_replay: list[tuple[int, str, str | None]] = []
    for r in rows:
        try:
            meta = _json.loads(r[3]) if r[3] else {}
        except _json.JSONDecodeError:
            meta = {}
        err = meta.get("error", "(no error field)")
        errors[err] += 1
        timeline[r[1][:10]] += 1
        if not meta.get("queued_for_replay", False):
            targets_with_no_replay.append((r[0], r[1], r[2]))
    return {
        "total": len(rows),
        "errors": errors.most_common(),
        "by_day": sorted(timeline.items(), reverse=True)[:14],
        "no_replay_queued": len(targets_with_no_replay),
        "sample_targets_no_replay": targets_with_no_replay[:5],
    }


def _summarize_warnings(cu) -> dict:
    """Group audit warnings by event type."""
    from collections import Counter
    rows = cu.execute(
        "SELECT id, ts, event, target_id, reason FROM audit_log "
        "WHERE severity='warning' ORDER BY ts DESC"
    ).fetchall()
    by_event: Counter = Counter()
    by_day: Counter = Counter()
    samples: list[tuple] = []
    for r in rows:
        by_event[r[2]] += 1
        by_day[r[1][:10]] += 1
        samples.append(r)
    return {
        "total": len(rows),
        "by_event": by_event.most_common(),
        "by_day": sorted(by_day.items(), reverse=True)[:14],
        "samples": samples[:10],
    }


def _per_user_breakdown(astor_dir: Path) -> tuple[list[dict], str | None, int, int, int, int, int]:
    """Scan all users/<u>/memory/astor_bus_*.db, aggregate stats.

    Returns (per_user_list, last_event_ts, users_total, facts_total,
             active_total, tomb_total, high_imp_total).
    """
    users_dir = astor_dir / "users"
    per_user: list[dict] = []
    last_event_ts: str | None = None
    users_total = facts_total = active_total = tomb_total = high_imp_total = 0

    for user_dir in sorted(users_dir.glob("*/memory")):
        user = user_dir.parent.name
        dbs = list(user_dir.glob("astor_bus_*.db"))
        if not dbs:
            continue
        db = dbs[0]
        try:
            co = sqlite3.connect(str(db))
            cu = co.cursor()
            n = _safe_count(cu, "SELECT COUNT(*) FROM memory_canonical")
            t = _safe_count(cu, "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned = 1")
            h = _safe_count(cu, "SELECT COUNT(*) FROM memory_canonical WHERE importance >= 0.7 AND tombstoned = 0")
            try:
                le = cu.execute("SELECT MAX(ts) FROM events").fetchone()[0]
            except Exception:
                le = None
            co.close()
            a = n - t
            users_total += 1
            facts_total += n
            active_total += a
            tomb_total += t
            high_imp_total += h
            if le and (not last_event_ts or le > last_event_ts):
                last_event_ts = le
            per_user.append({
                "user": user,
                "facts": n,
                "active": a,
                "tombstoned": t,
                "high_imp": h,
                "last_event": le,
            })
        except Exception:
            continue

    per_user.sort(key=lambda x: -x["active"])
    return per_user, last_event_ts, users_total, facts_total, active_total, tomb_total, high_imp_total


def _eval_trend(metrics_dir: Path) -> dict:
    """Read eval_history.jsonl, compute 30d window stats + variant latest."""
    out: dict[str, Any] = {
        "last_run_ts": None,
        "last_run_variant": None,
        "last_run_hit_rate": None,
        "last_run_mrr": None,
        "last_run_p95_ms": None,
        "window_30d_baselines": {"n": 0, "hit_rate": None, "mrr": None, "p95_latency_ms": None},
        "all_variants_last": {},
        "trend_status": "no_data",
    }
    hist_path = metrics_dir / "eval_history.jsonl"
    if not hist_path.exists():
        return out
    history: list[dict] = []
    for line in hist_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            history.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if history:
        last = history[-1]
        out["last_run_ts"] = last.get("ts")
        out["last_run_variant"] = last.get("variant")
        out["last_run_hit_rate"] = last.get("hit_rate_at_k")
        out["last_run_mrr"] = last.get("mrr")
        out["last_run_p95_ms"] = last.get("p95_latency_ms")

    baselines = [h for h in history if h.get("variant") == "baseline"]
    recent = baselines[-30:]
    if recent:
        out["window_30d_baselines"] = {
            "n": len(recent),
            "hit_rate": _stats(h.get("hit_rate_at_k") for h in recent),
            "mrr": _stats(h.get("mrr") for h in recent),
            "p95_latency_ms": _stats(h.get("p95_latency_ms") for h in recent),
        }
        if len(recent) >= 2:
            cur, earl = recent[-1], recent[0]
            out["delta"] = {
                "mrr": round(cur["mrr"] - earl["mrr"], 4),
                "hit_rate": round(cur["hit_rate_at_k"] - earl["hit_rate_at_k"], 4),
            }
            md = out["delta"]["mrr"]
            out["trend_status"] = "improving" if md > 0.02 else "regressing" if md < -0.02 else "stable"

    # Latest of each variant
    seen: dict[str, dict] = {}
    for h in reversed(history):
        v = h.get("variant")
        if v and v not in seen:
            seen[v] = h
    out["all_variants_last"] = {
        v: {
            "hit_rate": h.get("hit_rate_at_k"),
            "mrr": h.get("mrr"),
            "p95_ms": h.get("p95_latency_ms"),
            "ts": h.get("ts"),
        }
        for v, h in seen.items()
    }
    return out


def _growth_30d(astor_dir: Path) -> dict[str, int]:
    """Admin daily promoted count for the past 30 days."""
    admin_db = astor_dir / "users/admin/memory/astor_bus_admin.db"
    if not admin_db.exists():
        return {}
    try:
        co = sqlite3.connect(str(admin_db))
        cu = co.cursor()
        rows = cu.execute(
            "SELECT substr(promoted_at,1,10) d, COUNT(*) FROM memory_canonical GROUP BY d ORDER BY d"
        ).fetchall()
        co.close()
        return {d: c for d, c in rows}
    except Exception:
        return {}


def _top_keywords_and_recent(astor_dir: Path) -> tuple[list[tuple[str, int]], list[dict]]:
    """Top 20 keywords (from canonical.keywords JSON column) + 5 recent facts.

    Falls back to Counter over tags if keywords are empty.
    """
    admin_db = astor_dir / "users/admin/memory/astor_bus_admin.db"
    if not admin_db.exists():
        return [], []
    keywords: Counter = Counter()
    recent: list[dict] = []
    try:
        co = sqlite3.connect(str(admin_db))
        cu = co.cursor()
        # Try keywords first (JSON list)
        try:
            rows = cu.execute(
                "SELECT keywords FROM memory_canonical WHERE keywords != '[]' AND keywords != '' AND tombstoned = 0 LIMIT 5000"
            ).fetchall()
            for (kw_json,) in rows:
                try:
                    kws = json.loads(kw_json)
                    if isinstance(kws, list):
                        keywords.update(kws)
                except json.JSONDecodeError:
                    continue
        except sqlite3.OperationalError:
            pass
        # Fallback: tags
        if not keywords:
            try:
                rows = cu.execute(
                    "SELECT tags FROM memory_canonical WHERE tags != '[]' AND tombstoned = 0 LIMIT 5000"
                ).fetchall()
                for (tag_json,) in rows:
                    try:
                        keywords.update(json.loads(tag_json))
                    except json.JSONDecodeError:
                        continue
            except sqlite3.OperationalError:
                pass
        # Recent facts
        try:
            rows = cu.execute(
                "SELECT id, content, importance, promoted_at FROM memory_canonical "
                "WHERE tombstoned = 0 ORDER BY promoted_at DESC LIMIT 5"
            ).fetchall()
            recent = [
                {"id": r[0], "content": r[1][:140], "importance": r[2], "ts": r[3]}
                for r in rows
            ]
        except sqlite3.OperationalError:
            recent = []
        co.close()
    except Exception:
        return [], []
    return keywords.most_common(20), recent


def _importance_histogram(astor_dir: Path) -> dict[str, int]:
    """Bucket canonical.importance into critical/high/mid/low for visual histogram."""
    admin_db = astor_dir / "users/admin/memory/astor_bus_admin.db"
    if not admin_db.exists():
        return {"critical": 0, "high": 0, "mid": 0, "low": 0}
    try:
        co = sqlite3.connect(str(admin_db))
        cu = co.cursor()
        rows = cu.execute(
            "SELECT "
            "  SUM(CASE WHEN importance >= 0.9 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN importance >= 0.7 AND importance < 0.9 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN importance >= 0.5 AND importance < 0.7 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN importance < 0.5 THEN 1 ELSE 0 END) "
            "FROM memory_canonical WHERE tombstoned = 0"
        ).fetchone()
        co.close()
        return {
            "critical (>=0.9)": int(rows[0] or 0),
            "high (0.7-0.9)": int(rows[1] or 0),
            "mid (0.5-0.7)": int(rows[2] or 0),
            "low (<0.5)": int(rows[3] or 0),
        }
    except Exception:
        return {"critical": 0, "high": 0, "mid": 0, "low": 0}


def _health(astor_dir: Path) -> dict[str, int]:
    """Embedding failures + audit warnings + audit total (admin only)."""
    admin_db = astor_dir / "users/admin/memory/astor_bus_admin.db"
    if not admin_db.exists():
        return {"embedding_failed": 0, "audit_warnings": 0, "audit_total": 0}
    try:
        co = sqlite3.connect(str(admin_db))
        cu = co.cursor()
        ef = _safe_count(cu, "SELECT COUNT(*) FROM audit_log WHERE event = 'embedding_failed'")
        aw = _safe_count(cu, "SELECT COUNT(*) FROM audit_log WHERE severity = 'warning'")
        at = _safe_count(cu, "SELECT COUNT(*) FROM audit_log")
        co.close()
        return {"embedding_failed": ef, "audit_warnings": aw, "audit_total": at}
    except Exception:
        return {"embedding_failed": 0, "audit_warnings": 0, "audit_total": 0}


def build_dashboard_payload(astor_dir: str | Path) -> dict:
    """Aggregate the 6 dashboard dimensions into a single JSON-ready dict.

    Args:
        astor_dir: Path to Astor-Memory-Runtime root (contains users/ + public/).

    Returns:
        Dict with keys: generated_at, hero, eval_trend, per_user, growth_30d,
        top_keywords, recent_facts, importance_histogram, health.
    """
    astor = Path(astor_dir)
    metrics_dir = astor.parent / "astor-memory" / "astor" / "metrics"
    if not metrics_dir.exists():
        # fallback: astor/metrics relative to astor_dir
        metrics_dir = astor / "astor" / "metrics"

    per_user, last_event_ts, users_total, facts_total, active_total, tomb_total, high_imp_total = (
        _per_user_breakdown(astor)
    )
    eval_trend = _eval_trend(metrics_dir)
    growth = _growth_30d(astor)
    keywords, recent = _top_keywords_and_recent(astor)
    importance_hist = _importance_histogram(astor)
    health = _health(astor)

    delta_min: float | None = None
    if last_event_ts:
        try:
            ts_clean = last_event_ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_clean)
            delta_min = round((datetime.now(timezone.utc) - dt).total_seconds() / 60, 1)
        except Exception:
            delta_min = None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hero": {
            "total_users": users_total,
            "total_facts": facts_total,
            "active_facts": active_total,
            "tombstoned": tomb_total,
            "high_importance": high_imp_total,
            "last_event_ts": last_event_ts,
            "last_event_minutes_ago": delta_min,
            "trend_status": eval_trend.get("trend_status", "unknown"),
        },
        "eval_trend": eval_trend,
        "per_user": per_user,
        "growth_30d": growth,
        "top_keywords": [{"keyword": k, "count": c} for k, c in keywords],
        "recent_facts": recent,
        "importance_histogram": importance_hist,
        "health": health,
    }


if __name__ == "__main__":
    # CLI: python -m astor_memory.dashboard_data [astor_dir]
    import sys
    astor_dir = sys.argv[1] if len(sys.argv) > 1 else "D:/AI/Astor-Memory-Runtime"
    payload = build_dashboard_payload(astor_dir)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
