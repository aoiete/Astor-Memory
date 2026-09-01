"""
Success-pattern detector for astor-memory.

Identifies user statements that describe a successful recipe / behavior
loop / fix-that-worked, so the agent can tag them with outcome='success'
and auto-promote recurring ones to tier='public' (for cross-session recall).

v1.13.0 (2026-09-02): initial ship.

Mirror of astor_detect_capture_intent design (forge/extractor.py:174):
heuristic regex detection, no LLM cost.

Usage:
    from astor_memory.forge.pattern_detector import (
        astor_detect_success_pattern,
        astor_score_success_strength,
        astor_promote_recurring_success,
        astor_count_similar_success_facts,
    )

    if astor_detect_success_pattern("这个 cron 配置对了，今天跑通了"):
        strength = astor_score_success_strength(text)
        # ... tag outcome='success'
        # Then check if this pattern recurs:
        count = astor_count_similar_success_facts(fact_content, tier='public')
        if count >= 3:
            astor_promote_recurring_success(fact_id)
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern constants
# ---------------------------------------------------------------------------

# Chinese success phrases (checked case-insensitively).
_SUCCESS_PATTERNS_ZH = [
    r'成功了',
    r'搞定了',
    r'搞定了?这招',
    r'这个方法可以',
    r'这招可以',
    r'记住了',
    r'work\s*了',
    r'work了',
    r'跑通了',
    r'通了',
    r'ok\s*了',
    r'ok了',
    r'成了',
    r'通过了',
    r'就[这这]么干',
    r'这样就好了',
    r'能用了',
    r'可以了',
]

# English success phrases.
_SUCCESS_PATTERNS_EN = [
    r'\bthis works?\b',
    r'\bshipped\b',
    r'\bnailed it\b',
    r'\bfigured out\b',
    r'\bgot it working\b',
    r"\bgot that working\b",
    r"\bthat's the (trick|way)\b",
    r'\bworks for me\b',
    r'\bthat worked\b',
    r'\bthis is the way\b',
    r'\bdone\b',
]

# Combined compiled regex (case-insensitive).
_COMPILED = [
    re.compile(p, re.IGNORECASE)
    for p in _SUCCESS_PATTERNS_ZH + _SUCCESS_PATTERNS_EN
]

# Default threshold for "recurring" — auto-promote when ≥3 similar facts exist.
DEFAULT_RECURRENCE_THRESHOLD = 3

# Keyword tokenization regex for similarity check.
# CJK + ASCII words; matches P0-3 (auto_route_v2) pattern.
_TOKEN_RE = re.compile(r'[A-Za-z0-9_]+|[一-鿿]')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Tokenize text into a set of CJK + ASCII tokens for jaccard similarity."""
    return set(_TOKEN_RE.findall(text))


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def astor_detect_success_pattern(text: str) -> bool:
    """Return True if `text` describes a successful recipe / behavior loop.

    Heuristic regex detection. Mirrors astor_detect_capture_intent style
    (forge/extractor.py:174). No LLM cost. Multi-language (zh + en).

    Args:
        text: raw user statement (any length).

    Returns:
        True if any success-phrase regex matches; False otherwise.

    Examples:
        >>> astor_detect_success_pattern("搞定了")
        True
        >>> astor_detect_success_pattern("this works perfectly")
        True
        >>> astor_detect_success_pattern("今天天气不太好")
        False
    """
    if not text or not text.strip():
        return False
    return any(p.search(text) for p in _COMPILED)


def astor_score_success_strength(text: str) -> float:
    """Score the strength of a success pattern in `text` (0.0 - 1.0).

    Counts distinct success phrases; normalizes by max expected density.
    - 0.0: no match
    - 0.5: single phrase
    - 0.75: two distinct phrases
    - 1.0: ≥3 distinct phrases (very strong signal)

    Args:
        text: raw user statement.

    Returns:
        float in [0.0, 1.0].

    Examples:
        >>> astor_score_success_strength("搞定了")
        0.5
        >>> astor_score_success_strength("搞定了 this works")
        0.75
    """
    if not text or not text.strip():
        return 0.0
    matches = sum(1 for p in _COMPILED if p.search(text))
    if matches == 0:
        return 0.0
    if matches == 1:
        return 0.5
    if matches == 2:
        return 0.75
    return 1.0


