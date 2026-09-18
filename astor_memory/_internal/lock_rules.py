"""Phase E1 (2026-09-17): LOCK rule schema + evaluation helpers.

A LOCK rule is a fact that classifies incoming content to a tier. Astor
consults LOCK rules whenever the caller is NOT in the trusted_agent list
(via ``/v1/classify`` and the write pipeline). The goal is to let the
server decide — without trusting the caller's own tier argument — whether
a piece of content is private, public, or "rule-ship"-able (a curated
rule that can be promoted to public storage).

LOCK rule fact shape (stored in memory_canonical like any other fact):
    kind:        "lock_rule"
    tags:        ["LOCK"]
    topic:       unique rule identifier (e.g. "personal-finance-private")
    keywords:    list of trigger patterns (regex strings, case-insensitive)
    content:     human-readable description of the rule
    context:     JSON string: {
        "target_tier": "public" | "private" | "source" | "rule_ship",
        "scope":        "user" | "global",
        "priority":     int 1-10  (higher = matches first),
        "action":       "route" | "block" | "tag",
        "tags_extra":   ["..."]   (tags to add to the matched fact)
    }

The evaluation function matches the input text against each rule's
keywords (regex OR literal). The highest-priority match wins; ties break
on rule_name alphabetical order. If no rule matches, the caller is told
``no_match`` (caller then decides via LOCK-rule audit).

This is a deliberately small surface — no LLM, no async, no caching. The
intent is that rules are written by humans (admin) and evaluated
deterministically by the server. If a future caller wants LLM-based
semantic matching, they add a separate ``astor_llm_classify`` tool; the
LOCK-rule path stays auditable.
"""
from __future__ import annotations

import datetime as _dt_lr

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any


VALID_TARGET_TIERS = ("public", "private", "source", "rule_ship")
VALID_SCOPES = ("user", "global")
VALID_ACTIONS = ("route", "block", "tag")


