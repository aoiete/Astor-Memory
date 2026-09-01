"""
scenario_clustering_v2.py — Layer 2 of TencentDB Agent Memory 4-tier architecture
(adapted for astor-memory 1.2.7 schema).

What it does:
- Takes atomic facts from astor bus (Layer 1) — `memory_canonical` table
- Groups them by project/scenario using keyword clustering
- Stores scenarios in `scenarios.db` (Layer 2)
- On cold-start, query top-N scenarios by (relevance × decay × importance × access_count)

Schema differences from v1:
- BUS_DB points to astor_bus_public.db (not memory_bus.db)
- memory_canonical schema: namespace, content, kind, confidence, importance, tags,
  promoted_at, last_confirmed_at, access_count, tombstoned (no `scene` or `stable_id`)
- Scenarios stored in `<runtime_dir>public\scenarios.db` (instead of
  memory-bus/scenarios.db)

Usage:
    python scenario_clustering_v2.py cluster --since 7d
    python scenario_clustering_v2.py hydrate --query "weixin token" --top 3
    python scenario_clustering_v2.py status
"""
import sys, os, json, time, sqlite3, hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Astor 1.2.7 paths
BUS_DB = r"<runtime_dir>public\memory\astor_bus_public.db"
SCENARIO_STORE = Path(r"<runtime_dir>public\scenarios.db")


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def init_scenario_db():
    """Layer 2 store — scenarios table + fact links."""
    SCENARIO_STORE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SCENARIO_STORE)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scenarios (
            scenario_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            keywords TEXT,
            fact_ids TEXT,
            importance REAL DEFAULT 0.5,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_accessed REAL,
            access_count INTEGER DEFAULT 0,
            ttl_days INTEGER DEFAULT 30
        );
        CREATE INDEX IF NOT EXISTS idx_updated ON scenarios(updated_at);
        CREATE INDEX IF NOT EXISTS idx_accessed ON scenarios(last_accessed);

        CREATE TABLE IF NOT EXISTS scenario_links (
            scenario_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            fact_source TEXT,
            added_at REAL NOT NULL,
            PRIMARY KEY (scenario_id, fact_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fact ON scenario_links(fact_id);
    """)
    conn.close()


def _keyword_jaccard(a: str, b: str) -> float:
    """Cheap keyword-based similarity for scenario assignment (no LLM needed)."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def fetch_recent_facts(since_days: int = 7, exclude_test: bool = True):
    """Layer 1 → pull canonical facts from astor bus `memory_canonical` table.

    Schema (astor 1.2.7): id, candidate_id, event_id, namespace, content, kind,
    confidence, importance, tags, metadata, promoted_at, promoted_by,
    last_confirmed_at, access_count, tombstoned, expires_at.

    If `exclude_test=True`, filter out test/development markers before clustering.
    Test markers include: kind='forgettable', test_*, delete_me_*, [test]*,
    marker *, unit_test_*, provenance test *.
    """
    if not Path(BUS_DB).exists():
        return []
    conn = sqlite3.connect(BUS_DB)
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if exclude_test:
        rows = conn.execute(
            """SELECT id, namespace, content, kind, importance, promoted_at, tags, confidence
               FROM memory_canonical
               WHERE promoted_at > ?
                 AND (tombstoned IS NULL OR tombstoned = 0)
                 AND kind != 'forgettable'
                 AND content NOT LIKE 'test_%'
                 AND content NOT LIKE 'delete_me_%'
                 AND content NOT LIKE '[test]%'
                 AND content NOT LIKE 'marker %'
                 AND content NOT LIKE 'unit_test_%'
                 AND content NOT LIKE 'provenance test %'
                 AND content NOT LIKE 'e2e_test_%'
                 AND content NOT LIKE 'bugfix_test_%'
                 AND content NOT LIKE 'forgettable%'
                 AND content NOT LIKE 'user test preference %'
                 AND content NOT LIKE 'A fact about unit_test_%'
                 AND content NOT LIKE 'best_for_test_%'
                 AND content NOT LIKE 'test/%'
                 AND content NOT LIKE 'cross-user%'
                 AND content NOT LIKE 'test_market_data_%'
                 AND content NOT LIKE 'No user%'
                 AND content NOT LIKE 'fact about %'
                 AND content NOT LIKE 'Cascade test %'
                 AND content NOT LIKE 'Bugfix test %'
                 AND content NOT LIKE 'Test content %'
                 AND content NOT LIKE 'True fact about %'
                 AND content NOT LIKE 'fact test fact %'
                 AND content NOT LIKE 'test_pattern_%'
                 AND content NOT LIKE 'integration_test_%'
                 AND content NOT LIKE 'scenario_test_%'
                 AND content NOT LIKE 'test pattern %'
                 AND content NOT LIKE 'fact about _%'
                 AND content NOT LIKE 'test truth %'
               ORDER BY promoted_at DESC LIMIT 500""",
            (cutoff_iso,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, namespace, content, kind, importance, promoted_at, tags, confidence
               FROM memory_canonical
               WHERE promoted_at > ?
                 AND (tombstoned IS NULL OR tombstoned = 0)
               ORDER BY promoted_at DESC LIMIT 500""",
            (cutoff_iso,),
        ).fetchall()
    conn.close()

    facts = []
    for r in rows:
        try:
            tags = json.loads(r[6]) if r[6] else []
        except Exception:
            tags = []
        facts.append({
            "id": str(r[0]),
            "namespace": r[1],
            "summary": (r[2] or "")[:200],
            "content": r[2] or "",
            "kind": r[3],
            "importance": r[4] or 0.5,
            "promoted_at": r[5],
            "tags": tags,
            "confidence": r[7] or 0.5,
            "source": "astor_bus",
        })
    return facts


def cluster_facts(facts, jaccard_threshold=0.12, max_scenarios=30):
    """Greedy clustering: assign each fact to existing scenario if overlap >= threshold,
    else create new scenario. Uses tags + content keywords for matching."""
    init_scenario_db()
    conn = sqlite3.connect(SCENARIO_STORE)
    existing = conn.execute(
        "SELECT scenario_id, label, keywords, fact_ids FROM scenarios ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()

    scenarios = {}
    for row in existing:
        sid, label, kws_json, fids_json = row
        scenarios[sid] = {
            "label": label,
            "keywords": set(json.loads(kws_json or "[]")),
            "fact_ids": json.loads(fids_json or "[]"),
        }

    assignments = []
    new_scenarios = 0

    for fact in facts:
        text = (fact["summary"] + " " + fact["content"])[:1000]
        text_kws = set(text.lower().split())
        text_kws |= set(t.lower() for t in fact.get("tags", []) if t)
        text_kws = set(list(text_kws)[:50])

        if not text_kws:
            continue

        best_sid, best_score = None, 0.0
        for sid, s in scenarios.items():
            if not s["keywords"]:
                continue
            kw_overlap = len(text_kws & s["keywords"]) / max(1, len(s["keywords"]))
            jaccard = _keyword_jaccard(" ".join(text_kws), " ".join(list(s["keywords"])[:50]))
            score = 0.6 * kw_overlap + 0.4 * jaccard
            if score > best_score:
                best_sid, best_score = sid, score

        if best_sid and best_score >= jaccard_threshold:
            if fact["id"] not in scenarios[best_sid]["fact_ids"]:
                assignments.append((fact["id"], best_sid))
                scenarios[best_sid]["fact_ids"].append(fact["id"])
                scenarios[best_sid]["keywords"] |= text_kws
        else:
            new_sid = "sc_" + md5(text[:200])
            label = fact["summary"][:60] if fact["summary"] else "unnamed"
            scenarios[new_sid] = {
                "label": label,
                "keywords": text_kws,
                "fact_ids": [fact["id"]],
            }
            assignments.append((fact["id"], new_sid))
            new_scenarios += 1

    # Persist
    conn = sqlite3.connect(SCENARIO_STORE)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_ts = time.time()
    for sid, s in scenarios.items():
        kw_list = list(s["keywords"])[:100]
        existing_row = conn.execute(
            "SELECT created_at FROM scenarios WHERE scenario_id = ?", (sid,)
        ).fetchone()
        if existing_row:
            conn.execute(
                """UPDATE scenarios SET keywords=?, fact_ids=?, updated_at=?
                   WHERE scenario_id=?""",
                (json.dumps(kw_list), json.dumps(s["fact_ids"]), now_iso, sid),
            )
        else:
            conn.execute(
                """INSERT INTO scenarios
                   (scenario_id, label, keywords, fact_ids, importance,
                    created_at, updated_at, last_accessed, access_count, ttl_days)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 30)""",
                (sid, s["label"], json.dumps(kw_list), json.dumps(s["fact_ids"]),
                 0.5, now_iso, now_iso),
            )
        for fid in s["fact_ids"]:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO scenario_links
                       (scenario_id, fact_id, fact_source, added_at) VALUES (?, ?, ?, ?)""",
                    (sid, fid, "astor_bus", now_ts),
                )
            except Exception:
                pass
    conn.commit()
    conn.close()
    return assignments, new_scenarios


def hydrate(query: str, top: int = 3):
    """Cold-start: given a query, return top-N scenarios with their facts."""
    init_scenario_db()
    conn = sqlite3.connect(SCENARIO_STORE)
    rows = conn.execute(
        """SELECT scenario_id, label, keywords, fact_ids, importance,
                  updated_at, last_accessed, access_count
           FROM scenarios ORDER BY updated_at DESC LIMIT 100"""
    ).fetchall()
    conn.close()

    scored = []
    query_kws = set(query.lower().split())
    for row in rows:
        sid, label, kws_json, fids_json, imp, upd, last_acc, acc_cnt = row
        kws = set(json.loads(kws_json or "[]"))
        if not kws:
            continue
        kw_overlap = len(query_kws & kws) / max(1, len(kws))
        jaccard = _keyword_jaccard(query, " ".join(list(kws)[:50]))
        relevance = 0.7 * kw_overlap + 0.3 * jaccard
        try:
            upd_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            days_old = (datetime.now(timezone.utc) - upd_dt).total_seconds() / 86400
        except Exception:
            days_old = 30
        decay = max(0.1, 1.0 - days_old / 30.0)
        score = relevance * decay * (0.5 + 0.5 * imp) * (1 + 0.1 * (acc_cnt or 0))
        scored.append((score, sid, label, json.loads(fids_json or "[]")))

    scored.sort(reverse=True)
    top_n = scored[:top]

    if top_n:
        conn = sqlite3.connect(SCENARIO_STORE)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _, sid, _, _ in top_n:
            conn.execute(
                "UPDATE scenarios SET last_accessed=?, access_count=access_count+1 WHERE scenario_id=?",
                (now_iso, sid),
            )
        conn.commit()
        conn.close()

    # Fetch fact details from astor bus
    fact_details = {}
    if top_n:
        seen_ids = set()
        fact_ids = []
        for _, _, _, fids in top_n:
            for fid in fids:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                try:
                    fact_ids.append(int(fid))
                except (ValueError, TypeError):
                    pass
        if fact_ids and Path(BUS_DB).exists():
            conn = sqlite3.connect(BUS_DB)
            placeholders = ",".join("?" * len(fact_ids))
            try:
                rows = conn.execute(
                    f"SELECT id, kind, substr(content,1,150), promoted_at FROM memory_canonical WHERE id IN ({placeholders})",
                    fact_ids,
                ).fetchall()
                fact_details = {
                    str(r[0]): {"kind": r[1], "summary": r[2], "promoted_at": r[3]}
                    for r in rows
                }
            except Exception:
                pass
            conn.close()

    return [
        {
            "scenario_id": sid,
            "label": label,
            "score": round(score, 3),
            "facts": [
                {"id": fid, **fact_details.get(fid, {"summary": "(not in canonical)"})}
                for fid in fids
            ],
        }
        for score, sid, label, fids in top_n
    ]


def status():
    init_scenario_db()
    conn = sqlite3.connect(SCENARIO_STORE)
    n = conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
    n_links = conn.execute("SELECT COUNT(*) FROM scenario_links").fetchone()[0]
    top = conn.execute(
        """SELECT scenario_id, label, importance, access_count,
                  (SELECT COUNT(*) FROM scenario_links WHERE scenario_id = scenarios.scenario_id) AS n_facts,
                  updated_at
           FROM scenarios ORDER BY updated_at DESC LIMIT 10"""
    ).fetchall()
    conn.close()
    return {"total_scenarios": n, "total_fact_links": n_links, "top_10": top}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("cluster"); pc.add_argument("--since", default="7d")
    ph = sub.add_parser("hydrate"); ph.add_argument("--query", required=True); ph.add_argument("--top", type=int, default=3)
    ps = sub.add_parser("status")
    args = p.parse_args()

    if args.cmd == "cluster":
        since_days = 7
        if args.since.endswith("d"):
            since_days = int(args.since[:-1])
        facts = fetch_recent_facts(since_days)
        assignments, new_n = cluster_facts(facts)
        print(json.dumps({
            "facts_processed": len(facts),
            "scenarios_created": new_n,
            "total_assignments": len(assignments),
        }, indent=2))
    elif args.cmd == "hydrate":
        result = hydrate(args.query, top=args.top)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False, default=str))
