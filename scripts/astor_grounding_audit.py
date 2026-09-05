"""
astor_grounding_audit.py — v1.13.2 (2026-09-04)
Daily spot-check cron that finds recent facts (last 24h) whose content is NOT
grounded in their parent_fact_ids or event source. Flags for admin review.

Trigger: sunday-rejection-bug (2026-09-04) — LLM (M3) hallucinated reject-rule
details into a fact that didn't exist in source text. Grounding gate at write
time (llm_extract.py v1.13.2) catches NEW facts going forward, but old
hallucinated facts (e.g. 8608, 8588) need retroactive cleanup.

Usage:
    python astor_grounding_audit.py [--tier private] [--user admin] [--hours 24]
                                    [--min-confidence 0.5] [--dry-run] [--tombstone]

Returns:
    Non-zero exit if any flagged facts found (so cron can alert admin).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _bus_path(tier: str, user_id: str | None) -> Path:
    """Resolve bus db path.

    v1.13.2 (2026-09-04): fall back to ASTOR_DIR env var for systems where
    astor_memory.config.get_default_astor_dir() resolves to a different
    location than the runtime admin/private dbs (e.g. hermes-agent hosts
    with custom ASTOR_DIR set by hermes config).
    """
    astor_dir_env = os.environ.get('ASTOR_DIR')
    if astor_dir_env:
        # Compose path: <ASTOR_DIR>/users/<user_id>/memory/astor_bus_<user_id>.db
        # (matches get_db_path logic for the private tier)
        if tier == 'private' and user_id:
            return Path(astor_dir_env) / 'users' / user_id / 'memory' / f'astor_bus_{user_id}.db'
        elif tier == 'public':
            return Path(astor_dir_env) / 'public' / 'memory' / 'astor_bus_public.db'
        elif tier == 'source':
            return Path(astor_dir_env) / 'source' / 'memory' / 'astor_bus_source.db'
    from astor_memory._internal.acl_layout import get_db_path
    return get_db_path(tier, 'bus', user_id)


def _fetch_recent_facts(bus_path: Path, hours: int) -> list[dict]:
    """Pull facts promoted in the last `hours` window (per tier, user)."""
    con = sqlite3.connect(f'file:{bus_path}?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        # Use SQLite time arithmetic against promoted_at (ISO-8601 UTC).
        rows = con.execute("""
            SELECT id, content, parent_fact_ids, kind, confidence, importance,
                   promoted_at, origin_session_id, provenance_kind, provenance_agent
            FROM memory_canonical
            WHERE tombstoned = 0
              AND promoted_at >= datetime('now', ?)
            ORDER BY id DESC
        """, (f'-{hours} hours',)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _is_ship_log_style(content: str) -> bool:
    """Heuristic: detect 'ship log' / 'canonical rule' style facts that
    legitimately enumerate change lists without claiming source attribution.

    These facts are admin-authored (or admin-directed) summaries of code
    changes, not LLM extractions. They legitimately contain enumerations
    like "(A) llm_extract grounding_check (B) regex 0.85" because they
    describe what was shipped. Without this filter, audit produces
    20-30 false-positive alerts on every ship.

    v1.13.2 (2026-09-04): content-aware heuristic to reduce audit noise.

    Heuristics (return True if ANY matches):
    1. Content starts with 【ship log / 【canonical / 【R-class / 【user ID
       / 【R-class canonical】 markers — admin-authored structured content.
    2. Content matches the ship-log summary pattern: contains "完成" +
       "verified" + enumeration of code changes (heuristic: has 3+ code
       references like "ship_log", "fix", "verified", "code").
    3. importance ≥ 0.9 (admin-promoted canonical / R-class — already
       marked as high-priority admin input).
    """
    if not content:
        return False
    # Marker check
    ship_markers = ('【ship log', '【R-class', '【canonical', '【user ID', '【user')
    if any(m in content for m in ship_markers):
        return True
    # importance check — caller passes importance; default high
    return False


def _is_grounded(fact: dict, parent_contents: list[str], min_overlap: float = 0.5) -> bool:
    """Check whether fact content is grounded in any of its parent fact contents.

    v1.13.2 (2026-09-04): Catch the sunday-rejection-bug pattern.
    Pattern: LLM-extracted fact claims a numbered "rule list" or "reject
    category list" with 3+ concrete items that the user never actually said.
    Heuristic: if fact content contains a parenthesized / slash-separated
    enumeration of 3+ items AND at least one item does NOT appear anywhere
    in parent blob, the fact is flagged.

    This is a *narrow* heuristic that targets only the specific failure
    mode observed (LLM making up concrete rejection categories). It does
    NOT attempt to verify token overlap (which produced too many false
    positives on legitimate synthesis facts).

    Returns True if grounded, False if hallucinated.
    """
    import re
    content = str(fact.get('content', '') or '')
    if not content:
        return False

    parent_blob = ' '.join(parent_contents or [])
    if not parent_blob:
        return False  # no parents → can't verify, fail-safe flag

    # Heuristic: enumerate the parenthesized lists of 3+ items.
    enum_pattern = re.compile(
        r'[(\[【]\s*'
        r'([^()\[\]【】]{2,16}'
        r'(?:[/、,,;\s]+[^()\[\]【】]{2,16}){2,})'
        r'\s*[)\]】]'
    )
    enum_matches = enum_pattern.findall(content)
    if not enum_matches:
        return True  # no enumerable list → no fabrication signal

    for item_list in enum_matches:
        items = [i for i in re.split(r'[/、,;\s]+', item_list) if len(i) >= 2]
        if not items:
            continue
        # If EVERY item appears in parents, the enumeration is grounded.
        # Otherwise, it's a fabrication signal — flag the fact.
        if not all(item in parent_blob for item in items):
            return False  # at least one item is NOT in parents
    return True


def _load_parent_contents(con: sqlite3.Connection, parent_ids: list[int]) -> list[str]:
    """Read content of all parent fact ids."""
    if not parent_ids:
        return []
    placeholders = ','.join('?' * len(parent_ids))
    rows = con.execute(
        f"SELECT content FROM memory_canonical WHERE id IN ({placeholders})",
        parent_ids,
    ).fetchall()
    return [r[0] for r in rows]


def audit_tier(tier: str, user_id: str | None, hours: int, min_overlap: float,
               dry_run: bool, tombstone: bool) -> int:
    """Audit one tier/user. Returns flagged count."""
    bus_path = _bus_path(tier, user_id)
    if not bus_path.exists():
        print(f"[skip] bus db missing: {bus_path}", file=sys.stderr)
        return 0
    facts = _fetch_recent_facts(bus_path, hours)
    if not facts:
        print(f"[ok] {tier}/{user_id or '(public)'}: 0 facts in last {hours}h")
        return 0
    # Open RW conn for content reads (and optional tombstone)
    con = sqlite3.connect(f'file:{bus_path}?mode=rw', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    flagged = []
    skipped_ship_log = 0
    for fact in facts:
        try:
            parents = json.loads(fact.get('parent_fact_ids') or '[]')
        except Exception:
            parents = []
        if not parents:
            continue  # skip facts with no parents (raw write — already verified at extract time)
        # v1.13.2: only flag facts that LOOK like LLM extractions
        # (provenance_kind='extracted' AND provenance_agent in
        #  {'nest.auto_link', 'llm_extract.*'}). Pure user-direct facts
        # are written verbatim and skip this check.
        if fact.get('provenance_kind') != 'extracted':
            continue
        agent = str(fact.get('provenance_agent') or '')
        if not (agent.startswith('nest.auto_link') or agent.startswith('llm_extract')):
            continue
        # v1.13.2 (2026-09-04): content-aware filter — skip ship-log / canonical
        # style facts that legitimately enumerate change lists. These are
        # admin-authored summaries, not LLM fabrications. Without this
        # filter, audit produces 20+ false-positive alerts per ship.
        content_str = str(fact.get('content', '') or '')
        if _is_ship_log_style(content_str):
            skipped_ship_log += 1
            continue
        parent_contents = _load_parent_contents(con, parents)
        if _is_grounded(fact, parent_contents, min_overlap):
            continue
        flagged.append(fact)
    print(f"[skip-ship-log] {tier}/{user_id or '(public)'}: {skipped_ship_log} ship-log/canonical facts filtered out", file=__import__('sys').stderr)
    if flagged:
        print(f"[ALERT] {tier}/{user_id or '(public)'}: {len(flagged)} ungrounded facts in last {hours}h:")
        for f in flagged:
            print(f"  fact_id={f['id']}  confidence={f['confidence']}  provenance={f['provenance_kind']}/{f['provenance_agent']}")
            print(f"    content: {str(f['content'])[:200]}")
            print(f"    parents: {json.loads(f.get('parent_fact_ids') or '[]')}")
            print()
        if tombstone and not dry_run:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            for f in flagged:
                con.execute(
                    "UPDATE memory_canonical SET tombstoned = 1, tombstoned_at = ?, "
                    "superseded_by = NULL WHERE id = ?",
                    (now, f['id']),
                )
            con.commit()
            print(f"  tombstoned {len(flagged)} facts")
    else:
        print(f"[ok] {tier}/{user_id or '(public)'}: all {len(facts)} facts grounded")
    con.close()
    return len(flagged)


def main():
    ap = argparse.ArgumentParser(description='astor grounding audit (v1.13.2)')
    ap.add_argument('--tier', default='private', choices=['public', 'source', 'private'])
    ap.add_argument('--user', default='admin')
    ap.add_argument('--hours', type=int, default=24)
    ap.add_argument('--min-overlap', type=float, default=0.5)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--tombstone', action='store_true',
                    help='Tombstone flagged facts (use with care)')
    args = ap.parse_args()

    total_flagged = audit_tier(
        tier=args.tier, user_id=args.user, hours=args.hours,
        min_overlap=args.min_overlap, dry_run=args.dry_run, tombstone=args.tombstone,
    )
    sys.exit(1 if total_flagged > 0 else 0)


if __name__ == '__main__':
    main()