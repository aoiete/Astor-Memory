"""control_set_gate.py — zero-flip ship gating per LongMemEval-S pattern.

v1.16.71 (2026-10-06) Ship D: any ship that flips ANY control-set query from
matched=True to matched=False must abort the ship. The control set is a
60-query (here 47 actual) subset of the eval_set.jsonl that hit_rate=1.0 in
the snapshot baseline. Pattern from arxiv 2609.38021 (mp/-ula31Nw4kmDYPQooKPsGA).

Usage:
  python control_set_gate.py --baseline-run eval_baseline_20260930T075910Z
  python control_set_gate.py --baseline-run <latest> --astor-dir <runtime>

Returns 0 if all queries still match (PASS), 1 if any flipped (ABORT).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

CONTROL_SET = Path(__file__).parent / "eval_control_set.json"
DEFAULT_ASTOR_DIR = r"D:/AI/Astor-Memory-Runtime"


def _post_read(server: str, query: str, top_k: int = 5) -> list[int]:
    payload = json.dumps({"query": query, "tier": "source", "top_k": top_k}).encode()
    req = urllib.request.Request(
        f"{server}/v1/read", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    # Top-K fact_ids in order
    return [int(x.get("fact_id") or x.get("id")) for x in d.get("results", [])]


def _check_control_set(server: str) -> dict:
    cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
    queries = cs["queries"]
    pass_rule = cs.get("pass_rule", {"overlap_min": 0.5, "mrr_tolerance": 0.05})
    n_total = len(queries)
    n_pass = 0
    n_fail = 0
    flips = []
    for q in queries:
        baseline_top3 = q["baseline_top3_fids"]
        baseline_mrr = q["baseline_mrr"]
        try:
            payload = json.dumps({"query": q["query"], "tier": "source", "top_k": 10}).encode()
            req = urllib.request.Request(
                f"{server}/v1/read", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            top10 = [int(x.get("fact_id") or x.get("id")) for x in d.get("results", [])]
        except Exception as e:
            flips.append({
                "qid": q["qid"], "query": q["query"][:80],
                "category": q["category"], "error": str(e)[:200],
            })
            n_fail += 1
            continue
        # Compute overlap = |baseline_top3 ∩ top10| / |baseline_top3|
        overlap = sum(1 for fid in baseline_top3 if fid in top10) / max(len(baseline_top3), 1)
        # Compute current MRR (1.0 if any baseline_top3 is in top-1)
        current_first_rank = next(
            (i + 1 for i, fid in enumerate(top10) if fid in baseline_top3),
            0,
        )
        current_mrr = (1.0 / current_first_rank) if current_first_rank > 0 else 0.0
        passes_overlap = overlap >= pass_rule.get("overlap_min", 0.5)
        passes_mrr = current_mrr >= (baseline_mrr - pass_rule.get("mrr_tolerance", 0.05))
        if passes_overlap or passes_mrr:
            n_pass += 1
        else:
            n_fail += 1
            flips.append({
                "qid": q["qid"], "query": q["query"][:80],
                "category": q["category"],
                "baseline_top3": baseline_top3,
                "current_top3": top10[:3],
                "overlap": round(overlap, 2),
                "baseline_mrr": baseline_mrr,
                "current_mrr": round(current_mrr, 3),
            })
    return {
        "baseline_run": cs.get("baseline_run"),
        "baseline_version": cs.get("baseline_version"),
        "control_set_version": cs.get("version"),
        "n_total": n_total,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "flips": flips[:10],
        "verdict": "PASS" if n_fail == 0 else "FAIL",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:7803")
    ap.add_argument("--output", default=None,
                    help="Optional JSON file to write gate report")
    args = ap.parse_args()

    result = _check_control_set(args.server)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())