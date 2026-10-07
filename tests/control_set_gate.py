"""control_set_gate.py — semantic ship gating per LongMemEval-S pattern.

v1.16.74 (2026-10-07) Ship G-rework: the id-locked assertion (top3_fids) was
a false-negative generator. Between the 2026-09-30 baseline and 2026-10-07,
memory_canonical grew from a few hundred to 4381 facts, so every top-3
ranking flipped while recall quality stayed the same. Result: 0/40 PASS on a
healthy system — a gate that always fails is worse than no gate.

Fix: assert on MEANING, not on row identity.
  - expected_keywords  -> at least N of them must appear in the top-K answer text
  - baseline_top3_fids -> soft signal only, reported but NOT gating (a fact may
                         legitimately drop out of top-K once the corpus grows)

This keeps the gate useful for the failure mode that matters (recall returned
nothing relevant / returned garbage) while surviving corpus growth.

Usage:
  python control_set_gate.py                      # against live :7803
  python control_set_gate.py --output report.json
  python control_set_gate.py --top-k 20 --tier private

Returns 0 if the gate passes, 1 if any query lost its expected meaning.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTROL_SET = Path(__file__).parent / "eval_control_set.json"
DEFAULT_SERVER = "http://127.0.0.1:7803"

# Keyword hit ratio required to pass. 0.34 == "2 of 3 expected keywords"
# (most queries carry 3); lenient enough that a reworded fact still passes.
KEYWORD_MIN_RATIO = 0.34
# A query that returns nothing at all is an automatic fail regardless of gate.
MIN_RESULTS = 1


def _read(server: str, query: str, top_k: int, tier: str,
          user: str = "admin") -> list[dict]:
    # `user` is the actor identity. private-tier reads are ACL-gated, so a
    # read without it comes back 403 FORBIDDEN — same reason eval_runner.py
    # always passes user=admin.
    payload = json.dumps(
        {"query": query, "tier": tier, "top_k": top_k, "user": user}
    ).encode()
    req = urllib.request.Request(
        f"{server}/v1/read", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    return d.get("results", []) or []


def _text_of(results: list[dict]) -> str:
    parts = []
    for x in results:
        for key in ("content", "text", "snippet"):
            v = x.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
                break
    return "\n".join(parts)


def _check_control_set(server: str, top_k: int, tier: str,
                       user: str = "admin") -> dict:
    cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
    queries = cs["queries"]
    n_total = len(queries)
    n_pass = n_fail = 0
    failures = []
    fid_drift = []

    for q in queries:
        qid = q["qid"]
        query = q["query"]
        kws = q.get("expected_keywords") or []
        # Per-query tier override. A single global tier is wrong when the
        # control set spans tiers (e.g. project/theme facts live in the admin
        # private tier and are unreadable from source).
        q_tier = q.get("tier") or tier
        try:
            results = _read(server, query, top_k, q_tier, user)
        except (urllib.error.URLError, OSError, ValueError) as e:
            n_fail += 1
            failures.append({
                "qid": qid, "query": query[:80],
                "category": q.get("category"), "tier": q_tier,
                "reason": f"read_error: {e}"[:200],
            })
            continue

        if len(results) < MIN_RESULTS:
            n_fail += 1
            failures.append({
                "qid": qid, "query": query[:80],
                "category": q.get("category"), "tier": q_tier,
                "reason": f"no_results (top_k={top_k} returned {len(results)})",
            })
            continue

        text = _text_of(results)
        # Keyword match is case-insensitive; CJK needs no tokenisation.
        low = text.lower()
        hits = [k for k in kws if k.lower() in low]
        need = max(1, int(len(kws) * KEYWORD_MIN_RATIO + 0.999)) if kws else 0

        if kws and len(hits) < need:
            n_fail += 1
            failures.append({
                "qid": qid, "query": query[:80],
                "category": q.get("category"), "tier": q_tier,
                "reason": f"keyword_miss {len(hits)}/{len(kws)} (need {need})",
                "expected_keywords": kws,
                "hit_keywords": hits,
                "top_fids": [int(x.get("fact_id") or x.get("id") or 0) for x in results[:5]],
            })
            continue

        n_pass += 1

        # Soft signal: id drift is reported but never gates.
        base_fids = q.get("baseline_top3_fids") or []
        cur_fids = [int(x.get("fact_id") or x.get("id") or 0) for x in results[:3]]
        if base_fids and not (set(base_fids) & set(cur_fids)):
            fid_drift.append({
                "qid": qid, "query": query[:60],
                "baseline_top3": base_fids, "current_top3": cur_fids,
            })

    return {
        "gate_version": "v1.16.74-semantic+tier-override",
        "control_set_version": cs.get("version"),
        "baseline_ts": cs.get("baseline_ts"),
        "baseline_version": cs.get("baseline_version"),
        "server": server,
        "tier": tier,
        "user": user,
        "top_k": top_k,
        "keyword_min_ratio": KEYWORD_MIN_RATIO,
        "n_total": n_total,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "pass_rate": round(n_pass / n_total, 4) if n_total else 0.0,
        "failures": failures[:20],
        "fid_drift_count": len(fid_drift),
        "fid_drift_sample": fid_drift[:5],
        "verdict": "PASS" if n_fail == 0 else "FAIL",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--tier", default="source")
    ap.add_argument("--user", default="admin",
                    help="Actor identity; private-tier reads are ACL-gated.")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    result = _check_control_set(args.server, args.top_k, args.tier, args.user)
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
