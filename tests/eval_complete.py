"""eval_complete.py — complete-retrieval-rate probe (EvoMemState, arXiv 2611.03743).

Why this exists
---------------
Every recall metric astor has measured so far is a HIT metric: hit_rate,
mrr, oracle_hit_rate. All of them ask "did at least one relevant fact come
back?" The EvoMemState paper (XMU/JLU, arXiv 2611.03743) measures something
different and, by its own numbers, more informative:

    Complete Retrieval Rate — of the queries whose evidence is fully
    retrievable, in what fraction did the system actually retrieve ALL of
    the required evidence?

Their measurement: MemoryBank / LightMem / Supermemory failed to retrieve
all necessary information in 58.9% / 61.8% / 58.0% of the MemSyco-Bench
"Valid Memory Selection" samples, and on 4 of 5 systems the answer accuracy
was 5.9-33.3 points higher once the evidence was complete.

The gap in our harness
----------------------
`score_results()` counts how many RESULT ROWS contain an expected keyword
and calls the query a hit at `n_matches >= min_hits`. With min_hits=1 (103
of our 110 questions) that means "one row containing one keyword" is a
perfect score. It cannot distinguish:

    hit    = got 1 of 3 needed pieces of evidence   (looks identical)
    hit    = got all 3 needed pieces of evidence

and it never compares the two sides at the granularity of a keyword. A
query can sit at hit_rate=1.0 while being unable to answer the question,
because the one fact it retrieved does not carry the rest of the evidence.

What this module does
---------------------
It reuses the SAME keyword vocabulary and the SAME store-side rule as
eval_oracle.py, and only adds the read-side counterpart:

    oracle_covered   keywords the STORE holds (any row, no ranking layer)
    recall_covered   keywords the RECALL returned in top-k
    evidence_complete  recall_covered ⊇ oracle_covered

`complete_retrieval_rate` is then, over the queries whose evidence is
fully present in the store (oracle_hit), the fraction where the read path
returned ALL of it. That is the number the paper says predicts answer
accuracy; we currently have no way to see it at all.

TWO METRICS, because one measurement lied
------------------------------------------
The first version of this module measured completeness purely by keyword
coverage and reported complete_retrieval_rate = 0.7358. Cross-checking
against the store proved that number was mostly a measurement artifact:

  * Q42 ("R218 gateway no-restart") was scored incomplete with
    missing=['no-restart'], yet 9 of the 10 recalled rows were members of
    the store's evidence set. The evidence WAS retrieved -- the keyword
    just does not appear verbatim, because the facts spell it "no
    restart" (space) and "gateway no-restart" (hyphen) while the eval
    set spells it "no-restart" (hyphen).
  * Q01 ("德州扑克现金局") was scored 0/4 keywords covered, while the
    recalled rows were plainly the right facts ("poker brief", 纸面引擎) --
    written in English/pinyin, not the Chinese keywords the eval set
    happens to use.

So keyword coverage measures the EVAL SET's vocabulary, not the system's
retrieval. Hence two independent readings, reported side by side:

  complete_retrieval_rate      keyword-level (what the eval set names)
  evidence_hit_rate            ID-level (what the store actually holds)
  mean_evidence_recall_ratio   ID-level depth

The ID-level ones are language-independent and are the honest numbers:
they ask how many of the facts that constitute the answer came back.

Read-only. Touches neither the server nor the bus DB.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ASTOR_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent)))
TESTS_DIR = ROOT / "tests"
METRICS_DIR = Path(os.environ.get("ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics")))
SERVER = os.environ.get("ASTOR_SERVER", "http://127.0.0.1:7803")
EVAL_SET = TESTS_DIR / "eval_set.jsonl"

sys.path.insert(0, str(TESTS_DIR))
try:
    from eval_oracle import coverage as _oracle_coverage
    from eval_oracle import oracle_search as _oracle_search
except ImportError:  # pragma: no cover
    print("ERROR: eval_oracle.py not importable", file=sys.stderr)
    raise


def load_eval_set(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def recall(query: str, tier: str, user: str, top_k: int) -> tuple[list[dict], float]:
    body = {"query": query, "tier": tier, "user": user, "top_k": top_k}
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{SERVER}/v1/read",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read())
            return payload.get("results", []) or [], (time.perf_counter() - t0) * 1000.0
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"recall failed after 3 retries: {last_err}")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s\-_/\\.,:;()\[\]{}\"'`]+", "", s)


def _tokens(s: str) -> set[str]:
    """Split a keyword into matchable pieces.

    Tokenize the ORIGINAL string, not the normalized one -- normalizing
    first strips the hyphen, so "no-restart" would become one token
    "norestart" and never match "no restart". Latin runs stay whole words;
    CJK runs become 2-grams, because Chinese has no word boundary.
    """
    s = unicodedata.normalize("NFKC", s or "").lower()
    out = set(re.findall(r"[a-z0-9]+", s))
    cjk = re.sub(r"[a-z0-9]+", "", s)
    for i in range(len(cjk) - 1):
        out.add(cjk[i:i + 2])
    if len(cjk) == 1:
        out.add(cjk)
    return out


def _covered(kw: str, blob_norm: str, blob_tokens: set[str]) -> bool:
    """Is this keyword covered by the blob? Verbatim OR every piece present."""
    if _norm(kw) in blob_norm:
        return True
    pieces = _tokens(kw)
    return bool(pieces) and all(p in blob_tokens for p in pieces)


def keyword_coverage(results: list[dict], expected_keywords: list[str]) -> list[str]:
    """Which expected keywords appear in the RECALLED rows (lenient matching).

    Mirrors eval_oracle.coverage() in shape -- the blob is the concatenation
    of the matching rows' contents -- but tolerates the spelling variants
    the eval set and the corpus disagree on.
    """
    kws = [k.strip().lower() for k in (expected_keywords or []) if k and k.strip()]
    if not kws:
        return []
    blob_parts = []
    for r in results:
        content = (r.get("content") or r.get("text") or "").lower()
        if any(kw in content for kw in kws):
            blob_parts.append(content)
    blob = "\n".join(blob_parts)
    bn = _norm(blob)
    bt = set(re.findall(r"[a-z0-9]+", blob.lower()))
    return [k for k in kws if _covered(k, bn, bt)]


def run(eval_set: list[dict], top_k_default: int = 10) -> dict:
    detail = []
    latencies = []
    for q in eval_set:
        tier = q.get("tier", "private")
        user = q.get("user", "admin")
        top_k = q.get("top_k", top_k_default)
        try:
            results, lat_ms = recall(q["query"], tier, user, top_k)
        except Exception as e:
            detail.append({"qid": q["qid"], "error": str(e)[:160]})
            continue
        latencies.append(lat_ms)

        kws = q.get("expected_keywords", [])
        oracle = _oracle_coverage(kws, tier=tier, user=user, min_hits=q.get("min_hits", 1))
        oracle_cov = [k.lower() for k in (oracle.get("oracle_covered") or [])]
        recall_cov = keyword_coverage(results, oracle_cov)

        # ID-level evidence set: the facts the STORE holds for this query.
        # Language-independent, so it does not inherit the eval set's
        # vocabulary from the keyword side.
        evidence_ids = {o["id"] for o in _oracle_search(kws, tier=tier, user=user, limit=500)}
        recalled_ids = [r.get("fact_id") or r.get("id") for r in results]
        evidence_recalled = sum(1 for i in recalled_ids if i in evidence_ids)

        needed = set(oracle_cov)
        got = set(recall_cov)
        missing = sorted(needed - got)
        detail.append({
            "qid": q["qid"],
            "query": q["query"],
            "category": q.get("category"),
            "tier": tier,
            "latency_ms": round(lat_ms, 2),
            "n_results": len(results),
            "n_keywords": len(kws),
            "n_evidence_facts": len(evidence_ids),
            "evidence_facts_recalled": evidence_recalled,
            "evidence_recall_ratio": round(evidence_recalled / len(evidence_ids), 4) if evidence_ids else 0.0,
            "oracle_covered": sorted(oracle_cov),
            "recall_covered": sorted(recall_cov),
            "missing_from_recall": missing,
            "evidence_complete": bool(needed) and not missing,
            "oracle_hit": bool(oracle.get("oracle_hit")),
            "expected_miss": q.get("expected_miss", False),
        })

    scored = [d for d in detail if not d.get("expected_miss") and "error" not in d]
    # Only queries whose evidence the store fully holds can be scored for
    # completeness. Restricting the denominator is the whole point: a
    # storage gap must not be charged to the read path.
    eligible = [d for d in scored if d["oracle_hit"] and d["n_keywords"] > 0]

    complete = sum(1 for d in eligible if d["evidence_complete"])
    n_elig = len(eligible)
    crr = (complete / n_elig) if n_elig else 0.0

    # ID-level: did at least one of the answer-bearing facts come back?
    # A query whose answer needs several facts may only surface one of
    # them at top-k=10 without being a retrieval failure -- the ratio
    # reports the depth, the rate reports the presence.
    evidence_hit = sum(1 for d in eligible if d["evidence_facts_recalled"] > 0)
    evidence_rate = (evidence_hit / n_elig) if n_elig else 0.0
    mean_ratio = (statistics.mean(d["evidence_recall_ratio"] for d in eligible)
                  if eligible else 0.0)

    summary = {
        "metric": "complete_retrieval_rate",
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_queries": len(scored),
        "n_eligible": n_elig,
        "n_complete": complete,
        # keyword-level: what the eval set's vocabulary names
        "complete_retrieval_rate": round(crr, 4),
        # ID-level: language-independent, the honest reading
        "evidence_hit_rate": round(evidence_rate, 4),
        "mean_evidence_recall_ratio": round(mean_ratio, 4),
        "storage_gap_queries": len(scored) - n_elig,
        "avg_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0,
        "by_category": {},
    }
    cats: dict[str, list[dict]] = {}
    for d in eligible:
        cats.setdefault(d.get("category") or "unknown", []).append(d)
    for cat, ds in cats.items():
        summary["by_category"][cat] = {
            "n": len(ds),
            "complete_retrieval_rate": round(
                sum(1 for x in ds if x["evidence_complete"]) / len(ds), 4),
            "evidence_hit_rate": round(
                sum(1 for x in ds if x["evidence_facts_recalled"] > 0) / len(ds), 4),
            "mean_evidence_recall_ratio": round(statistics.mean(
                x["evidence_recall_ratio"] for x in ds), 4),
        }

    return {"summary": summary, "detail": detail,
            "incomplete_queries": [
                {"qid": d["qid"], "query": d["query"],
                 "missing_from_recall": d["missing_from_recall"],
                 "evidence_facts_recalled": d["evidence_facts_recalled"],
                 "n_evidence_facts": d["n_evidence_facts"]}
                for d in eligible if not d["evidence_complete"]]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(EVAL_SET))
    ap.add_argument("--out", default=str(METRICS_DIR / "eval_complete_latest.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    set_path = Path(args.set)
    if not set_path.exists():
        print(f"ERROR: eval set not found at {set_path}", file=sys.stderr)
        return 2

    eval_set = load_eval_set(set_path)
    print(f"Loaded {len(eval_set)} queries from {set_path}")
    print(f"Server: {SERVER}\n")

    result = run(eval_set)
    s = result["summary"]

    print("=== COMPLETE RETRIEVAL RATE ===")
    print(f"  eligible queries        = {s['n_eligible']} / {s['n_queries']}"
          f"  ({s['storage_gap_queries']} storage-gap, excluded)")
    print(f"  [keyword] complete_retrieval_rate   = {s['complete_retrieval_rate']:.4f}")
    print(f"  [ID]      evidence_hit_rate         = {s['evidence_hit_rate']:.4f}")
    print(f"  [ID]      mean_evidence_recall_ratio= {s['mean_evidence_recall_ratio']:.4f}")
    if s["by_category"]:
        print("  by category:")
        for cat, m in sorted(s["by_category"].items()):
            print(f"    {cat:12} n={m['n']:>3}  kw_crr={m['complete_retrieval_rate']:.3f}"
                  f"  ev_hit={m['evidence_hit_rate']:.3f}  ev_ratio={m['mean_evidence_recall_ratio']:.3f}")
    inc = result["incomplete_queries"]
    if inc:
        print(f"  keyword-incomplete ({len(inc)}):")
        for m in inc[:12]:
            print(f"    {m['qid']}: {m['query'][:44]}  missing={m['missing_from_recall']}"
                  f"  evidence {m['evidence_facts_recalled']}/{m['n_evidence_facts']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
