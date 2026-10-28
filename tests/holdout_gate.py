"""holdout_gate.py — RSIHub-style held-out gate for astor-memory.

Why this exists
---------------
eval_set.jsonl is what variant selection optimises against (bm25_weight,
mmr_lambda, hyde on/off). A second gate that reuses those same questions
cannot detect the failure RSIHub warns about: **the tuned score goes up
while unseen tasks go down.** eval_control_set.json was 38/39 identical in
query text to eval_set.jsonl, so it was not a held-out set.

eval_holdout_set.json is disjoint from eval_set.jsonl by construction and
every query in it was verified against a live /v1/read before being written.

The scoring rule is locked
--------------------------
RSIHub's boundary condition: "the evaluator and the certification records
are controlled by the framework — a candidate must not be able to raise its
score by changing the scoring rules." This gate implements that literally:

  * set_hash    = sha256 of the holdout set file
  * scorer_hash = sha256 of THIS file (the scorer)

Both are written into every history record. A delta is only computed
against a previous run whose (set_hash, scorer_hash) match. Editing the
set, or loosening KEYWORD_MIN_RATIO, voids the baseline instead of
quietly improving the score.

Usage
-----
    python holdout_gate.py                          # production config
    python holdout_gate.py --variant vector_only    # a named variant
    python holdout_gate.py --kwargs '{"bm25_weight":0.4}'
    python holdout_gate.py --output report.json

Exit codes: 0 = PASS, 1 = FAIL, 2 = DEGRADED / INFRA_ERROR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("ASTOR_PROJECT_ROOT",
                            str(Path(__file__).resolve().parent.parent)))
TESTS_DIR = ROOT / "tests"
METRICS_DIR = Path(os.environ.get(
    "ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics")))
HOLDOUT_SET = TESTS_DIR / "eval_holdout_set.json"
EVAL_SET = TESTS_DIR / "eval_set.jsonl"
HISTORY = METRICS_DIR / "holdout_history.jsonl"
DEFAULT_SERVER = "http://127.0.0.1:7803"

# Keyword hit ratio required to pass. Same shape as control_set_gate's
# rule: a query passes when enough of its expected keywords appear in the
# top-K answer text.
KEYWORD_MIN_RATIO = 0.5
MIN_RESULTS = 1

# Named variants mirror eval_runner's variants so the two gates can be
# compared on the same configuration.
VARIANTS = {
    "baseline": {"hybrid": True, "rerank": "on"},
    "vector_only": {"hybrid": False, "rerank": "off"},
    "rerank_off": {"hybrid": True, "rerank": "off"},
    "mmr_lambda_05": {"hybrid": True, "rerank": "on", "mmr_lambda": 0.5},
    "mmr_lambda_07": {"hybrid": True, "rerank": "on", "mmr_lambda": 0.7},
    "mmr_lambda_09": {"hybrid": True, "rerank": "on", "mmr_lambda": 0.9},
    "hyde_on": {"hybrid": True, "rerank": "on", "hyde": True},
    "hyde_off": {"hybrid": True, "rerank": "on", "hyde": False},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def scorer_hash() -> str:
    """Hash of the scorer itself.

    Includes this file's own bytes, so any edit to the scoring rule
    changes the hash and voids the previous baseline.
    """
    return _sha256(Path(__file__).resolve())


def set_hash() -> str:
    return _sha256(HOLDOUT_SET)


def check_disjointness() -> dict:
    """A held-out set that reuses the tuning set's questions is not held out.

    Compared by normalised query text. Overlap is reported and, above a
    threshold, treated as a hard error — the gate would otherwise be
    measuring the tuning set and reporting it as transfer.
    """
    if not EVAL_SET.exists():
        return {"checked": False, "reason": "eval_set.jsonl not found"}
    tune = set()
    for line in EVAL_SET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line).get("query", "")
            tune.add(" ".join(q.lower().split()))
    held = set()
    for q in json.loads(HOLDOUT_SET.read_text(encoding="utf-8"))["queries"]:
        held.add(" ".join(q["query"].lower().split()))
    overlap = tune & held
    return {
        "checked": True,
        "n_tuning": len(tune),
        "n_heldout": len(held),
        "n_overlap": len(overlap),
        "overlap_queries": sorted(overlap)[:5],
        "disjoint": len(overlap) == 0,
    }


def _read(server: str, query: str, tier: str, user: str, top_k: int,
          kwargs: dict, retries: int = 3) -> list[dict]:
    body = json.dumps({"query": query, "tier": tier, "user": user,
                       "top_k": top_k, **kwargs}).encode()
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{server}/v1/read", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("results", []) or []
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def _text_of(results: list[dict]) -> str:
    parts = []
    for x in results:
        for key in ("content", "text", "snippet"):
            v = x.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
                break
    return "\n".join(parts)


def run_gate(server: str, top_k: int, tier: str, user: str,
             kwargs: dict) -> dict:
    cs = json.loads(HOLDOUT_SET.read_text(encoding="utf-8"))
    queries = cs["queries"]
    n_total = len(queries)
    n_pass = n_fail = 0
    failures: list[dict] = []
    infra_errors: list[dict] = []
    mrrs: list[float] = []

    for q in queries:
        qid = q["qid"]
        kws = q.get("expected_keywords") or []
        q_tier = q.get("tier") or tier
        try:
            results = _read(server, q["query"], q_tier, user, top_k, kwargs)
        except (urllib.error.URLError, OSError, ValueError) as e:
            infra_errors.append({"qid": qid, "reason": f"read_error: {e}"[:200]})
            continue

        if len(results) < MIN_RESULTS:
            n_fail += 1
            failures.append({"qid": qid, "reason": "no_results",
                             "query": q["query"][:80]})
            continue

        low = _text_of(results).lower()
        hits = [k for k in kws if k.lower() in low]
        need = max(1, int(len(kws) * KEYWORD_MIN_RATIO + 0.999)) if kws else 0
        if kws and len(hits) < need:
            n_fail += 1
            failures.append({
                "qid": qid, "query": q["query"][:80],
                "reason": f"keyword_miss {len(hits)}/{len(kws)} (need {need})",
                "expected_keywords": kws, "hit_keywords": hits,
            })
            continue
        n_pass += 1
        first_rank = None
        for rank, r in enumerate(results, start=1):
            blob = (r.get("content") or r.get("text") or "").lower()
            if any(k.lower() in blob for k in kws):
                first_rank = rank
                break
        mrrs.append(1.0 / first_rank if first_rank else 0.0)

    n_answered = n_pass + n_fail
    pass_rate = round(n_pass / n_total, 4) if n_total else 0.0
    verdict = ("FAIL" if n_fail else
               "INFRA_ERROR" if (n_pass == 0 and infra_errors) else
               "DEGRADED" if infra_errors else
               "PASS")
    return {
        "gate_version": "v1.0.0-rsihub-heldout",
        "set_version": cs.get("version"),
        "set_hash": set_hash(),
        "scorer_hash": scorer_hash(),
        "keyword_min_ratio": KEYWORD_MIN_RATIO,
        "server": server,
        "tier": tier,
        "user": user,
        "top_k": top_k,
        "kwargs": kwargs,
        "n_total": n_total,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "n_infra_error": len(infra_errors),
        "n_answered": n_answered,
        "pass_rate": pass_rate,
        "mrr": round(statistics.mean(mrrs), 4) if mrrs else 0.0,
        "failures": failures[:20],
        "infra_errors": infra_errors[:10],
        "verdict": verdict,
    }


def _previous_same_scoring() -> dict | None:
    """Last history record with the SAME set_hash and scorer_hash.

    A record written under a different hash is not a valid baseline: the
    set or the scoring rule changed, so the numbers are not comparable.
    """
    if not HISTORY.exists():
        return None
    sh, ch = set_hash(), scorer_hash()
    for line in reversed(HISTORY.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("set_hash") == sh and rec.get("scorer_hash") == ch:
            return rec
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--tier", default="private")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))
    ap.add_argument("--kwargs", default=None,
                    help="JSON dict merged over the variant kwargs")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    kwargs = dict(VARIANTS[args.variant])
    if args.kwargs:
        kwargs.update(json.loads(args.kwargs))

    dis = check_disjointness()
    if dis.get("checked") and not dis.get("disjoint"):
        print(json.dumps({"error": "holdout_set_not_disjoint", **dis},
                         indent=2, ensure_ascii=False))
        return 2

    t0 = time.perf_counter()
    result = run_gate(args.server, args.top_k, args.tier, args.user, kwargs)
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["disjointness"] = dis
    result["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    prev = _previous_same_scoring()
    if prev is None:
        result["delta_vs_prev"] = None
        result["baseline_note"] = (
            "no previous run with the same set_hash+scorer_hash — this run "
            "establishes the baseline")
    else:
        result["delta_vs_prev"] = {
            "pass_rate": round(result["pass_rate"] - prev.get("pass_rate", 0), 4),
            "mrr": round(result["mrr"] - prev.get("mrr", 0), 4),
            "prev_ts": prev.get("ts"),
        }

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in result.items()
                            if k != "failures"}, ensure_ascii=False) + "\n")

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    v = result["verdict"]
    if v == "PASS":
        return 0
    if v == "FAIL":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
