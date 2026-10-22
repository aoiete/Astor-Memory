"""
measure_token_savings.py — quantify what astor actually saves per recall.

Claude-Mem claims "single-session token spend drops ~75%" and "three-tier
retrieval saves 10x tokens". astor has never measured its own number, so the
value of the memory system is currently a feeling, not a figure.

Method
------
For a set of queries, measure the token cost of TWO ways of answering the same
question:

  A. cold  — no memory. The agent has nothing and must re-derive context.
             We model this as "the whole project context has to be re-stated":
             the tokens of the raw event stream that produced the answer.

  B. warm  — astor recall. The agent reads top-K facts instead.

  savings = 1 - (warm / cold)

Cold is estimated from the underlying bus events for the facts that warm
returned (their source text), which is what a memoryless session would have to
carry. Warm is the actual character count of the returned fact content.

This is a model, not a lab measurement — it assumes the only alternative to
recalling a fact is carrying its raw provenance. The ratio is the useful part;
the absolute numbers are not.

Usage:
  python measure_token_savings.py                     # default query set
  python measure_token_savings.py --queries q1,q2,q3
  python measure_token_savings.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SERVER = "http://127.0.0.1:7803"
DEFAULT_TIER = "source"
DEFAULT_USER = "admin"

# Rough CJK/ASCII token ratio. For mixed zh/en text, ~1 token per 1.6 chars
# is a serviceable estimate; the RATIO between two counts is what matters and
# it cancels most of this constant.
CHARS_PER_TOKEN = 1.6

DEFAULT_QUERIES = [
    "how do I fix astor promote loop",
    "what is the user's portfolio rebalance policy",
    "which cron jobs need ASTOR_DIR",
    "why did astor fail with PIL import error",
    "what is the venv launcher two-stage process",
]


def _tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _find_bus_db(astor_dir: Path) -> list[Path]:
    """All bus DBs — a fact can live in any tier, and cold cost is measured
    against whichever tier actually holds its provenance."""
    out = []
    for pattern in ("**/astor_bus*.db", "astor_bus*.db"):
        out.extend(astor_dir.glob(pattern))
    return [p for p in out if p.is_file()]


def _source_chars_for(fact_ids: list[int], astor_dir: Path) -> dict[int, str]:
    """For each fact_id, find the source event text it was extracted from.

    memory_canonical.event_id -> events.content. That raw content is what a
    memoryless session would have had to carry in context.
    """
    by_fact: dict[int, str] = {}
    for db_path in _find_bus_db(astor_dir):
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
        except Exception:
            continue
        # Only bus DBs carry memory_canonical; forge/nest/canonical DBs do not.
        try:
            has = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_canonical'"
            ).fetchone()
        except sqlite3.Error:
            db.close()
            continue
        if not has:
            db.close()
            continue
        try:
            # batch query per db (sqlite has a variable limit; stay well under)
            CHUNK = 400
            ids = fact_ids
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i + CHUNK]
                ph = ",".join("?" * len(chunk))
                try:
                    for r in db.execute(
                        f"SELECT c.id AS fid, e.content AS src "
                        f"FROM memory_canonical c JOIN events e ON c.event_id = e.id "
                        f"WHERE c.id IN ({ph})",
                        chunk,
                    ):
                        if r["src"] and r["fid"] not in by_fact:
                            by_fact[r["fid"]] = r["src"]
                except sqlite3.Error:
                    continue
        finally:
            db.close()
        if len(by_fact) == len(fact_ids):
            break
    return by_fact


def _read(server: str, query: str, top_k: int, tier: str, user: str) -> list[dict]:
    payload = json.dumps(
        {"query": query, "tier": tier, "top_k": top_k, "user": user}
    ).encode()
    req = urllib.request.Request(
        f"{server}/v1/read", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return (json.loads(r.read()).get("results") or [])


def measure(server: str, astor_dir: Path, queries: list[str],
            top_k: int, tier: str, user: str) -> dict:
    rows = []
    for q in queries:
        try:
            results = _read(server, q, top_k, tier, user)
        except (urllib.error.URLError, OSError, ValueError) as e:
            rows.append({"query": q, "error": str(e)[:160]})
            continue
        if not results:
            rows.append({"query": q, "n_hits": 0, "cold_tokens": 0,
                         "warm_tokens": 0, "savings": None})
            continue

        fids = [int(r.get("fact_id") or r.get("id") or 0) for r in results]
        warm_chars = sum(len(r.get("content") or "") for r in results)
        warm_tokens = _tokens(" " * 0) + round(warm_chars / CHARS_PER_TOKEN)

        src = _source_chars_for(fids, astor_dir)
        cold_chars = sum(len(v) for v in src.values())
        if cold_chars == 0:
            # provenance not reachable — report warm only, savings unknown
            cold_tokens = 0
        else:
            cold_tokens = round(cold_chars / CHARS_PER_TOKEN)

        savings = (1 - warm_tokens / cold_tokens) if cold_tokens else None
        rows.append({
            "query": q,
            "n_hits": len(results),
            "warm_tokens": warm_tokens,
            "cold_tokens": cold_tokens,
            "savings": round(savings, 4) if savings is not None else None,
            "fids": fids[:8],
        })

    measured = [r for r in rows if r.get("savings") is not None]
    out = {
        "server": server, "tier": tier, "user": user, "top_k": top_k,
        "chars_per_token": CHARS_PER_TOKEN,
        "astor_dir": str(astor_dir),
        "n_queries": len(rows),
        "n_measured": len(measured),
        "rows": rows,
    }
    if measured:
        s = [r["savings"] for r in measured]
        out["mean_savings"] = round(statistics.fmean(s), 4)
        out["median_savings"] = round(statistics.median(s), 4)
        out["mean_warm_tokens"] = round(statistics.fmean(
            [r["warm_tokens"] for r in measured]), 1)
        out["mean_cold_tokens"] = round(statistics.fmean(
            [r["cold_tokens"] for r in measured]), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--astor-dir", default=None,
                    help="runtime dir (default: the shared resolver)")
    ap.add_argument("--tier", default=DEFAULT_TIER)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--queries", default=None,
                    help="comma-separated; defaults to a built-in set")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    qs = ([q.strip() for q in args.queries.split(",") if q.strip()]
          if args.queries else DEFAULT_QUERIES)

    result = measure(args.server, Path(args.astor_dir), qs,
                     args.top_k, args.tier, args.user)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    # A measurement that could not resolve provenance is not a pass.
    return 0 if result.get("mean_savings") is not None else 1


if __name__ == "__main__":
    sys.exit(main())