def astor_count_similar_success_facts(
    content: str,
    tier: str = 'public',
    user_id: str | None = None,
    db_path: str | Path | None = None,
    *,
    jaccard_threshold: float = 0.3,
) -> int:
    """Count facts in memory_canonical whose content has Jaccard similarity
    >= `jaccard_threshold` to `content`, AND are tagged as outcome='success'.

    Used by astor_promote_recurring_success to decide promotion eligibility.

    Args:
        content: text to compare against stored facts.
        tier: 'public' / 'source' / 'private'. Defaults to 'public'.
        user_id: required when tier='private'.
        db_path: optional explicit bus.db path. Defaults to ASTOR_DIR/bus.db.
        jaccard_threshold: minimum jaccard similarity for "similar".

    Returns:
        int count of similar success-tagged facts (>= 0).

    Note:
        Searches within the same (tier, user_id) scope as the caller is
        authoring in. Cross-scope search is intentionally NOT supported —
        a private_<user> success pattern does not count toward promoting a
        public-tier fact.
    """
    if not content or not content.strip():
        return 0
    target_tokens = _tokenize(content)
    if not target_tokens:
        return 0

    if db_path is None:
        from ..config import get_default_bus_path, _user_bus_path
        if tier == 'private':
            if not user_id:
                raise ValueError(
                    "astor_count_similar_success_facts: tier='private' requires user_id"
                )
            db_path = _user_bus_path(user_id)
        else:
            db_path = get_default_bus_path(tier)
    db_path = Path(db_path)
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT id, content FROM memory_canonical
            WHERE tier = ?
              AND tombstoned = 0
              AND (tags LIKE '%outcome:success%' OR metadata LIKE '%"outcome": "success"%')
            """,
            (tier,),
        ).fetchall()
    finally:
        conn.close()

    count = 0
    for _id, fact_content in rows:
        if not fact_content:
            continue
        sim = _jaccard(target_tokens, _tokenize(fact_content))
        if sim >= jaccard_threshold:
            count += 1
    return count


def astor_promote_recurring_success(
    fact_id: int,
    *,
    tier: str = 'public',
    user_id: str | None = None,
    db_path: str | Path | None = None,
    actor: str = 'system',
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD,
) -> bool:
    """Promote a fact to tier='public' if a similar success-tagged fact
    appears ≥`threshold` times.

    Idempotent: if fact is already tier='public', no-op.
    Audits the promotion via audit_log row.

    Args:
        fact_id: canonical row id to potentially promote.
        tier: target tier for promotion. Defaults to 'public'.
        user_id: required when tier='private' (rare; cross-user isolation).
        db_path: optional explicit bus.db path.
        actor: actor label for the audit row (e.g. 'system', 'first_admin').
        threshold: min recurrence count to trigger promotion.

    Returns:
        True if promoted, False if skipped (already public, below threshold,
        or fact not found).
    """
    if db_path is None:
        from ..config import get_default_bus_path
        db_path = get_default_bus_path('public')
    db_path = Path(db_path)
    if not db_path.exists():
        return False

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id, tier, content FROM memory_canonical WHERE id = ? AND tombstoned = 0",
            (fact_id,),
        ).fetchone()
        if row is None:
            return False
        _id, current_tier, content = row
        if current_tier == tier:
            return False  # already at target tier

        # Count similar success-tagged facts in the CURRENT tier
        # (not the target tier). A private_<user> success pattern should
        # count toward promoting the same fact from private to public.
        # astor_count_similar_success_facts ALREADY counts self (it pulls
        # all rows with matching content + outcome:success tag, including
        # the fact being promoted), so no +1 here.
        count = astor_count_similar_success_facts(
            content=content,
            tier=current_tier,
            user_id=user_id,
            db_path=db_path,
        )
        if count < threshold:
            return False

        # Promote.
        conn.execute(
            "UPDATE memory_canonical SET tier = ? WHERE id = ?",
            (tier, fact_id),
        )
        conn.commit()

        # Audit (non-fatal if it fails; the promotion already succeeded).
        try:
            conn.execute(
                """INSERT INTO audit_log
                       (event, actor, target_type, target_id, old_state, new_state, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    'promote_recurring_success',
                    actor,
                    'fact',
                    str(fact_id),
                    json.dumps({'tier': current_tier}),
                    json.dumps({
                        'tier': tier,
                        'recurrence_count': count,
                        'threshold': threshold,
                    }),
                    f'auto-promote: {count} similar success facts >= threshold {threshold}',
                ),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
    return True


__all__ = [
    'astor_detect_success_pattern',
    'astor_score_success_strength',
    'astor_count_similar_success_facts',
    'astor_promote_recurring_success',
    'DEFAULT_RECURRENCE_THRESHOLD',
]