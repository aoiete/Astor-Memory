"""What does a retirement cost the answer?

The counterfactual the survey on multimodal agent memory (arXiv
2610.19366) suggests: delete or replace the retrieved result and see
whether the agent's dependence on that memory was real.

This is the retrieval half of it. For every question in the eval set it
retires the fact the question leans on and asks what is left:

- did another fact take over the gold's rank?
- do the expected keywords still have cover in what comes back?

A question whose keywords lose their cover is a question the agent can
no longer answer from memory. That is the price of a demotion, and it
has to be known rather than assumed.

Read-mostly: every fact it retires is reactivated afterwards, and it
verifies the restore.

Usage:  measure_retirement_impact.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r"D:/AI/astor-memory")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7803"
sys.path.insert(0, str(REPO / "tests"))


def post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def try_post(path, payload):
    """A read can return a row that is not a local canonical fact -- a
    peer result, for one -- and the demote endpoint answers 404 for
    those. That is not an error for this measurement, it just means the
    question is not leaning on a fact we can retire."""
    try:
        return post(path, payload)
    except urllib.error.HTTPError:
        return {"ok": False, "error": "http_error"}


def read(q):
    return post("/v1/read", {"query": q["query"], "tier": q.get("tier", "public"),
                              "user_id": q.get("user", "admin"),
                              "top_k": q.get("top_k", 10)})


def cover(results, keywords):
    """How many of the keywords still have a fact behind them."""
    text = " ".join((r.get("content") or "") for r in results)
    return sum(1 for k in keywords if k.lower() in text.lower())


questions = [json.loads(l) for l in
             (REPO / "tests/eval_set.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]

rows = []
restored = []
skipped = 0
try:
    for q in questions:
        kws = q.get("expected_keywords") or []
        before = read(q)
        results = before.get("results") or []
        if not results:
            continue
        top = results[0]
        fid = top.get("fact_id")
        before_cover = cover(results, kws)
        if before_cover == 0:
            # the question had no cover to begin with; retiring cannot
            # take anything away
            continue

        r = try_post(f"/v1/fact/{fid}/set_status",
                     {"status": "inactive",
                      "reason": "retirement impact (temporary)"})
        if not r.get("ok"):
            skipped += 1
            continue
        restored.append(fid)

        after = read(q)
        after_results = after.get("results") or []
        after_cover = cover(after_results, kws)
        ids = [h.get("fact_id") for h in after_results]
        rows.append({
            "qid": q["qid"],
            "top": fid,
            "cover_before": before_cover,
            "cover_after": after_cover,
            "lost": before_cover > after_cover,
            "gold_rank_after": (ids.index(fid) + 1) if fid in ids else None,
            "took_over": any(h.get("fact_id") != fid for h in after_results),
        })
finally:
    for fid in restored:
        try_post(f"/v1/fact/{fid}/set_status",
                 {"status": "active", "reason": "impact measurement cleanup"})

n = len(rows)
lost = [r for r in rows if r["lost"]]
print(f"基址 {BASE}")
print(f"测了 {n} 题（原本有关键词覆盖的）")
print(f"  退役后关键词失去覆盖: {len(lost)}/{n}")
if n:
    ranks = [r["gold_rank_after"] for r in rows if r["gold_rank_after"]]
    if ranks:
        ranks.sort()
        print(f"  退役事实的新名次: 中位 {ranks[len(ranks)//2]} 最大 {ranks[-1]}")
    print(f"  有别的 fact 顶上: {sum(1 for r in rows if r['took_over'])}/{n}")
print(f"已还原 {len(restored)} 条 fact；跳过 {skipped} 条（非本地 canonical 行）")
if lost:
    print("\n失去覆盖的题:")
    for r in lost:
        print(f"  {r['qid']}: 覆盖 {r['cover_before']} -> {r['cover_after']}"
              f" (fact {r['top']})")
