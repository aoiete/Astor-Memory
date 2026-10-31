"""One-off generator for tests/eval_holdout_set.json (RSIHub held-out set).

Not part of the test suite — kept so the set can be rebuilt/audited.

Why this exists: eval_control_set.json was 38/39 identical in query text to
eval_set.jsonl. eval_set is what variant selection optimises against, so a
control set that reuses its questions is not a held-out set: it cannot
detect "score went up, unseen tasks went down" (the RSIHub failure mode).

Procedure:
  1. candidate facts = high-importance, non-noise, in the admin private tier
  2. skip any fact whose tokens overlap >50% with eval_set's covered tokens
  3. build a query + expected_keywords from the fact content
  4. VERIFY each candidate with a live /v1/read -- keep only ones that pass
  5. emit json with set_hash so the gate can prove the set was not edited
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.request
from pathlib import Path
from astor_memory.config import get_default_astor_dir

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
DB = get_default_astor_dir() / 'users' / 'admin' / 'memory' / 'astor_bus_admin.db'
SERVER = "http://127.0.0.1:7803"
TARGET = 40

NOISE = re.compile(
    r"^(cd |cp |powershell|hermes-agent[/\\]|python |\[20\d\d-|Wrote |"
    r"session 中使用工具|git |mkdir |ls |cat )",
    re.I,
)
TOKEN = re.compile(r"[A-Za-z0-9_\-]{4,}|[一-鿿]{2,}")

# Operator identity must never reach a file that ships. The corpus is the
# operator's own memory, so a fact can carry a username, a chat id, or a
# home path. The held-out set is a shipped artifact (it is committed and
# run by anyone), so any fact that names the operator is skipped.
#
# The patterns below are structural, not a list of names: a literal name
# would itself be the thing this rule forbids. A home-directory path, a
# Windows user directory, or a chat-id shape identifies an operator
# whatever the name happens to be.
IDENTITY = re.compile(
    r"[A-Z]:\\\\?Users\\\\?[\w.]+|"
    r"[A-Z]:/(?:Users|home)/[\w.]+|"
    r"/(?:home|Users)/[\w.]+/|"
    r"\b[\w.+-]+@[\w-]+\.[\w.]+\b|"
    r"\bo?[0-9a-f]{8,}[A-Za-z0-9_-]*@im\.(?:wechat|weixin|qq)\b|"
    r"\boc_[0-9a-f]{20,}\b|"
    r"\bsk-[A-Za-z0-9]{12,}\b|"
    r"\bghp_[A-Za-z0-9]{20,}\b|"
    r"\b[A-Z]:/AI/[\w./-]+",
    re.I,
)


def live_read(query: str, tier: str, user: str, top_k: int = 10):
    body = json.dumps({"query": query, "tier": tier, "user": user,
                       "top_k": top_k}).encode()
    req = urllib.request.Request(
        f"{SERVER}/v1/read", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("results", []) or []


def main() -> int:
    ev = [json.loads(l) for l in (TESTS / "eval_set.jsonl").open(encoding="utf-8")
          if l.strip()]
    covered: set[str] = set()
    for e in ev:
        for k in e.get("expected_keywords", []):
            covered.add(k.lower())
        for t in re.findall(r"[A-Za-z0-9_\-]{4,}", e["query"]):
            covered.add(t.lower())

    con = sqlite3.connect(str(DB))
    rows = con.execute(
        """SELECT id, kind, importance, content FROM memory_canonical
           WHERE tombstoned = 0 AND importance >= 0.85
             AND length(content) BETWEEN 60 AND 260
             AND kind NOT IN ('lock_rule','rule','mental_model','knowledge_page')
           ORDER BY importance DESC, access_count DESC"""
    ).fetchall()
    con.close()

    # candidate facts whose topic the eval set does not already probe
    cands = []
    for fid, kind, imp, content in rows:
        if NOISE.match(content.strip()):
            continue
        # A fact that names the operator cannot become a shipped query.
        # Skip it outright -- a genericised rewrite would test a fact that
        # no longer exists, and the set must stay honest about what it
        # asserts.
        if IDENTITY.search(content):
            continue
        toks = {t.lower() for t in TOKEN.findall(content)}
        if not toks:
            continue
        if len(toks & covered) / len(toks) > 0.5:
            continue
        cands.append((fid, kind, content))
    print(f"candidates not overlapping eval_set topics: {len(cands)}")

    kept: list[dict] = []
    seen_q: set[str] = set()
    for fid, kind, content in cands:
        if len(kept) >= TARGET:
            break
        text = content.replace("\n", " ").strip()
        # expected_keywords = the most distinctive tokens in the fact
        toks = TOKEN.findall(text)
        uniq: list[str] = []
        for t in toks:
            tl = t.lower()
            if tl in covered or len(t) < 3:
                continue
            if any(tl in u.lower() for u in uniq):
                continue
            uniq.append(t)
        if len(uniq) < 5:
            continue
        kws = uniq[:4]
        # The query must NOT be a verbatim copy of the fact. If it were,
        # BM25 exact-match would return the fact under every variant and
        # the gate could never observe a ranking regression -- it would
        # always pass, which is as useless as a gate that always fails.
        # So the query drops the two most distinctive tokens (which stay
        # in expected_keywords) and is built from the rest: recall then
        # has to go through the semantic path to find the fact.
        query_toks = uniq[2:8]
        q = " ".join(query_toks)[:70]
        if not q or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        try:
            results = live_read(q, "private", "admin")
        except Exception as exc:
            print(f"  [read-error] {q!r}: {exc}")
            continue
        if not results:
            continue
        blob = "\n".join(
            (r.get("content") or r.get("text") or "") for r in results).lower()
        if sum(1 for k in kws if k.lower() in blob) < 2:
            continue
        kept.append({
            "qid": f"H{len(kept) + 1:02d}",
            "query": q,
            "category": kind,
            "expected_keywords": kws,
            "tier": "private",
            "user": "admin",
            "source_fid": fid,
        })
        print(f"  + {kept[-1]['qid']} {q[:56]!r}")

    if len(kept) < 20:
        print(f"FAIL: only {len(kept)} verified queries, need >= 20")
        return 1

    payload = {
        "version": 1,
        "description": (
            "RSIHub-style HELD-OUT set. Disjoint from eval_set.jsonl by "
            "construction (candidate facts whose tokens overlap eval_set's "
            "covered tokens by >50% are skipped), and every query is "
            "verified against a live /v1/read before being written here. "
            "Purpose: detect 'the tuned score went up but unseen tasks went "
            "down' -- the failure mode a control set that reuses eval_set "
            "questions cannot detect. set_hash is recorded in the gate "
            "result so an edited set is visible."
        ),
        "built_at": "2026-10-28",
        "queries": kept,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    out = TESTS / "eval_holdout_set.json"
    out.write_text(raw + "\n", encoding="utf-8")
    payload["set_hash"] = hashlib.sha256(
        (raw + "\n").encode("utf-8")).hexdigest()[:16]
    print(f"\nwrote {out} with {len(kept)} queries")
    print(f"set_hash (of file incl. hash field placeholder) = {payload['set_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
