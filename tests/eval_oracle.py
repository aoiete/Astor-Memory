"""eval_oracle.py — separate "the store never had it" from "we did not find it".

Why this exists
---------------
Every metric astor has measured so far is a RETRIEVAL metric: hit_rate,
mrr, ndcg. They all look at what came back from `/v1/read`. That leaves
one question unanswered, and it is the expensive one:

    a query missed -- because the evidence was never stored,
    or because the evidence was stored and the search did not surface it?

The two have opposite fixes. The first is a write-path problem (the fact
was never written, or was written as junk). The second is a retrieval
problem (ranking, weights, index staleness). A single hit_rate cannot
tell you which one you are looking at, so every fix has to be tried
blind.

Uni-Mem (arXiv 2610.29671, Huawei Noah / SEU / SYSU) hits exactly this
wall in a different setting. Its Event-Wise Credit Assignment step scores
memory with a FROZEN ANSWER MODEL reading the evidence record directly,
precisely so that "stored badly" and "searched badly" get punished
separately. The answer model is the expensive part; the idea is not.

What this module does
---------------------
`oracle_search` asks the store what it actually holds, with no ranking
layer in between: a direct SQL scan over `memory_canonical` for rows
whose content contains the expected keywords. Same keywords, same
scoring rule as the retrieval path -- the ONLY difference is whether the
result went through embedding + BM25 + rerank + MMR.

So for every query we get three numbers instead of one:

    oracle_hit    the store HAS a row that covers the query
    recall_hit    /v1/read actually returned a matching row
    storage_gap   oracle_hit is False  -> the write path lost it
    retrieval_gap oracle_hit and not recall_hit -> the read path lost it

Read-only: it opens the bus DB in `mode=ro` and never writes.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Where the runtime keeps its bus databases. Public is a tier directory;
# a private tier lives under users/<user>/memory/. (The tier NAME is
# public/private/source -- it is not a directory prefix.)
def _runtime_dir() -> Path:
    return Path(os.environ.get("ASTOR_DIR") or (Path.home() / ".astor"))


def bus_path(tier: str = "public", user: str | None = None) -> Path | None:
    """Locate the bus DB for a tier/user, or None if it does not exist."""
    root = _runtime_dir()
    if tier == "private":
        if not user:
            return None
        cand = root / "users" / user / "memory" / f"astor_bus_{user}.db"
    else:
        cand = root / tier / "memory" / f"astor_bus_{tier}.db"
    return cand if cand.exists() else None


def _tokens(expected_keywords: list[str]) -> list[str]:
    return [k.strip().lower() for k in (expected_keywords or []) if k and k.strip()]


def oracle_search(
    expected_keywords: list[str],
    tier: str = "public",
    user: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Rows whose content contains ANY expected keyword.

    Same rule as the retrieval path's score_results(): a row counts when
    it contains one of the keywords. Scoring the two sides with
    different rules would manufacture a gap that does not exist -- an
    AND here against an OR there would report every multi-keyword query
    as a storage miss. The only difference between the two sides must be
    the ranking layer.
    """
    kws = _tokens(expected_keywords)
    if not kws:
        return []
    db = bus_path(tier, user)
    if db is None:
        return []
    where = " OR ".join(["LOWER(content) LIKE ?" for _ in kws])
    params = [f"%{k}%" for k in kws]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT id, content FROM memory_canonical "
            f"WHERE tombstoned = 0 AND {where} "
            "ORDER BY importance DESC, id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"id": r[0], "content": r[1]} for r in rows]


def classify(oracle_hit: bool, recall_hit: bool) -> str:
    """Which side of the wall did this query fall off?"""
    if not oracle_hit:
        return "storage_gap"      # the store never held it
    if not recall_hit:
        return "retrieval_gap"   # it was there, the search missed it
    return "hit"


def coverage(expected_keywords: list[str], tier: str = "public",
             user: str | None = None, min_hits: int = 1) -> dict:
    """Oracle coverage of one query -- the store-side view, no ranking.

    Scored exactly like the retrieval side: count the rows that contain
    one of the keywords; the query is a hit when that count reaches
    min_hits.
    """
    kws = _tokens(expected_keywords)
    rows = oracle_search(expected_keywords, tier=tier, user=user, limit=50)
    if not rows:
        return {"oracle_hit": False, "oracle_n": 0,
                "oracle_covered": [], "oracle_missed": list(kws)}
    n_match = 0
    blob_parts = []
    for r in rows:
        content = (r.get("content") or "").lower()
        if any(kw in content for kw in kws):
            n_match += 1
            blob_parts.append(content)
    blob = "\n".join(blob_parts)
    covered = [k for k in kws if k in blob]
    return {
        "oracle_hit": n_match >= max(1, min_hits),
        "oracle_n": n_match,
        "oracle_covered": covered,
        "oracle_missed": [k for k in kws if k not in blob],
    }


__all__ = ["bus_path", "oracle_search", "classify", "coverage"]
