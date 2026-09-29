"""mental_models.py — Hindsight-style fixed-question answer sheets (Ship P1.1).

Mental model: a kind=mental_model fact that answers a fixed question
(e.g. "What is this user's core workflow?", "What timezone is admin
in?", "What is the RAG retrieval policy?"). Hindsight's design:
- Stored separately from regular facts (kind=mental_model marker)
- Updated in the background when new evidence lands
- Read = direct DB fetch, NOT a vector/BM25 search (instant, no LLM)

Why this matters for astor:
- Recurring queries ("what timezone is admin", "what's our ship policy
  on X") currently go through expensive hybrid search + MMR + meta-recall.
  Mental model reads skip all of that.
- Self-evolving: when new facts arrive that relate to a mental model's
  topic, the model can be auto-rewritten (Hindsight: "new evidence
  updates, doesn't overwrite").

Architecture (minimal):
- Storage: reuses memory_canonical with kind='mental_model' marker
  + a memory_question column? No — for v1 we tag mental_model facts
  with their question in content prefix:
    "[MM] question: <question>\nanswer: <answer>"
- Read: GET /v1/mental_model?question=... → exact question lookup
  (no vector search)
- Refresh: am mental-model rebuild --question=... → LLM synthesize
  fresh answer from recent canonical facts, write_kind=mental_model,
  supersede old (set tombstoned=1 on prior)
- Auto-link: when a new fact is written that mentions the topic
  keywords, mark the related mental_model as 'stale' for background
  rebuild (defer to P2 cron)

For v1 we ship:
- Storage convention (question + answer in content, kind=mental_model)
- Read endpoint /v1/mental_model?question=... (exact-match)
- CLI am mental-model rebuild --question=...
- CLI am mental-model list

Deferred (P2 / P3):
- Background auto-rewrite cron
- Confidence-weighted update (new evidence weights)
- Personality params
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

# Mental model content format (Hindsight-style):
#   [MM] question: <question>\nanswer: <answer>
# Tags are stripped from recall — they're metadata, not user data.
_MM_TAG = "[MM] "
_QUESTION_PREFIX = "question: "
_ANSWER_PREFIX = "answer: "


@dataclass(frozen=True)
class MentalModel:
    """One mental model fact: a fixed question + cached answer."""
    fact_id: int
    question: str          # the fixed question (canonical form)
    answer: str            # current answer (may be stale, awaiting refresh)
    confidence: float      # 0.0-1.0 (Hindsight analog)
    created_at: str        # ISO
    updated_at: str        # ISO (last rewrite timestamp)
    source_count: int      # how many canonical facts back this answer


def _parse_mm_content(content: str) -> tuple[str, str] | None:
    """Parse [MM] question: ... \\n answer: ... into (question, answer).
    Returns None if content doesn't follow the mental-model format."""
    if not content or not content.startswith(_MM_TAG):
        return None
    body = content[len(_MM_TAG):]
    # Find first newline to split question/answer
    parts = body.split("\n", 1)
    if len(parts) < 2:
        return None
    qline = parts[0].strip()
    if not qline.startswith(_QUESTION_PREFIX):
        return None
    question = qline[len(_QUESTION_PREFIX):].strip()
    answer_line = parts[1].strip()
    if answer_line.startswith(_ANSWER_PREFIX):
        answer = answer_line[len(_ANSWER_PREFIX):].strip()
    else:
        answer = answer_line
    if not question or not answer:
        return None
    return question, answer


def _format_mm_content(question: str, answer: str) -> str:
    """Format (question, answer) into the [MM] convention."""
    return f"{_MM_TAG}{_QUESTION_PREFIX}{question}\n{_ANSWER_PREFIX}{answer}"


def list_mental_models(bus: Any, tier: str = "public",
                       user_id: str | None = None) -> list[MentalModel]:
    """Return all non-tombstoned mental_model facts for tier/user."""
    # v1.15.36: schema columns are created_at / promoted_at / last_confirmed_at
    # (no dedicated updated_at on memory_canonical). Use promoted_at as
    # the "updated_at" proxy — sufficient for ordering.
    sql = """
        SELECT id, content, confidence, created_at, promoted_at
        FROM memory_canonical
        WHERE kind = 'mental_model' AND tombstoned = 0
          AND tier = ?
          AND (user_id = ? OR (? IS NULL AND user_id IS NULL))
        ORDER BY promoted_at DESC
    """
    rows = bus.conn.execute(sql, (tier, user_id, user_id)).fetchall()
    out: list[MentalModel] = []
    for r in rows:
        parsed = _parse_mm_content(r[1] or "")
        if parsed is None:
            continue
        q, a = parsed
        out.append(MentalModel(
            fact_id=int(r[0]),
            question=q,
            answer=a,
            confidence=float(r[2] or 0.5),
            created_at=str(r[3] or ""),
            updated_at=str(r[4] or ""),
            source_count=0,
        ))
    return out


