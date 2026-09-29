"""knowledge_pages.py — Hindsight-style knowledge page layer (Ship P3.1).

v1.15.39: operator-curated markdown pages, each linking a set of related
facts via parent_fact_ids stored in metadata. Like Hindsight's "knowledge
page" feature — a single canonical entrypoint for a topic that organizes
related facts.

vs. mental_models:
- mental_model = fixed-question answer (one Q → one A)
- knowledge_page = topic-organized multi-fact reference (one slug → many
  facts + markdown body)

Schema:
  kind='knowledge_page' rows in memory_canonical.
  Content: '[KP] slug: <slug>\\ntitle: <title>\\nupdated: <iso>\\n\\n<body markdown>'
  metadata JSON: {"parent_fact_ids": [int, ...], "kind": "knowledge_page"}

CLI:
  am knowledge-page upsert --slug <slug> --title <title> --body <md>
                          [--tier public|...] [--user-id <u>] [--parent-ids 1,2,3]
  am knowledge-page list   [--tier public|...] [--user-id <u>]
  am knowledge-page get    --slug <slug> [--tier public|...]
  am knowledge-page link   --slug <slug> --add-fid 123 --remove-fid 456

Endpoints:
  GET  /v1/knowledge_page?slug=X&tier=Y  → {found, page, linked_facts: [...]}
  GET  /v1/knowledge_page/list?tier=Y
  POST /v1/knowledge_page/upsert  → upsert via JSON body (Hindsight-style)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


_KIND = "knowledge_page"


@dataclass
class KnowledgePage:
    fact_id: int
    slug: str
    title: str
    body: str
    parent_fact_ids: list[int] = field(default_factory=list)
    confidence: float = 0.7
    created_at: str = ""
    updated_at: str = ""
    tier: str = "public"
    user_id: str | None = None


def _format_kp_content(slug: str, title: str, body: str,
                        now_iso: str) -> str:
    """Format the canonical knowledge_page content string.

    Format: '[KP] slug: <slug>\\ntitle: <title>\\nupdated: <iso>\\n\\n<body>'
    Use double-newline separator between header and body for downstream
    parsers that split on '\\n\\n'.
    """
    return f"[KP] slug: {slug}\ntitle: {title}\nupdated: {now_iso}\n\n{body}"


def _parse_kp_content(content: str) -> tuple[str, str, str] | None:
    """Parse a knowledge_page content blob. Returns (slug, title, body) or
    None when the content isn't a knowledge_page blob.
    """
    if not content or not content.startswith("[KP]"):
        return None
    parts = content.split("\n\n", 1)
    header = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    slug = ""
    title = ""
    updated = ""
    for ln in header.split("\n"):
        if ln.startswith("[KP] slug: "):
            slug = ln[len("[KP] slug: "):].strip()
        elif ln.startswith("title: "):
            title = ln[len("title: "):].strip()
        elif ln.startswith("updated: "):
            updated = ln[len("updated: "):].strip()
    if not slug:
        return None
    return slug, title, body, updated  # type: ignore[return-value]


def upsert_knowledge_page(
    bus: Any,
    slug: str,
    title: str,
    body: str,
    tier: str = "public",
    user_id: str | None = None,
    parent_fact_ids: Iterable[int] = (),
    confidence: float = 0.7,
) -> int:
    """Insert or refresh a knowledge_page via the canonical bus pipeline.

    v1.15.39: same pattern as upsert_mental_model — append_event +
    insert_candidate + promote_candidate. Refresh = tombstone prior
    same-slug row, insert new.

    Returns the new canonical fact_id.
    """
    if not slug or not slug.strip():
        raise ValueError("slug required")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = _format_kp_content(slug, title, body, now_iso)
    ns = f"kp:{slug[:128]}"

    event_id = bus.append_event(
        namespace=ns,
        agent_id=user_id or "operator",
        source="knowledge-page-upsert",
        action="upsert",
        content=content,
        metadata={
            "kind": _KIND,
            "slug": slug,
            "title": title,
            "parent_fact_ids": list(parent_fact_ids),
            "memory_class": _KIND,
        },
    )
    keywords = [w for w in (slug + " " + title).lower().split() if len(w) > 3][:10]
    candidate_id = bus.insert_candidate(
        event_id=event_id,
        namespace=ns,
        content=content,
        kind=_KIND,
        confidence=confidence,
        importance=0.6,
        tags=[_KIND, "wiki", f"slug:{slug}"],
        metadata={
            "memory_class": _KIND,
            "slug": slug,
            "title": title,
            "parent_fact_ids": list(parent_fact_ids),
        },
        scene="casual",
        keywords=keywords,
        context=f"knowledge_page:{slug[:80]}",
        entities=None,
    )
    canonical_id = bus.promote_candidate(
        candidate_id=candidate_id,
        promoted_by="knowledge-page-upsert",
        user_id=user_id,
        tier=tier,
        scope_type="long_term",
        verdict="settled",
        origin_session_id=None,
        stable_id=f"kp:{slug[:64]}",
        provenance_kind="operator",
        provenance_agent="knowledge_page",
        evidence_quote=title,
        source_ref="knowledge_page",
        source_hash=f"kp:{slug[:64]}",
    )

    # Tombstone prior same-slug page (refresh semantics).
    prior_rows = bus.conn.execute(
        "SELECT id, metadata FROM memory_canonical "
        "WHERE kind=? AND tombstoned=0 "
        "AND tier=? AND ((? IS NULL AND user_id IS NULL) OR user_id=?) "
        "AND id != ?",
        (_KIND, tier, user_id, user_id or "", canonical_id),
    ).fetchall()
    for prior_id, prior_meta_json in prior_rows:
        try:
            prior_meta = json.loads(prior_meta_json) if prior_meta_json else {}
        except Exception:
            prior_meta = {}
        if prior_meta.get("slug") == slug:
            bus.conn.execute(
                "UPDATE memory_canonical SET tombstoned=1, tombstoned_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), prior_id),
            )

    bus.conn.commit()
    return canonical_id


def _row_to_page(row: sqlite3.Row | tuple) -> KnowledgePage | None:
    """Map a memory_canonical row to a KnowledgePage.

    row order: (id, content, confidence, created_at, promoted_at,
                metadata, tier, user_id)
    """
    if row is None:
        return None
    rid, content, conf, created, promoted, meta_json, tier, uid = row[:8]
    parsed = _parse_kp_content(content or "")
    if parsed is None:
        return None
    slug, title, body, updated = parsed
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except Exception:
        meta = {}
    parents = meta.get("parent_fact_ids") or []
    return KnowledgePage(
        fact_id=int(rid),
        slug=slug,
        title=title,
        body=body,
        parent_fact_ids=[int(p) for p in parents if str(p).isdigit()],
        confidence=float(conf or 0.5),
        created_at=str(created or ""),
        updated_at=str(updated or promoted or ""),
        tier=tier or "public",
        user_id=uid,
    )


def list_knowledge_pages(
    bus: Any,
    tier: str = "public",
    user_id: str | None = None,
    limit: int = 50,
) -> list[KnowledgePage]:
    """Return all non-tombstoned knowledge_page facts for tier/user."""
    cur = bus.conn.execute(
        "SELECT id, content, confidence, created_at, promoted_at, "
        "       metadata, tier, user_id "
        "FROM memory_canonical "
        "WHERE kind=? AND tombstoned=0 "
        "AND tier=? AND (user_id IS ? OR user_id=?) "
        "ORDER BY promoted_at DESC LIMIT ?",
        (_KIND, tier, user_id, user_id or "", int(limit)),
    )
    out: list[KnowledgePage] = []
    for r in cur.fetchall():
        p = _row_to_page(r)
        if p is not None:
            out.append(p)
    return out


def get_knowledge_page(
    bus: Any,
    slug: str,
    tier: str = "public",
    user_id: str | None = None,
) -> KnowledgePage | None:
    """Return the most-recent non-tombstoned knowledge_page for slug."""
    # Escape LIKE wildcards in slug.
    escaped = (
        slug.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    cur = bus.conn.execute(
        "SELECT id, content, confidence, created_at, promoted_at, "
        "       metadata, tier, user_id "
        "FROM memory_canonical "
        "WHERE kind=? AND tombstoned=0 "
        "AND content LIKE ? ESCAPE '\\' "
        "AND tier=? AND (user_id IS ? OR user_id=?) "
        "ORDER BY promoted_at DESC, id DESC LIMIT 1",
        (_KIND, f"[KP] slug: {escaped}%", tier, user_id, user_id or ""),
    )
    row = cur.fetchone()
    return _row_to_page(row)


def get_linked_facts(
    bus: Any,
    page: KnowledgePage,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the linked facts (parent_fact_ids) with content preview."""
    if not page.parent_fact_ids:
        return []
    placeholders = ",".join("?" for _ in page.parent_fact_ids)
    cur = bus.conn.execute(
        f"SELECT id, content, kind, confidence, promoted_at "
        f"FROM memory_canonical "
        f"WHERE id IN ({placeholders}) AND tombstoned=0 "
        f"ORDER BY id DESC LIMIT ?",
        list(page.parent_fact_ids) + [int(limit)],
    )
    return [
        {
            "fact_id": int(r[0]),
            "content": (r[1] or "")[:300],
            "kind": r[2],
            "confidence": float(r[3] or 0.5),
            "promoted_at": r[4],
        }
        for r in cur.fetchall()
    ]


def link_facts(
    bus: Any,
    slug: str,
    add_facts: Iterable[int] = (),
    remove_facts: Iterable[int] = (),
    tier: str = "public",
    user_id: str | None = None,
) -> int:
    """Add or remove fact_ids from a knowledge_page's parent_fact_ids list.

    Implementation: re-runs upsert_knowledge_page with the merged
    parent_fact_ids + the existing title/body. Returns the new fact_id.
    """
    page = get_knowledge_page(bus, slug, tier=tier, user_id=user_id)
    if page is None:
        raise ValueError(f"knowledge_page slug={slug!r} not found")
    merged = set(page.parent_fact_ids)
    for fid in add_facts:
        merged.add(int(fid))
    for fid in remove_facts:
        merged.discard(int(fid))
    return upsert_knowledge_page(
        bus, slug=slug, title=page.title, body=page.body,
        tier=tier, user_id=user_id,
        parent_fact_ids=sorted(merged),
    )


def is_enabled() -> bool:
    """ASTOR_KNOWLEDGE_PAGES=1 enables read endpoints (default on)."""
    import os
    return os.environ.get("ASTOR_KNOWLEDGE_PAGES", "1") == "1"
