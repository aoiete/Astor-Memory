"""v1.15.29 (2026-09-28) - Ship M: full peer-config YAML export/import.

This is a richer version of the social_graph export from Phase 2. The
v1.0 bundle only contains friend/blacklist/whitelist/pending peers
with their basic relationship row. The v1.1 bundle also includes:

  - All peer kinds (incl. 'quarantine' from Ship L)
  - topic_index per peer (topic, weight, fact_count, last_seen_at)
  - audit summary (last_seen_iso, last_action, total_actions)
  - rate_limit snapshot per peer (count, cap, oldest_iso)

Use case: complete peer-config backup that can be re-imported to
recreate the entire peer graph + topics + quarantine state, e.g. for
migration to a new ASTOR_DIR or version-controlled peer-config repo.

Anti-hostile notes:
  - Import is idempotent (re-importing is a no-op or merges cleanly)
  - The bundle is a SNAPSHOT, not a live system. Rate-limit counts
    are read-only info; importing doesn't re-create bucket state
    (that's still rebuilt from audit log via /v1/peer/rate-limit/rebuild).
  - Public keys are NOT exported (they're sensitive material; the
    import requires re-issuing pubkey via `am peer add --pubkey`).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


BUNDLE_VERSION = "1.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def export_full_config(
    *,
    astor_dir: str | None = None,
    include_audit: bool = True,
) -> dict:
    """Build a full peer-config bundle (version 1.1).

    Returns the bundle dict ready for YAML serialization. The caller
    is responsible for writing it to disk.
    """
    from .peer_relationships import list_peers, list_topics_for_peer
    from .peer_rate_limit import snapshot as _rl_snapshot
    from .audit_logger import astor_query_peer_audit

    peers = list_peers(astor_dir=astor_dir)
    sections = {}
    for kind in ("friend", "blacklist", "whitelist", "pending", "quarantine"):
        sections[kind] = [p for p in peers if p.get("kind") == kind]

    # Topics: dict of peer_id -> list of {topic, weight, fact_count, last_seen_at}
    topics: dict = {}
    for p in peers:
        try:
            tlist = list_topics_for_peer(p["peer_id"], astor_dir=astor_dir)
        except Exception:
            tlist = []
        if tlist:
            topics[p["peer_id"]] = [
                {
                    "topic": t["topic"],
                    "weight": float(t.get("weight", 0)),
                    "fact_count": int(t.get("fact_count", 0)),
                    "last_seen_at": t.get("last_seen_at"),
                }
                for t in tlist
            ]

    # Rate-limit snapshots
    rate_limits = {}
    for p in peers:
        try:
            s = _rl_snapshot(p["peer_id"])
            rate_limits[p["peer_id"]] = {
                "count": s.get("count", 0),
                "cap": s.get("cap", 0),
                "oldest_iso": s.get("oldest_iso"),
            }
        except Exception:
            rate_limits[p["peer_id"]] = {"count": 0, "cap": 0, "oldest_iso": None}

    # Audit summary
    audit_summary: dict = {}
    if include_audit:
        for p in peers:
            try:
                rows = astor_query_peer_audit(p["peer_id"], limit=200)
            except Exception:
                rows = []
            if rows:
                audit_summary[p["peer_id"]] = {
                    "last_seen_iso": rows[0].get("ts"),
                    "last_action": rows[0].get("action"),
                    "total_actions": len(rows),
                }

    # Strip pubkey (sensitive) from each peer dict
    def _safe_peer(p: dict) -> dict:
        out = dict(p)
        out.pop("public_key", None)
        # Sanitize metadata (drop any tokens, keep just structural fields)
        meta = out.get("metadata") or {}
        if isinstance(meta, dict):
            keep = {
                "allow_search", "blacklist_reason",
                "quarantine_reason", "quarantined_at", "original_trust",
            }
            out["metadata"] = {k: v for k, v in meta.items() if k in keep}
        return out

    bundle = {
        "version": BUNDLE_VERSION,
        "exported_at": _now_iso(),
        "astor_version": "1.15.29+",
        "section": "ship_m_full_peer_config",
        "summary": {
            "total_peers": len(peers),
            "friend_count": len(sections["friend"]),
            "blacklist_count": len(sections["blacklist"]),
            "whitelist_count": len(sections["whitelist"]),
            "pending_count": len(sections["pending"]),
            "quarantine_count": len(sections["quarantine"]),
            "topics_count": len(topics),
        },
        "peers": {k: [_safe_peer(p) for p in v] for k, v in sections.items()},
        "topics": topics,
        "rate_limits": rate_limits,
        "audit_summary": audit_summary,
    }
    return bundle


def import_full_config(
    bundle: dict,
    *,
    strategy: str = "skip",
    astor_dir: str | None = None,
) -> dict:
    """Restore peer config from a v1.1 bundle.

    strategy:
      - 'skip': existing peers are left as-is (default; safest)
      - 'overwrite': trust + alias + metadata + kind are replaced,
        but the row is preserved
      - 'add_only': only NEW peers are added; existing ones untouched

    Returns a summary dict with counts (added, skipped, overwritten,
    topics_restored, quarantined_restored).
    """
    from .peer_relationships import (
        add_peer, update_trust, get_peer, set_topic, update_endpoint,
        quarantine_peer, _error_count_path,
    )

    if not isinstance(bundle, dict):
        return {"error": "bundle must be a dict"}
    if bundle.get("version") not in ("1.0", "1.1"):
        return {"error": f"unsupported bundle version: {bundle.get('version')}"}

    # v1.0 fallback: use the Phase 2 path
    if bundle.get("version") == "1.0":
        return _import_v10(bundle, strategy=strategy, astor_dir=astor_dir)

    added = 0
    skipped = 0
    overwritten = 0
    topics_restored = 0
    quarantined_restored = 0

    peers_by_kind = bundle.get("peers") or {}
    for kind in ("friend", "blacklist", "whitelist", "pending", "quarantine"):
        for p in peers_by_kind.get(kind, []):
            pid = p.get("peer_id")
            if not pid:
                continue
            existing = get_peer(pid, astor_dir=astor_dir)
            if existing and strategy == "skip":
                skipped += 1
                continue
            if existing and strategy == "add_only":
                skipped += 1
                continue
            # Add or overwrite
            add_peer(
                pid,
                kind=p.get("kind", kind),
                trust=int(p.get("trust", 30)),
                alias=p.get("alias"),
                topic_weights=None,
                metadata=p.get("metadata") or {},
                astor_dir=astor_dir,
            )
            if existing:
                overwritten += 1
            else:
                added += 1
            # Restore endpoint
            if p.get("endpoint"):
                try:
                    update_endpoint(pid, p["endpoint"], astor_dir=astor_dir)
                except Exception:
                    pass
            # Restore quarantine-specific state
            if kind == "quarantine":
                try:
                    quarantine_peer(pid, reason=(p.get("metadata") or {}).get(
                        "quarantine_reason", ""), astor_dir=astor_dir)
                    quarantined_restored += 1
                except Exception:
                    pass

    # Topics
    for pid, tlist in (bundle.get("topics") or {}).items():
        for t in tlist:
            try:
                set_topic(
                    t["topic"], pid,
                    weight=float(t.get("weight", 1.0)),
                    astor_dir=astor_dir,
                )
                topics_restored += 1
            except Exception:
                pass

    # Rate-limit snapshots and audit_summary are NOT restored
    # (rate-limits rebuild from audit; audit_summary is read-only)

    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "overwritten": overwritten,
        "topics_restored": topics_restored,
        "quarantined_restored": quarantined_restored,
        "strategy": strategy,
    }


def _import_v10(bundle: dict, *, strategy: str, astor_dir: str | None) -> dict:
    """Import a v1.0 bundle (Phase 2 social_graph).

    v1.15.29 Ship M: pre-existing bug fix. The original code used 
    `existing = (section == "friends" and kind == "friend")` which 
    was always True for friend entries, making friends always skipped.
    Replaced with actual `get_peer` check.
    """
    from .peer_relationships import add_peer, get_peer
    added = 0
    skipped = 0
    overwritten = 0
    sections = ("friends", "blacklist", "whitelist", "pending")
    for section in sections:
        for entry in bundle.get(section, []):
            existing = get_peer(entry["peer_id"], astor_dir=astor_dir)
            if existing and strategy == "skip":
                skipped += 1
                continue
            if existing and strategy == "add_only":
                skipped += 1
                continue
            add_peer(
                entry["peer_id"],
                kind=entry.get("kind", section[:-1] if section != "friends"
                              else "friend"),
                trust=entry.get("trust", 30),
                alias=entry.get("alias"),
                public_key=entry.get("public_key"),
                topic_weights=entry.get("topic_weights"),
                metadata=entry.get("metadata"),
                astor_dir=astor_dir,
            )
            if existing:
                overwritten += 1
            else:
                added += 1
    return {
        "ok": True,
        "added": added, "skipped": skipped, "overwritten": overwritten,
        "strategy": strategy, "version": "1.0",
    }