def get_mental_model(bus: Any, question: str,
                     tier: str = "public",
                     user_id: str | None = None) -> MentalModel | None:
    """Return the most-recently-updated mental_model for an exact question.

    v1.15.36: LIKE pattern escapes SQL wildcards (% _) so user input
    like "?" doesn't accidentally match other rows.
    """
    # Escape LIKE wildcards in question before building pattern.
    escaped_q = (
        question.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    sql = """
        SELECT id, content, confidence, created_at, promoted_at
        FROM memory_canonical
        WHERE kind = 'mental_model' AND tombstoned = 0
          AND tier = ?
          AND (user_id = ? OR (? IS NULL AND user_id IS NULL))
          AND content LIKE ? ESCAPE '\\'
        ORDER BY promoted_at DESC
        LIMIT 1
    """
    pattern = f"[MM] question: {escaped_q}%"
    rows = bus.conn.execute(sql, (tier, user_id, user_id, pattern)).fetchall()
    if not rows:
        return None
    r = rows[0]
    parsed = _parse_mm_content(r[1] or "")
    if parsed is None:
        return None
    q, a = parsed
    return MentalModel(
        fact_id=int(r[0]),
        question=q,
        answer=a,
        confidence=float(r[2] or 0.5),
        created_at=str(r[3] or ""),
        updated_at=str(r[4] or ""),
        source_count=0,
    )


def upsert_mental_model(bus: Any, question: str, answer: str,
                         tier: str = "public", user_id: str | None = None,
                         confidence: float = 0.7,
                         source_facts: Iterable[int] = ()) -> int:
    """Insert or refresh a mental_model via the canonical write pipeline.

    v1.15.36: use bus.append_event + bus.insert_candidate +
    bus.promote_candidate — same chain as /v1/write — instead of a hand-
    rolled INSERT. This guarantees every NOT NULL memory_canonical column
    is populated correctly (the 46-column schema evolved over time; hand-
    rolling drifts).

    Returns the new canonical fact_id.
    """
    content = _format_mm_content(question, answer)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ns = f"mm:{question[:128]}"

    # 1) append_event → event_id (required by memory_candidates).
    #    Use namespace = mental_model marker; content carries the MM body.
    event_id = bus.append_event(
        namespace=ns,
        agent_id=user_id or "operator",
        source="mental-model-upsert",
        action="upsert",
        content=content,
        metadata={
            "kind": "mental_model",
            "question": question,
            "answer": answer,
            "confidence": confidence,
            "source_facts": list(source_facts),
            "memory_class": "mental_model",
        },
    )

    # 2) insert_candidate → candidate_id.
    #    tag with keywords/entities so recall can surface MM rows when
    #    operator asks the literal question.
    keywords = [w for w in question.lower().split() if len(w) > 3][:8]
    candidate_id = bus.insert_candidate(
        event_id=event_id,
        namespace=ns,
        content=content,
        kind="mental_model",
        confidence=confidence,
        importance=0.7,
        tags=["mental_model"],
        metadata={
            "memory_class": "mental_model",
            "question": question,
            "answer": answer,
        },
        scene="casual",
        keywords=keywords,
        context=f"mental_model:{question[:80]}",
        entities=None,
    )

    # 3) promote_candidate → canonical_id.
    canonical_id = bus.promote_candidate(
        candidate_id=candidate_id,
        promoted_by="mental-model-upsert",
        user_id=user_id,
        tier=tier,
        scope_type="long_term",
        verdict="settled",
        origin_session_id=None,
        stable_id=f"mm:{question[:64]}",
        provenance_kind="operator",
        provenance_agent="mental_model",
        evidence_quote=question,
        source_ref="mental_model",
        source_hash=f"mm:{question[:64]}",
    )

    # 4) Tombstone any prior non-tombstoned mental_model for the same
    #    question (same tier + user_id). This is the "refresh" semantics:
    #    operator runs `am mental-model rebuild --question X` and the old
    #    X answer gets tombstoned so recall doesn't surface stale info.
    #    Match by question stored in metadata JSON.
    prior_rows = bus.conn.execute(
        "SELECT id, metadata FROM memory_canonical "
        "WHERE kind='mental_model' AND tombstoned=0 "
        "AND tier=? AND ((? IS NULL AND user_id IS NULL) OR user_id=?) "
        "AND id != ?",
        (tier, user_id, user_id or "", canonical_id),
    ).fetchall()
    for prior_id, prior_meta_json in prior_rows:
        try:
            prior_meta = json.loads(prior_meta_json) if prior_meta_json else {}
        except Exception:
            prior_meta = {}
        if prior_meta.get("question") == question:
            bus.conn.execute(
                "UPDATE memory_canonical SET tombstoned=1, "
                "tombstoned_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), prior_id),
            )

    # v1.15.36: commit the tombstone UPDATE. The append_event +
    # insert_candidate + promote_candidate chain runs in its own internal
    # transactions, but the tombstone UPDATE here does not auto-commit.
    bus.conn.commit()
    return canonical_id

def _ensure_sources_table(conn: sqlite3.Connection) -> None:
    """Create mental_model_sources table if missing. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mental_model_sources ("
        "mental_model_id INTEGER NOT NULL,"
        " source_fact_id INTEGER NOT NULL,"
        " created_at TEXT NOT NULL,"
        "PRIMARY KEY (mental_model_id, source_fact_id))"
    )
    conn.commit()


def is_enabled() -> bool:
    """ASTOR_MENTAL_MODELS=1 enables read endpoint (default on).

    v1.15.36: default flipped ON since the module ships v1.15.36.
    Set ASTOR_MENTAL_MODELS=0 to disable (legacy opt-out path).
    """
    return os.environ.get("ASTOR_MENTAL_MODELS", "1") == "1"