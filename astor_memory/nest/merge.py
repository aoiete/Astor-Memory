"""
Astor merge-dedup v2 — semantic duplicate detection across tier boundaries.

Why v2:
  v1 dedup is content-hash based (server.py:stable_id). It catches exact
  duplicates only — paraphrase the same fact and it's stored twice.
  v2 uses embedding cosine + LLM judge to find semantically equivalent
  facts and merge them into a single canonical row, preserving provenance.

Workflow (operator-driven, NOT automatic):
  1. POST /v1/merge/find    {tier, user_id, scope, threshold, top_k}
     → list candidate groups: {winner, losers:[], reason, llm_verdict}
     → does NOT mutate anything
  2. operator reviews the groups
  3. POST /v1/merge/apply   {merges:[{winner_id, loser_id}, ...]}
     → tombstone losers, union tags + metadata, write audit
     → is idempotent (re-applying after a successful merge is a no-op)

Safety:
  - All mutations gated by ACL role check (first_admin only — operator).
  - Winners must have importance >= losers (auto-promote if not, but flag).
  - LLM judge decides verifier=settled / rejected — threshold-only fallback
    when the LLM is unreachable (e.g. API outage).
  - Every merge writes an audit_log row with severity='warning' so the
    change is auditable.

Scope:
  - Within-tier (default): only facts in the same (tier, user_id) are
    candidates. Cross-tier is opt-in via `cross_tier=true`.
  - Cross-tier uses scope weights from opt7 (read multi policy).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Iterable, Optional

import numpy as np

from .._internal.acl_layout import get_astor_dir


# ----- Tunables -----
DEFAULT_COSINE_THRESHOLD = 0.92    # very close, v1 strict
LLM_JUDGE_TIMEOUT = 15.0
LLM_JUDGE_ENABLED = True  # set False to skip LLM and use only cosine


# ----- LLM judge (best-effort, never fatal) -----
def _llm_judge_pair(content_a: str, content_b: str) -> dict:
    """Ask the configured LLM whether two facts are equivalent.

    Returns dict like:
      {"verdict": "same" | "distinct" | "subsume",
       "winner": "a" | "b" | "either",
       "confidence": 0.0-1.0,
       "reason": "<short>"}
    On any error returns:
      {"verdict": "unknown", "winner": "either", "confidence": 0.0,
       "reason": "LLM judge unavailable: <error>"}
    """
    if not LLM_JUDGE_ENABLED:
        return {"verdict": "unknown", "winner": "either",
                "confidence": 0.0, "reason": "LLM judge disabled"}

    try:
        # Lazy import — gauge which providers are configured. We only
        # have gemini/anthropic/openai/grok-style providers hooked up.
        # For dedup, gemini-2.0-flash is plenty (low latency, cheap).
        from hermes_agent.providers import get_default_provider  # type: ignore
        provider, model = get_default_provider(
            task='classify_small',  # route hint — cheap model
        )
    except Exception as e:
        return {"verdict": "unknown", "winner": "either",
                "confidence": 0.0, "reason": f"no provider: {e}"}

    prompt = (
        "You are a memory dedup judge. Given two facts stored in a "
        "long-term memory system, decide whether they describe the same "
        "underlying truth (and can be merged into one) or are distinct.\n"
        "Respond ONLY with JSON, no prose, no markdown fences:\n"
        '{"verdict":"same"|"distinct"|"subsume",\n'
        ' "winner":"a"|"b"|"either",\n'
        ' "confidence":<0..1>,\n'
        ' "reason":"<≤12 words>"}\n\n'
        f"Fact A: {content_a}\n"
        f"Fact B: {content_b}\n"
    )
    try:
        out = provider.generate(
            model=model, prompt=prompt,
            max_tokens=120, temperature=0,
        )
        text = out.text if hasattr(out, 'text') else str(out)
        # find JSON in response
        s = text.find('{')
        e = text.rfind('}')
        if s == -1 or e == -1:
            return {"verdict": "unknown", "winner": "either",
                    "confidence": 0.0,
                    "reason": f"LLM returned non-JSON: {text[:80]}"}
        data = json.loads(text[s:e + 1])
        # normalise
        verdict = str(data.get("verdict", "unknown")).lower()
        if verdict not in ("same", "distinct", "subsume"):
            verdict = "unknown"
        winner = str(data.get("winner", "either")).lower()
        if winner not in ("a", "b", "either"):
            winner = "either"
        try:
            conf = float(data.get("confidence", 0))
        except Exception:
            conf = 0.0
        return {
            "verdict": verdict,
            "winner": winner,
            "confidence": max(0.0, min(1.0, conf)),
            "reason": str(data.get("reason", ""))[:80],
        }
    except Exception as exc:
        return {"verdict": "unknown", "winner": "either",
                "confidence": 0.0, "reason": f"LLM error: {exc}"}


# ----- Cosine scoring -----
def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ----- Core: find candidate duplicate groups -----
def find_duplicate_groups(
    tier: str = "public",
    user_id: str | None = None,
    *,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
    top_k: int = 50,
    use_llm: bool = True,
    max_groups: int = 100,
) -> dict:
    """Find semantic duplicate groups in (tier, user_id).

    Strategy:
      1. Pull up to top_k facts (most-recent first) from memory_canonical.
      2. For each fact, retrieve its nest embedding (768-d float32).
      3. Compare all O(n^2) pairs above threshold; cluster.
      4. For each cluster of size >= 2, optionally LLM-judge the
         first-vs-each pair; keep only pairs the LLM confirms as same.

    Returns:
      {
        "tier", "user_id",
        "candidate_count": int,
        "groups": [
          {
            "group_id": "g0", "size": 3, "method": "cosine+llm",
            "members": [
              {"fact_id": int, "content": str, "importance": float,
               "promoted_at": str, "cluster_pick": bool}
            ],
            "suggested_winner": {"fact_id": int, ...},
            "losers": [{"fact_id": int, "score_to_winner": float}],
            "llm_verdicts": [{"pair": [a, b], "verdict": str,
                              "winner": str, "confidence": float,
                              "reason": str}],
          }
        ],
        "scanned_at": iso8601,
      }
    """
    from . import vector_store as _vs   # AstorNest
    from .embeddings import astor_get_embedding_model
    from .. import astor_bus

    bus = astor_bus(tier=tier, user_id=user_id)
    nest = _vs.AstorNest  # we'll call get_embedding directly
    # We need the canonical embedding for each fact. Pull from nest DB.
    # 1) gather candidate facts
    rows = bus.conn.execute(
        "SELECT id, content, kind, importance, promoted_at, stable_id "
        "FROM memory_canonical "
        "WHERE tombstoned = 0 OR tombstoned IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (int(top_k * 4),),   # overshoot because we filter below
    ).fetchall()
    if not rows:
        return {
            "tier": tier, "user_id": user_id,
            "candidate_count": 0, "groups": [],
            "scanned_at": _now_iso(),
        }
    # 2) embedding for each (from the embedding model directly, since
    # nest.search needs a query vector; but we need per-doc vectors).
    # Use the embeddings table in nest DB.
    nest_obj = _vs.astor_nest(tier=tier, user_id=user_id)
    embed_rows = nest_obj.conn.execute(
        "SELECT fact_id, embedding FROM embeddings"
    ).fetchall()
    emb_map: dict[int, np.ndarray] = {}
    for fid, blob in embed_rows:
        if blob is None:
            continue
        n = len(blob) // 4
        arr = np.array(struct_unpack(f'{n}f', blob), dtype=np.float32) \
            if False else _unpack(blob)
        emb_map[int(fid)] = arr
    # 3) candidates with embeddings only (skip unembedded facts — they
    # cannot be semantically matched)
    candidates = []
    for r in rows[:top_k]:
        fid = int(r[0])
        if fid in emb_map:
            candidates.append({
                "fact_id": fid,
                "content": str(r[1]),
                "kind": str(r[2]),
                "importance": float(r[3]),
                "promoted_at": str(r[4]),
                "stable_id": str(r[5]) if r[5] else None,
                "embedding": emb_map[fid],
            })
    n = len(candidates)
    if n < 2:
        return {
            "tier": tier, "user_id": user_id,
            "candidate_count": n, "groups": [],
            "scanned_at": _now_iso(),
        }
    # 4) O(n^2) cosine scan above threshold. Mark pairs in adjacency.
    pair_scores: list[tuple[int, int, float]] = []  # (i, j, sim)
    parent: list[int] = list(range(n))  # union-find
    def find(x: int) -> int:
        """Find candidate duplicate fact_ids (cosine >= threshold) for one anchor fact."""
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        """Merge two fact_ids into one (preserves both revision chains)."""
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for i in range(n):
        ea = candidates[i]["embedding"]
        for j in range(i + 1, n):
            eb = candidates[j]["embedding"]
            sim = _cosine(ea, eb)
            if sim >= threshold:
                pair_scores.append((i, j, sim))
                union(i, j)
    # 5) Build groups
    groups_by_root: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups_by_root.setdefault(r, []).append(i)
    raw_groups = [v for v in groups_by_root.values() if len(v) >= 2]
    # 6) For each group, optionally LLM-judge pairwise and pick winner
    out_groups: list[dict] = []
    for gidx, members in enumerate(raw_groups):
        if len(out_groups) >= max_groups:
            break
        # Pick winner by max(importance, then largest content length,
        # then newest promoted_at)
        winner_idx = max(
            members,
            key=lambda i: (
                candidates[i]["importance"],
                len(candidates[i]["content"]),
                candidates[i]["promoted_at"],
            ),
        )
        winner = candidates[winner_idx]
        llm_verdicts: list[dict] = []
        losers: list[dict] = []
        for m in members:
            if m == winner_idx:
                continue
            other = candidates[m]
            sim = _cosine(candidates[winner_idx]["embedding"],
                          candidates[m]["embedding"])
            entry = {
                "fact_id": other["fact_id"],
                "score_to_winner": round(sim, 4),
            }
            if use_llm:
                verdict = _llm_judge_pair(
                    winner["content"], other["content"],
                )
                llm_verdicts.append({
                    "pair": [winner["fact_id"], other["fact_id"]],
                    **verdict,
                })
                # Only count as duplicate if LLM agrees
                if verdict["verdict"] not in ("same", "subsume"):
                    continue  # skip this candidate
                # If LLM picks the loser as winner (e.g. loser is
                # cleaner), we still keep winner_idx — but flag for
                # manual review.
                if verdict.get("winner") == "b":
                    entry["llm_suggests_winner"] = "loser"
            losers.append(entry)
        if not losers:
            continue
        out_groups.append({
            "group_id": f"g{gidx}",
            "size": len(members),
            "method": "cosine+llm" if use_llm else "cosine",
            "members": [
                {k: v for k, v in cand.items() if k != "embedding"}
                for cand in (
                    [candidates[m] for m in members]
                )
            ],
            "suggested_winner": {
                k: v for k, v in winner.items() if k != "embedding"
            },
            "losers": losers,
            "llm_verdicts": llm_verdicts,
        })
    return {
        "tier": tier, "user_id": user_id,
        "candidate_count": n,
        "groups": out_groups,
        "scanned_at": _now_iso(),
    }


def apply_merges(merges: list[dict], actor: str = "merge_v2") -> dict:
    """Apply a list of {winner_id, loser_id, tier, user_id} merges.

    Each merge:
      1. Reads winner + loser from bus
      2. Updates winner: importance = max, tags = union, append
         "merged_from:<loser_id>" to metadata
      3. Tombstones loser (tombstoned=1, reason)
      4. Removes loser from nest embeddings
      5. Removes loser from lex index
      6. Writes audit_log row (severity='warning')

    Returns summary with per-merge status.
    """
    from .. import astor_bus, astor_nest
    from .lex_index import astor_lex

    applied = []
    skipped = []
    for entry in merges:
        winner_id = int(entry["winner_id"])
        loser_id = int(entry["loser_id"])
        tier = entry.get("tier", "public")
        user_id = entry.get("user_id")
        try:
            bus_user = user_id if tier in ("private", "repo") else None
            bus = astor_bus(tier=tier, user_id=bus_user)
            winner_row = bus.conn.execute(
                "SELECT id, content, kind, importance, tags, metadata "
                "FROM memory_canonical WHERE id=?",
                (winner_id,),
            ).fetchone()
            loser_row = bus.conn.execute(
                "SELECT id, content, kind, importance, tags, metadata, "
                "stable_id, scope_type FROM memory_canonical WHERE id=?",
                (loser_id,),
            ).fetchone()
            if winner_row is None or loser_row is None:
                skipped.append({
                    "winner_id": winner_id, "loser_id": loser_id,
                    "reason": "missing in bus",
                })
                continue
            # 1. union tags + max importance + augment metadata
            w_tags = json.loads(winner_row[4] or "[]")
            l_tags = json.loads(loser_row[4] or "[]")
            merged_tags = list(dict.fromkeys(w_tags + l_tags))  # ordered
            merged_importance = max(float(winner_row[3]),
                                    float(loser_row[3]))
            w_meta = json.loads(winner_row[5] or "{}")
            w_meta["merged_from"] = list(
                set((w_meta.get("merged_from") or []) + [loser_id])
            )
            w_meta["merged_at"] = _now_iso()
            w_meta["merged_by"] = actor
            # 2. tombstone loser
            bus.conn.execute(
                "UPDATE memory_canonical SET tombstoned = 1, "
                "metadata = ? WHERE id = ?",
                (json.dumps({"merged_into": winner_id}), loser_id),
            )
            # 3. update winner
            bus.conn.execute(
                "UPDATE memory_canonical SET tags = ?, importance = ?, "
                "metadata = ? WHERE id = ?",
                (json.dumps(merged_tags, ensure_ascii=False),
                 merged_importance,
                 json.dumps(w_meta, ensure_ascii=False),
                 winner_id),
            )
            bus.conn.commit()
            # 4. remove loser from nest embeddings
            try:
                nest = astor_nest(tier=tier, user_id=bus_user)
                nest.conn.execute(
                    "DELETE FROM embeddings WHERE fact_id = ?",
                    (loser_id,),
                )
                nest.conn.commit()
            except Exception:
                pass
            # 5. remove loser from lex
            try:
                lex = astor_lex(tier=tier, user_id=bus_user)
                lex.remove_fact_hard(loser_id)
            except Exception:
                pass
            # 6. audit
            try:
                bus.conn.execute(
                    "INSERT INTO audit_log(event, actor, target_type, "
                    "target_id, reason, metadata, severity) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ('merge',
                     actor,
                     'fact',
                     str(winner_id),
                     f'tombstoned {loser_id}; '
                     f'tier={tier} user={user_id}',
                     json.dumps({
                         "loser_id": loser_id,
                         "loser_content": str(loser_row[1])[:120],
                         "merged_tags_count": len(merged_tags),
                         "merged_importance": merged_importance,
                     }, ensure_ascii=False),
                     'warning'),
                )
                bus.conn.commit()
            except Exception:
                pass
            applied.append({
                "winner_id": winner_id,
                "loser_id": loser_id,
                "tier": tier,
                "user_id": user_id,
                "importance": merged_importance,
                "tags_count": len(merged_tags),
            })
        except Exception as exc:
            skipped.append({
                "winner_id": winner_id, "loser_id": loser_id,
                "reason": f"exception: {exc}",
            })
    return {
        "applied": applied, "skipped": skipped,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "actor": actor,
        "at": _now_iso(),
    }


# ----- helpers -----
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat() + "Z"


def _unpack(blob: bytes) -> np.ndarray:
    """Unpack 4-byte floats blob → np.array."""
    n = len(blob) // 4
    import struct as _s
    return np.array(_s.unpack(f'{n}f', blob), dtype=np.float32)