def _now_iso() -> str:
    """UTC ISO-8601 with 'Z' suffix. v1.14.63 helper."""
    return (
        _dt_lr.datetime.now(_dt_lr.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
_LOCK_RULE_KIND = "lock_rule"
_LOCK_RULE_TAG = "LOCK"


@dataclass(frozen=True)
class LockRule:
    """Parsed LOCK rule ready for evaluation."""

    rule_id: int                  # fact_id in memory_canonical
    rule_name: str                # topic
    target_tier: str              # VALID_TARGET_TIERS
    scope: str                    # VALID_SCOPES
    action: str                   # VALID_ACTIONS
    priority: int                 # higher = matches first
    keywords: tuple[str, ...]     # regex patterns (compiled lazily)
    description: str = ""
    tags_extra: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, text: str) -> bool:
        """True if any keyword regex matches ``text`` (case-insensitive)."""
        if not text or not self.keywords:
            return False
        for pat in self._compiled_patterns():
            if pat.search(text):
                return True
        return False

    def _compiled_patterns(self) -> list[re.Pattern[str]]:
        # Lazy compile so the constructor stays cheap.
        if not hasattr(self, "_compiled_cache"):
            object.__setattr__(
                self,
                "_compiled_cache",
                [re.compile(pat, re.IGNORECASE) for pat in self.keywords],
            )
        return self._compiled_cache  # type: ignore[attr-defined]


def parse_lock_rule(fact: dict[str, Any]) -> LockRule | None:
    """Extract a ``LockRule`` from a fact dict. Returns ``None`` if the
    fact is not a LOCK rule or is malformed.
    """
    if not isinstance(fact, dict):
        return None
    tags = fact.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    if _LOCK_RULE_TAG not in tags:
        return None

    kind = fact.get("kind", "")
    if kind and kind != _LOCK_RULE_KIND:
        # Be lenient: a fact with the LOCK tag but a different kind is
        # still treated as a candidate rule. Strict callers can use a
        # dedicated ``kind="lock_rule"`` filter at the query layer.
        pass

    rule_name = (fact.get("topic") or "").strip() or f"rule-{fact.get('fact_id', '?')}"
    keywords = fact.get("keywords") or []
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except Exception:
            keywords = []
    keywords = tuple(str(k) for k in keywords if k)

    ctx_raw = fact.get("context") or "{}"
    if isinstance(ctx_raw, dict):
        ctx = ctx_raw
    else:
        try:
            ctx = json.loads(ctx_raw)
        except Exception:
            ctx = {}
    target_tier = str(ctx.get("target_tier") or "private")
    if target_tier not in VALID_TARGET_TIERS:
        target_tier = "private"
    scope = str(ctx.get("scope") or "global")
    if scope not in VALID_SCOPES:
        scope = "global"
    action = str(ctx.get("action") or "route")
    if action not in VALID_ACTIONS:
        action = "route"
    try:
        priority = int(ctx.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    tags_extra = ctx.get("tags_extra") or []
    if isinstance(tags_extra, str):
        tags_extra = [tags_extra]
    tags_extra = tuple(str(t) for t in tags_extra if t)

    return LockRule(
        rule_id=int(fact.get("fact_id") or 0),
        rule_name=rule_name,
        target_tier=target_tier,
        scope=scope,
        action=action,
        priority=priority,
        keywords=keywords,
        description=str(fact.get("content") or ""),
        tags_extra=tags_extra,
    )


def fetch_lock_rules(
    con: sqlite3.Connection,
    *,
    user_id: str | None = None,
    scope: str | None = None,
) -> list[LockRule]:
    """Fetch LOCK rules from a bus DB connection.

    Args:
        con: open sqlite3 connection (bus DB).
        user_id: if set, returns rules scoped to that user + global rules.
        scope: optional filter ("user" or "global").

    Notes:
        The caller is responsible for opening the right tier DB. By
        convention, LOCK rules live in the ``public`` tier (admin-only
        write path) so they can be consulted by any caller without
        requiring private-tier access. If you also want per-user
        LOCK rules, pass the per-user DB and filter by ``scope``.

        The actual ``memory_canonical`` schema varies across DB versions
        (some lack ``topic`` / ``namespace`` / ``metadata`` columns).
        We probe the column list first and only SELECT what exists so the
        helper works on every supported DB version.
    """
    existing = {row[1] for row in con.execute("PRAGMA table_info(memory_canonical)").fetchall()}
    select_cols = ["id", "kind", "content", "tags"]
    for c in ("topic", "keywords", "context", "metadata", "namespace", "user_id", "tombstoned"):
        if c in existing:
            select_cols.append(c)
    # Wrap the tag-match OR in parens so the tombstone filter applies to
    # both branches (SQL precedence would otherwise let `tags LIKE
    # '%"LOCK"%'` short-circuit the AND).
    rows = con.execute(
        f"SELECT {', '.join(select_cols)} FROM memory_canonical "
        "WHERE (tags LIKE '%\"LOCK\"%' OR tags LIKE 'LOCK%') "
        "  AND (tombstoned = 0 OR tombstoned IS NULL) "
        "ORDER BY id"
    ).fetchall()

    out: list[LockRule] = []
    for r in rows:
        fact: dict[str, Any] = {"fact_id": r["id"]}
        for c in select_cols:
            if c == "id":
                continue
            fact[c] = r[c]
        # Promote metadata.__keywords__ / __context__ / __topic__ into
        # top-level fields so parse_lock_rule() can find them.
        meta_raw = fact.get("metadata")
        meta: dict[str, Any] = {}
        if meta_raw:
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except Exception:
                meta = {}
        if not fact.get("keywords"):
            fact["keywords"] = meta.get("__keywords__") or []
        if not fact.get("context"):
            fact["context"] = meta.get("__context__") or "{}"
        if not fact.get("topic"):
            fact["topic"] = meta.get("__topic__") or ""

        rule = parse_lock_rule(fact)
        if rule is None:
            continue
        if scope and rule.scope != scope:
            continue
        if user_id and rule.scope == "user":
            fact_user = fact.get("user_id") or _fact_user_id(fact)
            if fact_user and fact_user != user_id:
                continue
        out.append(rule)
    return out


def _fact_user_id(fact: dict[str, Any]) -> str | None:
    """Extract ``user_id`` from a fact's namespace if it follows the
    convention ``<tier>/<user>[/<rest>]`` or ``<user>``.
    """
    ns = fact.get("namespace") or ""
    parts = ns.split("/")
    if not parts:
        return None
    if parts[0] in ("public", "source", "private"):
        if len(parts) >= 2:
            return parts[1]
        return None
    return parts[0] or None


def evaluate_text(
    text: str,
    rules: list[LockRule],
) -> dict[str, Any] | None:
    """Match ``text`` against ``rules`` and return the winning rule's
    decision as a JSON-friendly dict, or ``None`` if no rule matches.

    The winner is the highest-priority rule whose ``matches()`` returns
    True. Ties break on ``rule_name`` ascending (deterministic).
    """
    matches: list[tuple[LockRule, str]] = []
    for rule in rules:
        if rule.matches(text):
            matches.append((rule, rule.rule_name))
    if not matches:
        return None
    matches.sort(key=lambda x: (-x[0].priority, x[1]))
    rule = matches[0][0]
    return {
        "rule_id": rule.rule_id,
        "rule_name": rule.rule_name,
        "target_tier": rule.target_tier,
        "action": rule.action,
        "priority": rule.priority,
        "tags_extra": list(rule.tags_extra),
        "description": rule.description,
    }


__all__ = [
    "LockRule",
    "VALID_TARGET_TIERS",
    "VALID_SCOPES",
    "VALID_ACTIONS",
    "parse_lock_rule",
    "fetch_lock_rules",
    "evaluate_text",
    "seed_lock_rule",
]


def seed_lock_rule(
    con: sqlite3.Connection,
    *,
    topic: str,
    keywords: list[str],
    target_tier: str = "private",
    scope: str = "global",
    priority: int = 5,
    action: str = "route",
    description: str = "",
    tags_extra: list[str] | None = None,
    user_id: str = "admin",
    namespace: str = "admin",
    actor: str = "admin:admin",
) -> int:
    """Insert a LOCK rule fact. Returns the new ``id``.

    Schema (memory_canonical):
      kind:        "lock_rule"
      tags:        ["LOCK"] + tags_extra
      content:     description
      topic:       topic (rule name)
      keywords:    keywords (JSON array of regex/literal patterns)
      context:     {target_tier, scope, priority, action, tags_extra}
      namespace:   <user_id> or "admin"
      user_id:     user_id

    Writes one audit row (action=admin_op) so the rule insertion is
    attributable.
    """
    if target_tier not in VALID_TARGET_TIERS:
        raise ValueError(f"target_tier must be one of {VALID_TARGET_TIERS}")
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}")
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")

    ctx = {
        "target_tier": target_tier,
        "scope": scope,
        "priority": priority,
        "action": action,
        "tags_extra": list(tags_extra or []),
    }
    tags_list = ["LOCK"] + list(tags_extra or [])

    # Detect which optional columns the DB has so we don't crash on old
    # schemas.
    existing = {row[1] for row in con.execute("PRAGMA table_info(memory_canonical)").fetchall()}
    cols = ["kind", "content", "tags", "namespace", "user_id"]
    vals = ["?", "?", "?", "?", "?"]
    bind = ["lock_rule", description or topic, json.dumps(tags_list),
            namespace, user_id]
    if "topic" in existing:
        cols.append("topic")
        vals.append("?")
        bind.append(topic)
    if "keywords" in existing:
        cols.append("keywords")
        vals.append("?")
        bind.append(json.dumps(keywords))
    if "context" in existing:
        cols.append("context")
        vals.append("?")
        bind.append(json.dumps(ctx))
    if "metadata" in existing:
        # Also stash structured fields under metadata for legacy readers.
        cols.append("metadata")
        vals.append("?")
        bind.append(json.dumps({
            "__keywords__": keywords,
            "__context__": ctx,
            "__topic__": topic,
        }))
    # v1.14.63 (2026-09-17, R-class fix): explicit tombstoned=0. The
    # column default is 0, but decay sweep + dedup + reflection can race
    # on freshly seeded rules. Pinning the value here makes the contract
    # explicit so a future schema-default flip doesn't silently tombstone
    # LOCK rules. Also force last_confirmed_at = NOW so the 90d decay
    # gate doesn't trip on rules that haven't been /v1/read.
    if "tombstoned" in existing:
        cols.append("tombstoned")
        vals.append("?")
        bind.append(0)
    if "last_confirmed_at" in existing:
        cols.append("last_confirmed_at")
        vals.append("?")
        bind.append(_now_iso())

    # candidate_id has a UNIQUE constraint, event_id is NOT NULL. Synthesize
    # both from current max so LOCK rule inserts never collide with normal
    # write-path rows.
    try:
        max_cand = con.execute(
            "SELECT COALESCE(MAX(candidate_id), 0) FROM memory_canonical"
        ).fetchone()[0]
        max_event = con.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM memory_canonical"
        ).fetchone()[0]
        cols.append("candidate_id")
        vals.append("?")
        bind.append(int(max_cand) + 1)
        cols.append("event_id")
        vals.append("?")
        bind.append(int(max_event) + 1)
    except Exception:
        # Older DBs without those columns — already in 'cols'.
        pass

    sql = (f"INSERT INTO memory_canonical ({', '.join(cols)}) "
           f"VALUES ({', '.join(vals)})")
    cur = con.execute(sql, bind)
    rule_id = int(cur.lastrowid or 0)
    con.commit()

    # Audit the rule insertion so admins can review who added what.
    try:
        from .audit_logger import astor_audit
        astor_audit(
            actor=actor,
            tier="source",
            action="admin_op",
            target=f"lock_rule/{rule_id}",
            reason=f"seed LOCK rule {topic!r}",
            metadata={
                "target_tier": target_tier,
                "scope": scope,
                "priority": priority,
                "keywords_count": len(keywords),
            },
        )
    except Exception:
        # Audit is best-effort.
        pass

    return rule_id
