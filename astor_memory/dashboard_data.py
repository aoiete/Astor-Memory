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
from datetime import datetime, timezone, timedelta
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


def _entities_coverage(astor_dir: Path) -> dict:
    """v1.14.25 Ship G (2026-09-15): aggregate entities_json coverage across
    all bus DBs (public + source + per-user private). Reports total facts,
    facts with non-empty entities_json, coverage ratio, and per-tier breakdown.

    Cheap: one COUNT query per DB. JSON1 json_array_length is O(1) per row.
    """
    total_facts = 0
    facts_with_entities = 0
    per_tier: dict[str, dict] = {}
    # Public + source DBs
    for tier in ('public', 'source'):
        db_path = astor_dir / tier / 'memory' / f'astor_bus_{tier}.db'
        if not db_path.exists():
            continue
        try:
            co = sqlite3.connect(str(db_path))
            cu = co.cursor()
            # Check schema version
            cols = {row[1] for row in cu.execute(
                "PRAGMA table_info(memory_canonical)"
            ).fetchall()}
            if 'entities_json' not in cols:
                co.close()
                continue
            n = _safe_count(cu,
                "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned = 0")
            ne = _safe_count(cu,
                "SELECT COUNT(*) FROM memory_canonical "
                "WHERE tombstoned = 0 AND json_array_length(entities_json) > 0")
            total_facts += n
            facts_with_entities += ne
            per_tier[tier] = {'total': n, 'with_entities': ne,
                              'coverage': round(ne / n, 4) if n > 0 else 0.0}
            co.close()
        except Exception:
            continue
    # Per-user private DBs (aggregate under 'private' bucket)
    private_total = private_with = 0
    users_dir = astor_dir / 'users'
    if users_dir.exists():
        for user_dir in sorted(users_dir.glob('*/memory')):
            dbs = list(user_dir.glob('astor_bus_*.db'))
            for db in dbs:
                try:
                    co = sqlite3.connect(str(db))
                    cu = co.cursor()
                    cols = {row[1] for row in cu.execute(
                        "PRAGMA table_info(memory_canonical)"
                    ).fetchall()}
                    if 'entities_json' not in cols:
                        co.close()
                        continue
                    n = _safe_count(cu,
                        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned = 0")
                    ne = _safe_count(cu,
                        "SELECT COUNT(*) FROM memory_canonical "
                        "WHERE tombstoned = 0 AND json_array_length(entities_json) > 0")
                    private_total += n
                    private_with += ne
                    co.close()
                except Exception:
                    continue
    if private_total > 0:
        per_tier['private'] = {
            'total': private_total,
            'with_entities': private_with,
            'coverage': round(private_with / private_total, 4),
        }
        total_facts += private_total
        facts_with_entities += private_with
    return {
        'total_facts': total_facts,
        'facts_with_entities': facts_with_entities,
        'coverage_ratio': round(facts_with_entities / total_facts, 4) if total_facts > 0 else 0.0,
        'per_tier': per_tier,
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
        # S21 (2026-09-15): snapshot is median of last 3 baselines (not all-time max),
        # so a single hot-cache outlier doesn't poison the comparison baseline.
        # Matches tests/eval_trend.py and astor/metrics/last_good_snapshot.json.
        snap_window = baselines[-3:]
        snap_hr = sorted(h.get("hit_rate_at_k", 0) for h in snap_window)[len(snap_window) // 2]
        snap_mrr = sorted(h.get("mrr", 0) for h in snap_window)[len(snap_window) // 2]
        snap_ts = snap_window[-1].get("ts")
        out["snapshot"] = {
            "hit_rate_at_k": snap_hr,
            "mrr": snap_mrr,
            "ts": snap_ts,
            "rationale": "dashboard median of last 3 baselines (matches tests/eval_trend.py)",
        }
        if len(recent) >= 2:
            cur = recent[-1]
            # S21: prefer delta_vs_snapshot over window-earliest (which was misleading)
            cur_hr = cur.get("hit_rate_at_k", 0)
            cur_mrr = cur.get("mrr", 0)
            d_hr = cur_hr - snap_hr
            d_mrr = cur_mrr - snap_mrr
            out["delta_vs_snapshot"] = {
                "hit_rate": round(d_hr, 4),
                "mrr": round(d_mrr, 4),
            }
            # Cold-cache variance: last-2 delta ≥3pp = warmup/regression signal.
            if len(baselines) >= 2:
                last_two = baselines[-2:]
                cc_delta = last_two[1]["hit_rate_at_k"] - last_two[0]["hit_rate_at_k"]
                out["cold_cache_variance"] = {
                    "detected": abs(cc_delta) >= 0.03,
                    "delta": round(cc_delta, 4),
                    "interpretation": ("cold_cache_warmup" if cc_delta > 0.03
                                       else "warm_cache_regression" if cc_delta < -0.03
                                       else "stable"),
                }
            else:
                out["cold_cache_variance"] = {"detected": False, "reason": "need >=2 baselines"}
            # Trend logic — within 5pp of snapshot = stable, even if negative.
            if d_hr >= -0.05:
                out["trend_status"] = "stable"
            elif out["cold_cache_variance"].get("detected") and \
                    out["cold_cache_variance"].get("interpretation") == "cold_cache_warmup":
                out["trend_status"] = "cold_cache"
            else:
                out["trend_status"] = "regressing"
            # Legacy window-earliest delta (kept for backward compat in API).
            earl = recent[0]
            out["delta"] = {
                "mrr": round(cur["mrr"] - earl["mrr"], 4),
                "hit_rate": round(cur["hit_rate_at_k"] - earl["hit_rate_at_k"], 4),
            }

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


def _recent_capture(astor_dir, limit: int = 10) -> dict:
    """v1.14.37 Recent Capture panel — facts grouped by 3 axes for the dashboard.

    Returns 3 buckets each capped at `limit` rows:
      - by_kind: success_pattern / failure_pattern / lesson / user_preference / other
      - by_tier: public / private / source
      - by_platform: discord / telegram / wechat / cron / manual / other
        (platform inferred from origin_session_id prefix: 'discord:' / 'telegram:'
         / 'wechat:' / 'cron:' / cli/manual → 'manual')

    Each row: {id, content[140ch], kind, tier, namespace, ts, importance,
    origin_session_id, provenance_kind, provenance_agent}

    Tier sources:
      - admin private: users/admin/memory/astor_bus_admin.db
      - source tier:   public/memory/astor_bus_source.db (admin writes from R&D)
      - public tier:   public/memory/astor_bus_public.db (shared cross-user)
    """
    astor_dir = Path(astor_dir)
    admin_db = astor_dir / "users/admin/memory/astor_bus_admin.db"
    source_db = astor_dir / "public/memory/astor_bus_source.db"
    public_db = astor_dir / "public/memory/astor_bus_public.db"

    by_kind: dict[str, list[dict]] = {}
    by_tier: dict[str, list[dict]] = {}
    by_platform: dict[str, list[dict]] = {}

    def _platform_from_session(sid: str | None) -> str:
        if not sid:
            return "manual"
        s = sid.lower()
        # Match exact prefix + dash (e.g. "telegram-..." without colon) AND colon form.
        # Order matters: more specific first (wechat/weixin before generic checks).
        for prefix in ("discord:", "dc:", "discord-", "dc-"):
            if s.startswith(prefix):
                return "discord"
        for prefix in ("telegram:", "tg:", "telegram-", "tg-"):
            if s.startswith(prefix):
                return "telegram"
        for prefix in ("wechat:", "wx:", "weixin:", "wechat-", "wx-", "weixin-"):
            if s.startswith(prefix):
                return "wechat"
        for prefix in ("cron:", "cron-", "hermes-cron", "schedule-"):
            if s.startswith(prefix):
                return "cron"
        for prefix in ("cli:", "manual:", "agent:", "cli-", "manual-", "agent-"):
            if s.startswith(prefix):
                return "manual"
        return "other"

    def _scan(db_path: Path, tier_label: str):
        if not db_path.exists():
            return
        try:
            co = sqlite3.connect(str(db_path))
            cu = co.cursor()
            rows = cu.execute(
                "SELECT id, content, kind, importance, namespace, promoted_at, "
                "origin_session_id, provenance_kind, provenance_agent "
                "FROM memory_canonical WHERE tombstoned = 0 "
                "ORDER BY promoted_at DESC LIMIT ?",
                (limit * 3,),  # grab more then take top N per bucket below
            ).fetchall()
            for r in rows:
                fid, content, kind, importance, ns, ts, sid, p_kind, p_agent = r
                platform = _platform_from_session(sid)
                # Truncate content for list rendering; full content on click.
                row = {
                    "id": fid,
                    "content": (content or "")[:140],
                    "kind": kind or "fact",
                    "importance": importance or 0.0,
                    "namespace": ns,
                    "ts": ts,
                    "tier": tier_label,
                    "platform": platform,
                    "origin_session_id": sid,
                    "provenance_kind": p_kind,
                    "provenance_agent": p_agent,
                }
                by_kind.setdefault(kind or "fact", []).append(row)
                by_tier.setdefault(tier_label, []).append(row)
                by_platform.setdefault(platform, []).append(row)
            co.close()
        except Exception:
            pass

    # Scan all 3 tiers.
    _scan(admin_db, "private")
    _scan(source_db, "source")
    _scan(public_db, "public")

    # Cap each bucket to `limit`.
    by_kind = {k: v[:limit] for k, v in by_kind.items()}
    by_tier = {k: v[:limit] for k, v in by_tier.items()}
    by_platform = {k: v[:limit] for k, v in by_platform.items()}

    # Counts for badges (use total scan size, not capped, so user sees real density).
    counts = {
        "by_kind": {k: len(v) for k, v in by_kind.items()},
        "by_tier": {k: len(v) for k, v in by_tier.items()},
        "by_platform": {k: len(v) for k, v in by_platform.items()},
    }

    return {
        "by_kind": by_kind,
        "by_tier": by_tier,
        "by_platform": by_platform,
        "counts": counts,
        "limit_per_bucket": limit,
    }


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


def _summarize_llm_spend(astor_dir) -> dict:
    """Per-tier / per-user LLM call aggregation across all forge DBs.

    Phase E5 (2026-09-17): spend tracking for the dashboard. Walks every
    ``astor_forge_*.db`` under ``<tier>/memory/`` and ``users/<u>/memory/``,
    aggregates ``llm_call_log`` rows by user_id + provider + operation.

    Args:
        astor_dir: Path or str pointing at the Astor-Memory-Runtime root.

    Returns a dict shaped:
      {
        "by_tier":   [{tier, calls, success, input_chars, latency_ms_sum}, ...],
        "by_user":   [{user_id, calls, success, input_chars, latency_ms_sum}, ...],
        "by_provider":[{provider, calls, success, input_chars, latency_ms_sum}, ...],
        "totals":    {calls, success, input_chars, latency_ms_sum, error_count},
      }

    Best-effort: missing or empty forge DBs are silently skipped.
    """
    if not isinstance(astor_dir, Path):
        astor_dir = Path(astor_dir)
    # Approximate cost: 1 token ≈ 4 chars; gpt-4o-mini ~ $0.15/M input.
    # The number is informational; we expose raw counts so the admin
    # can map them to whatever pricing they actually pay.
    out: dict = {
        "by_tier": [],
        "by_user": [],
        "by_provider": [],
        "totals": {
            "calls": 0,
            "success": 0,
            "error_count": 0,
            "input_chars": 0,
            "latency_ms_sum": 0,
        },
    }
    forge_dbs: list[Path] = []
    # Per-tier forge DBs (public, source, private, repo).
    for tier in ("public", "source", "private", "repo"):
        cand = astor_dir / tier / "memory" / f"astor_forge_{tier}.db"
        if cand.exists():
            forge_dbs.append(cand)
    # Per-user forge DBs (one per user with their own private store).
    users_root = astor_dir / "users"
    if users_root.exists():
        for user_dir in sorted(p for p in users_root.iterdir() if p.is_dir()):
            cand = user_dir / "memory" / f"astor_forge_{user_dir.name}.db"
            if cand.exists():
                forge_dbs.append(cand)

    if not forge_dbs:
        return out

    by_tier: dict[str, dict] = {}
    by_user: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    totals = {"calls": 0, "success": 0, "error_count": 0,
              "input_chars": 0, "latency_ms_sum": 0}

    for db_path in forge_dbs:
        try:
            co = sqlite3.connect(str(db_path))
            cu = co.cursor()
            # Confirm the table exists; older DBs may not have it.
            tbls = {r[0] for r in cu.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "llm_call_log" not in tbls:
                co.close()
                continue
            # Identify the tier/user from the path.
            tier = "?"
            user_id = "?"
            parts = db_path.parts
            if "memory" in parts:
                mi = parts.index("memory")
                if mi >= 1:
                    tier = parts[mi - 1]
            if "/users/" in str(db_path):
                user_id = str(db_path).split("/users/")[1].split("/memory/")[0]
            elif tier in ("public", "source", "private", "repo"):
                user_id = f"<{tier}>"
            # Aggregate from this DB.
            for r in cu.execute(
                "SELECT user_id, tier, provider, operation, input_length, "
                "       success, latency_ms "
                "FROM llm_call_log"
            ).fetchall():
                ruid, rtier, rprovider, _op, rlen, rsucc, rlat = r
                if not rsucc:
                    totals["error_count"] += 1
                else:
                    totals["success"] += 1
                totals["calls"] += 1
                totals["input_chars"] += (rlen or 0)
                totals["latency_ms_sum"] += (rlat or 0)
                # by_tier
                bt = by_tier.setdefault(rtier or tier, {
                    "tier": rtier or tier, "calls": 0,
                    "success": 0, "input_chars": 0, "latency_ms_sum": 0,
                })
                if rsucc:
                    bt["success"] += 1
                bt["calls"] += 1
                bt["input_chars"] += (rlen or 0)
                bt["latency_ms_sum"] += (rlat or 0)
                # by_user
                bu = by_user.setdefault(ruid or user_id, {
                    "user_id": ruid or user_id, "calls": 0,
                    "success": 0, "input_chars": 0, "latency_ms_sum": 0,
                })
                if rsucc:
                    bu["success"] += 1
                bu["calls"] += 1
                bu["input_chars"] += (rlen or 0)
                bu["latency_ms_sum"] += (rlat or 0)
                # by_provider
                bp = by_provider.setdefault(rprovider or "?", {
                    "provider": rprovider or "?", "calls": 0,
                    "success": 0, "input_chars": 0, "latency_ms_sum": 0,
                })
                if rsucc:
                    bp["success"] += 1
                bp["calls"] += 1
                bp["input_chars"] += (rlen or 0)
                bp["latency_ms_sum"] += (rlat or 0)
            co.close()
        except Exception:
            # Best-effort; skip DBs we can't open.
            continue

    out["by_tier"] = sorted(by_tier.values(), key=lambda x: -x["calls"])
    out["by_user"] = sorted(by_user.values(), key=lambda x: -x["calls"])
    out["by_provider"] = sorted(by_provider.values(), key=lambda x: -x["calls"])
    out["totals"] = totals
    return out


def _collect_all_bus_db_paths(astor_dir: Path) -> list[Path]:
    """Discover every memory_canonical SQLite db under {public, source, users/*, private*/}.

    Used by per-tier dashboard aggregations. Returns deduplicated list of Paths.
    Walks well-known locations to find bus dbs without iterating all .db files.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    candidates = [
        astor_dir / "public" / "memory",
        astor_dir / "source" / "memory",
        astor_dir / "private" / "memory",
        astor_dir / "users",  # dir of users/<uid>/memory/
    ]
    for c in candidates:
        if not c.exists():
            continue
        if c.is_dir():
            for p in c.rglob("*bus*.db"):
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(p)
        else:
            # legacy: the file itself
            rp = c.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(c)
    return out


def _tier_alias_for(db_path: Path, astor_dir: Path) -> str:
    """Map a bus db Path to its tier alias string for dashboard grouping.

    Filename-based fast path (more reliable than path-based for our 9-db layout).
    """
    fname = db_path.name.lower()
    if fname == "astor_bus_public.db":
        return "public"
    if fname == "astor_bus_source.db":
        return "source"
    if fname.startswith("astor_bus_users_"):
        return fname.replace("astor_bus_", "").replace(".db", "")
    if fname.startswith("astor_bus_"):
        stem = fname.replace("astor_bus_", "").replace(".db", "")
        if stem in {"admin", "anyu", "aran", "bo-wang", "demo_external_agent",
                    "halama", "jason", "jaydon", "nelson", "owen", "rita",
                    "roy", "steve", "sunday", "xian-ding", "xindi", "yuqi"}:
            return f"private_{stem}"
        return stem
    return "other"


def _memory_class_distribution(astor_dir: Path) -> dict[str, dict[str, int]]:
    """v1.14.74 (2026-09-18) Hindsight 4-tier count per tier + per user.

    Walks every bus db under {public, source, users/<u>, private/private_<u>}
    and tallies memory_class for active facts (tombstoned=0). Returns dict
    keyed by tier alias with sub-totals + overall distribution.

    Returns: {"public": {"world_fact": N, "experience": N, ...}, ...,
              "overall": {"world_fact": N, "experience": N, ...}}
    """
    buckets: dict[str, dict[str, int]] = {}
    db_paths = _collect_all_bus_db_paths(astor_dir)
    for dbp in db_paths:
        tier_alias = _tier_alias_for(dbp, astor_dir)
        try:
            c = sqlite3.connect(dbp, timeout=3)
            # Probe: only query if memory_class column exists
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(memory_canonical)"
            ).fetchall()}
            if "memory_class" not in cols:
                c.close()
                continue  # pre-v1.14.74 DB, skip until next reload runs migration
            rows = c.execute(
                "SELECT memory_class, COUNT(*) FROM memory_canonical "
                "WHERE tombstoned = 0 GROUP BY memory_class"
            ).fetchall()
            c.close()
        except Exception:
            continue
        buckets.setdefault(tier_alias, {})
        for mc, n in rows:
            buckets[tier_alias][mc] = buckets[tier_alias].get(mc, 0) + n

    # Aggregate overall
    overall: dict[str, int] = {}
    for tier in buckets.values():
        for mc, n in tier.items():
            overall[mc] = overall.get(mc, 0) + n
    if overall:
        buckets["overall"] = overall
    return buckets


def _decayed_count(astor_dir: Path) -> dict[str, int]:
    """v1.14.74 (2026-09-18) — Count of tombstoned facts (decay soft-deleted or manual).

    Useful metric for the dashboard panel that visualizes decay policy outcome.
    Note: this overlaps with `hero.tombstoned` but breaks it down further:
      tombstoned_recent_30d = tombstoned AND tombstoned_at > 30 days ago
      tombstoned_old        = tombstoned AND tombstoned_at <= 30 days ago (or NULL)
      tombstoned_no_ts      = tombstoned AND tombstoned_at IS NULL (pre-feature)
    """
    buckets = {"total": 0, "recent_30d": 0, "old": 0, "no_timestamp": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db_paths = _collect_all_bus_db_paths(astor_dir)
    for dbp in db_paths:
        try:
            c = sqlite3.connect(dbp, timeout=3)
            rows = c.execute(
                "SELECT tombstoned_at FROM memory_canonical WHERE tombstoned = 1"
            ).fetchall()
            c.close()
        except Exception:
            continue
        for (ts,) in rows:
            buckets["total"] += 1
            if not ts:
                buckets["no_timestamp"] += 1
                continue
            # tombstoned_at can be ISO string OR epoch int (legacy)
            ts_str = str(ts) if not isinstance(ts, str) else ts
            if ts_str > cutoff:
                buckets["recent_30d"] += 1
            else:
                buckets["old"] += 1
    return buckets


def build_dashboard_payload(astor_dir: str | Path) -> dict:
    """Aggregate the 6 dashboard dimensions into a single JSON-ready dict.

    Args:
        astor_dir: Path to Astor-Memory-Runtime root (contains users/ + public/).

    Returns:
        Dict with keys: generated_at, hero, eval_trend, per_user, growth_30d,
        top_keywords, recent_facts, importance_histogram, health, plus
        v1.14.74 additions memory_class_distribution + decayed_count.
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
    # v1.14.25 Ship G: entities_json coverage across all tiers.
    entities_cov = _entities_coverage(astor)
    # v1.14.37 Ship N: Recent Capture panel — facts grouped by kind/tier/platform.
    recent_capture = _recent_capture(astor, limit=10)
    # Phase E5 (2026-09-17): LLM call spend tracking.
    llm_spend = _summarize_llm_spend(astor)
    # v1.14.74 (2026-09-18) Hindsight insights: taxonomy distribution + decay stats.
    memory_class_distribution = _memory_class_distribution(astor)
    decayed_count = _decayed_count(astor)

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
        "memory_class_distribution": memory_class_distribution,
        "decayed_count": decayed_count,
        "llm_spend": llm_spend,
        "health": health,
        # v1.14.25 Ship G (2026-09-15): structured entity binding coverage.
        # Useful for monitoring the Ship B backfill progress and tracking
        # the % of facts that have at least 1 extracted entity.
        "entities_coverage": entities_cov,
        # v1.14.37 Ship N: Recent Capture panel — grouped facts for dashboard UI.
        "recent_capture": recent_capture,
    }


if __name__ == "__main__":
    # CLI: python -m astor_memory.dashboard_data [astor_dir]
    import sys
    astor_dir = sys.argv[1] if len(sys.argv) > 1 else "D:/AI/Astor-Memory-Runtime"
    payload = build_dashboard_payload(astor_dir)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
