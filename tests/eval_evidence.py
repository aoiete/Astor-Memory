"""A ruler for retrieval changes that does not need hand annotation.

The keyword ruler answers "does recall return anything about this
topic". It cannot answer "is the RIGHT fact at the top", because it
has no right fact -- the eval set is written by topic, not by fact.

This builds one. For each question the gold is the fact that contains
the most distinct expected keywords; ties go to the higher
importance, then the lower id. That rule is lexical and fixed, so it
does not move when the retrieval path is changed -- which is the whole
requirement for an A/B ruler. It also covers every question, where
requiring all keywords at once left 44 of 110 with no candidate.

**What this ruler can and cannot say.** It measures whether the
ranking layer surfaces the best lexical match. That is the right
question for a change to the lexical half -- bm25_weight, the rerank
stage, the top_k clamp. It does not measure semantic quality: the gold
is chosen lexically, so a change that only moves the vector half will
look flat here even if it helps. Read a flat result as "this change
does not affect the lexical ranking", not as "this change does
nothing".

The gold is written to a file and reused, so two runs of two
configurations compare against the same set. Regenerate it only when
the store changes materially.

    python tests/eval_evidence.py --build      # write the gold set
    python tests/eval_evidence.py --report     # score the current config
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from eval_oracle import bus_path  # noqa: E402

EVAL_SET = HERE / "eval_set.jsonl"
GOLD_FILE = HERE / "eval_gold.jsonl"
# configurable so a second instance can be measured against the same gold
ENDPOINT = os.environ.get("ASTOR_ENDPOINT", "http://127.0.0.1:7803")


def _questions():
    out = []
    with EVAL_SET.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _rows_for(q):
    """Every fact that mentions at least one expected keyword."""
    kws = [k.lower() for k in (q.get("expected_keywords") or []) if k]
    if not kws:
        return []
    db = bus_path(q.get("tier", "public"), q.get("user"))
    if db is None:
        return []
    where = " OR ".join(["LOWER(content) LIKE ?" for _ in kws])
    params = [f"%{k}%" for k in kws]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return []
    try:
        # The parentheses are load-bearing: SQL binds AND tighter than
        # OR, so without them the tombstone filter applies to the first
        # keyword only and retired rows come back.
        return con.execute(
            "SELECT id, content, importance FROM memory_canonical "
            f"WHERE tombstoned = 0 AND ({where}) "
            "ORDER BY importance DESC, id DESC", params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _gold_for(q):
    """The fact that mentions the most of the question's keywords."""
    kws = [k.lower() for k in (q.get("expected_keywords") or []) if k]
    best = None
    for fid, content, importance in _rows_for(q):
        low = (content or "").lower()
        hits = sum(1 for k in kws if k in low)
        if hits == 0:
            continue
        key = (hits, importance or 0.0, -fid)
        if best is None or key > best[0]:
            best = (key, fid, content, hits)
    if best is None:
        return None
    # The content preview is real user data, so it stays out of the file
    # the repository carries. The (qid -> fact_id) mapping is the whole
    # value of the gold; the preview is only for a human check, and
    # --show prints it.
    import hashlib
    return {"qid": q["qid"], "fact_id": best[1], "keyword_hits": best[3],
            "content_sha": hashlib.sha1(
                (best[2] or "").encode("utf-8")).hexdigest()[:12]}


def build():
    gold = []
    missing = []
    for q in _questions():
        g = _gold_for(q)
        if g is None:
            missing.append(q["qid"])
        else:
            gold.append(g)
    with GOLD_FILE.open("w", encoding="utf-8") as fh:
        for g in gold:
            fh.write(json.dumps(g, ensure_ascii=False) + "\n")
    print(f"[gold] wrote {len(gold)} entries to {GOLD_FILE.name}")
    if missing:
        print(f"[gold] {len(missing)} question(s) had no candidate: {missing}")
    hits = sum(1 for g in gold if g["keyword_hits"] >= 2)
    print(f"[gold] {hits}/{len(gold)} golds match at least two keywords "
          f"(the rest match one, which is a weaker pick)")
    return 0


def _read(query, tier, user, top_k):
    body = json.dumps({"query": query, "tier": tier, "user_id": user,
                        "top_k": top_k}).encode()
    req = urllib.request.Request(ENDPOINT + "/v1/read", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data, (time.time() - t0) * 1000


def report():
    if not GOLD_FILE.exists():
        print("[gold] missing -- run with --build first")
        return 1
    gold = {json.loads(l)["qid"]: json.loads(l)
            for l in GOLD_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    questions = {q["qid"]: q for q in _questions()}

    at1 = at5 = at10 = 0
    rr = 0.0
    scored = 0
    latencies = []
    for qid, g in gold.items():
        q = questions.get(qid)
        if q is None:
            continue
        data, ms = _read(q["query"], q.get("tier", "public"),
                         q.get("user"), 10)
        latencies.append(ms)
        ids = [h.get("fact_id") for h in (data.get("results") or [])]
        scored += 1
        if not ids:
            continue
        if ids[0] == g["fact_id"]:
            at1 += 1
        if g["fact_id"] in ids[:5]:
            at5 += 1
        if g["fact_id"] in ids[:10]:
            at10 += 1
        if g["fact_id"] in ids:
            rr += 1.0 / (ids.index(g["fact_id"]) + 1)

    if not scored:
        print("[report] nothing scored")
        return 1
    print(f"[report] scored {scored} question(s)")
    print(f"         recall@1  {at1/scored:.3f}")
    print(f"         recall@5  {at5/scored:.3f}")
    print(f"         recall@10 {at10/scored:.3f}")
    print(f"         MRR       {rr/scored:.3f}")
    print(f"         latency   {sum(latencies)/len(latencies):.0f} ms "
          f"(mean over {len(latencies)})")
    print()
    print("  This ruler compares the LEXICAL ranking. A change that only")
    print("  moves the vector half reads flat here.")
    return 0


def show_gold():
    """Print the gold picks with their content, for a human check."""
    if not GOLD_FILE.exists():
        print("[gold] missing -- run with --build first")
        return 1
    questions = {q["qid"]: q for q in _questions()}
    for line in GOLD_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        g = json.loads(line)
        q = questions.get(g["qid"], {})
        rows = _rows_for(q)
        content = next((r[1] for r in rows if r[0] == g["fact_id"]), "")
        print(f"  {g['qid']:5} fact {g['fact_id']:<7} hits={g['keyword_hits']}  "
              f"{str(content)[:110]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true",
                    help="(re)write the gold set from the current store")
    ap.add_argument("--report", action="store_true",
                    help="score the current retrieval configuration")
    ap.add_argument("--show", action="store_true",
                    help="print the gold picks with their content (not stored "
                         "in the gold file, because it is real user data)")
    args = ap.parse_args()
    if args.build:
        return build()
    if args.show:
        return show_gold()
    if args.report:
        return report()
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
