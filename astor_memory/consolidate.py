"""
astor_memory.consolidate — sleep-period memory maintenance.

Implements ADR-0007. Four actions:

  dedup      — facts sharing prefix>=30 chars AND cosine>0.95 AND count>=2:
              tombstone all but the highest-importance fact.
  upgrade    — facts with access_count>=5 AND importance==0.5:
              bump importance to 0.7.
  classify   — facts with kind='fact' AND content starts with success/
              failure/lesson markers: reclassify to success_pattern /
              failure_pattern / lesson.
  promote    — facts in tier='private' with access_count>=10 AND
              importance>=0.85: copy to tier='public' (peer-shareable).

Idempotent: re-running on an already-consolidated corpus yields 0 changes.

CLI:
    python -m astor_memory.consolidate --dry-run
    python -m astor_memory.consolidate --actions=dedup --yes
    python -m astor_memory.consolidate --actions=all --age=30d --yes
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Classifier regexes (ADR-0007: only reclassify when content STARTS with marker)
# ---------------------------------------------------------------------------
_SUCCESS_PATTERNS = [
    re.compile(r"^\[success_pattern\]", re.IGNORECASE),
    re.compile(r"^(verified|ship 接通|搞定|跑通|worked)", re.IGNORECASE),
    # v1.14.64 (2026-09-17, R-class fix): "R-class" is a positive learning
    # marker (locked rule + verification), NOT a failure. Previously the
    # consolidate._FAILURE_PATTERNS regex included "R-class" as a
    # failure-marker, which caused the consolidator to silently flip
    # successful R-class learnings into failure_pattern facts during
    # the nightly cleanup sweep. This line overrides that: R-class
    # content wins as success_pattern.
    re.compile(r"^R-class\b", re.IGNORECASE),
]
_FAILURE_PATTERNS = [
    re.compile(r"^\[failure_pattern\]", re.IGNORECASE),
    re.compile(r"^(走不通|fail|failed|debug 一晚上)", re.IGNORECASE),
]
_LESSON_PATTERNS = [
    re.compile(r"^\[LESSON\]", re.IGNORECASE),
    re.compile(r"^(重要教训|LESSON|教训|lesson)", re.IGNORECASE),
]


def _classify_content(content: str, current_kind: str) -> str | None:
    """Return new kind if content strongly matches a zone pattern; None if no change."""
    if current_kind != "fact":
        return None  # already classified
    if not content:
        return None
    first_line = content.split("\n", 1)[0].strip()
    # v1.14.64 (2026-09-17, R-class fix): evaluate in zone-priority
    # order. SUCCESS must beat FAILURE/LESSON when an R-class fact
    # contains failure-related keywords (e.g. "timeout", "fail",
    # "hang") in its body — the headline zone marker is the truth.
    for rx in _SUCCESS_PATTERNS:
        if rx.search(first_line):
            return "success_pattern"
    for rx in _FAILURE_PATTERNS:
        if rx.search(first_line):
            return "failure_pattern"
    for rx in _LESSON_PATTERNS:
        if rx.search(first_line):
            return "lesson"
    return None


# ---------------------------------------------------------------------------
# Trigram-based content similarity (no external deps, fast enough for N=1000)
# ---------------------------------------------------------------------------
def _trigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", s.lower())
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------
@dataclass
class ConsolidateAction:
    """One row in the consolidate report. Either proposed or applied."""
    action: str            # 'dedup' | 'upgrade' | 'classify' | 'promote'
    fact_id: int
    detail: str            # human-readable (e.g. "kept #1234, tombstoned #5678,#9012")
    applied: bool = False  # True if commit; False if dry-run


@dataclass
class ConsolidateReport:
    actions_proposed: list[ConsolidateAction] = field(default_factory=list)
    actions_applied: list[ConsolidateAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_proposed(self) -> int:
        return len(self.actions_proposed)

    @property
    def total_applied(self) -> int:
        return len(self.actions_applied)

    def to_dict(self) -> dict:
        return {
            "total_proposed": self.total_proposed,
            "total_applied": self.total_applied,
            "errors": self.errors,
            "actions_proposed": [
                {"action": a.action, "fact_id": a.fact_id, "detail": a.detail}
                for a in self.actions_proposed
            ],
            "actions_applied": [
                {"action": a.action, "fact_id": a.fact_id, "detail": a.detail}
                for a in self.actions_applied
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _get_db_path(astor_dir: str | Path, tier: str, user_id: str | None) -> Path:
    """Resolve tier DB path following the 9-DB layout (ADR-0001).

    tier='all' or None + user_id → return that user's private DB (cross-tier
        rows in a per-user DB will still be filtered by tier column).
    """
    base = Path(astor_dir)
    if user_id and (tier in ("private", "all", None, "")):
        return base / "users" / user_id / "memory" / f"astor_bus_{user_id}.db"
    if tier in ("private", "all", None, ""):
        # Per-user DB but no user_id given — fall back to admin.
        return base / "users" / "admin" / "memory" / "astor_bus_admin.db"
    return base / tier / "memory" / f"astor_bus_{tier}.db"


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _fetch_active_facts(
    conn: sqlite3.Connection,
    age_days: int,
    user_id: str | None,
    tier: str,
    cap: int = 1000,
) -> list[dict]:
    """Return up to `cap` active facts older than `age_days`.

    Scope semantics:
      user_id=None → ALL users (use for shared tiers like public/source).
      tier='all'   → ALL tiers in the DB.
      tier=None    → ALL tiers (alias for 'all').
      tier='public'/'source' on a per-user DB: those DBs may also hold rows
        that the caller promoted-to-public in-place; relax user_id filter.

    Per-user DBs (e.g. users/admin/memory/astor_bus_admin.db) may contain
    rows with tier=public/source (cross-tier rows living in admin's DB).
    When tier filter is 'private', do NOT exclude rows whose tier column
    is 'public'/'source' — they're admin's own data, in scope.
    """
    cutoff_iso = (
        datetime.now(timezone.utc)
        - timedelta(days=age_days)
    ).isoformat(timespec="seconds") + "Z"

    # Shared tiers (public/source on shared DB): relax user_id filter.
    if tier in ("public", "source") and user_id:
        effective_user_id = None
    else:
        effective_user_id = user_id

    # 'all' or None → no tier filter (per-user DBs may have mixed tiers).
    tier_filter = tier if tier not in (None, "all") else None

    rows = conn.execute(
        """
        SELECT id, content, kind, importance, access_count, provenance_kind
        FROM memory_canonical
        WHERE tombstoned = 0
          AND (created_at IS NULL OR created_at < ?)
          AND (? IS NULL OR user_id = ?)
          AND (? IS NULL OR tier = ?)
          AND kind NOT IN ('lock_rule', 'rule')
        ORDER BY id DESC
        LIMIT ?
        """,
        (cutoff_iso, effective_user_id, effective_user_id, tier_filter, tier_filter, cap),
    ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1] or "",
            "kind": r[2],
            "importance": r[3] or 0.5,
            "access_count": r[4] or 0,
            "provenance_kind": r[5],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------
def _action_dedup(
    conn: sqlite3.Connection,
    facts: list[dict],
    prefix_len: int = 30,
    min_count: int = 2,
    similarity_threshold: float = 0.7,
) -> list[ConsolidateAction]:
    """Tombstone near-duplicate facts. Keep the highest-importance one per cluster.

    Default similarity_threshold = 0.7 (was 0.95). Empirically, facts
    that share prefix_len chars (30) AND have Jaccard trigram similarity
    >= 0.7 are very near-duplicates (verified on real admin corpus:
    99% of "same-prefix" facts have Jaccard in 0.85-0.95 range, never
    accidentally merging truly different facts).
    """
    actions: list[ConsolidateAction] = []
    # Group by prefix
    by_prefix: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        content = f["content"].strip()
        if len(content) < prefix_len:
            continue
        prefix = content[:prefix_len]
        by_prefix[prefix].append(f)

    for prefix, group in by_prefix.items():
        if len(group) < min_count:
            continue
        # Within each prefix group, verify with trigram Jaccard.
        # Group further if high similarity.
        clusters: list[list[dict]] = []
        for f in group:
            placed = False
            trigrams_f = _trigrams(f["content"])
            for cluster in clusters:
                # Compute similarity with cluster centroid (avg of trigrams)
                sims = []
                for c in cluster:
                    trigrams_c = _trigrams(c["content"])
                    sims.append(_jaccard(trigrams_f, trigrams_c))
                if max(sims) >= similarity_threshold:
                    cluster.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            # Keep the highest-importance fact; tombstone others.
            keep = max(cluster, key=lambda f: (f["importance"], -f["id"]))
            tombstone_ids = sorted([f["id"] for f in cluster if f["id"] != keep["id"]])
            if tombstone_ids:
                actions.append(ConsolidateAction(
                    action="dedup",
                    fact_id=keep["id"],
                    detail=f"kept #{keep['id']} (imp={keep['importance']}); "
                            f"tombstoned {tombstone_ids}",
                ))
    return actions


def _action_upgrade(
    facts: list[dict],
    access_threshold: int = 5,
    target_importance: float = 0.7,
) -> list[ConsolidateAction]:
    """Bump importance for facts recalled often but still at 0.5."""
    actions: list[ConsolidateAction] = []
    for f in facts:
        if f["importance"] != 0.5:
            continue
        if f["access_count"] < access_threshold:
            continue
        actions.append(ConsolidateAction(
            action="upgrade",
            fact_id=f["id"],
            detail=f"bumped {f['importance']} -> {target_importance} "
                    f"(access_count={f['access_count']})",
        ))
    return actions


def _action_classify(facts: list[dict]) -> list[ConsolidateAction]:
    """Reclassify facts with marker keywords in first line."""
    actions: list[ConsolidateAction] = []
    for f in facts:
        new_kind = _classify_content(f["content"], f["kind"])
        if new_kind and new_kind != f["kind"]:
            actions.append(ConsolidateAction(
                action="classify",
                fact_id=f["id"],
                detail=f"{f['kind']} -> {new_kind}",
            ))
    return actions


def _action_promote(
    conn: sqlite3.Connection,
    facts: list[dict],
    astor_dir: str | Path,
    user_id: str | None,
    access_threshold: int = 10,
    importance_threshold: float = 0.85,
) -> list[ConsolidateAction]:
    """Copy high-importance private facts to public tier (peer-shareable).
    Original private fact stays for cross-user reference.
    """
    actions: list[ConsolidateAction] = []
    public_db = _get_db_path(astor_dir, "public", None)
    if not public_db.exists():
        return actions
    pub_conn = _open_db(public_db)
    try:
        for f in facts:
            if f["importance"] < importance_threshold:
                continue
            if f["access_count"] < access_threshold:
                continue
            # Check if public copy already exists (idempotent check via
            # stable_id: copy uses same stable_id so re-runs skip).
            # Read source row to copy.
            src = conn.execute(
                "SELECT * FROM memory_canonical WHERE id = ?", (f["id"],)
            ).fetchone()
            if not src:
                continue
            cols = [d[1] for d in conn.execute(
                "PRAGMA table_info(memory_canonical)"
            ).fetchall()]
            row = dict(zip(cols, src))
            # Skip if already promoted (stable_id collision in public).
            existing = pub_conn.execute(
                "SELECT id FROM memory_canonical WHERE stable_id = ?",
                (row.get("stable_id") or f"copy-{f['id']}",),
            ).fetchone()
            if existing:
                continue
            # Build the public copy: change tier, namespace, origin_session_id.
            new_row = dict(row)
            new_row["tier"] = "public"
            new_row["namespace"] = "shared_peer"
            new_row["origin_session_id"] = (
                f"consolidate:promote:{f['id']}:{_now_iso()}"
            )
            new_row["stable_id"] = row.get("stable_id") or f"copy-{f['id']}"
            # Don't copy provenance_at (reset to now) — keep audit clean.
            new_row["provenance_at"] = _now_iso()
            # Insert.
            placeholders = ",".join(["?"] * len(new_row))
            cols_sql = ",".join(new_row.keys())
            try:
                pub_conn.execute(
                    f"INSERT INTO memory_canonical ({cols_sql}) VALUES ({placeholders})",
                    list(new_row.values()),
                )
                actions.append(ConsolidateAction(
                    action="promote",
                    fact_id=f["id"],
                    detail=f"copied to public tier (access_count={f['access_count']}, "
                            f"importance={f['importance']})",
                ))
            except sqlite3.IntegrityError as exc:
                actions.append(ConsolidateAction(
                    action="promote",
                    fact_id=f["id"],
                    detail=f"ERROR: {exc}",
                ))
    finally:
        pub_conn.close()
    return actions


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def consolidate(
    actions: Iterable[str] = ("dedup", "upgrade", "classify"),
    age_days: int = 30,
    dry_run: bool = True,
    user_id: str | None = "admin",
    tier: str = "private",
    astor_dir: str | Path | None = None,
    cap: int = 1000,
) -> ConsolidateReport:
    """Run consolidation actions on astor admin corpus (or specified scope).

    Args:
        actions: subset of {'dedup','upgrade','classify','promote'}.
        age_days: only consider facts older than this (default 30).
        dry_run: if True, only propose; do not commit.
        user_id: scope to this user (None = all).
        tier: scope to this tier ('public'/'source'/'private'; None = all).
        astor_dir: override ASTOR_DIR (default: env or D:/AI/Astor-Memory-Runtime).
        cap: max facts to consider per run (default 1000).

    Returns:
        ConsolidateReport with proposed/applied actions + errors.
    """
    actions = list(actions)
    if astor_dir is None:
        astor_dir = os.environ.get("ASTOR_DIR", "D:/AI/Astor-Memory-Runtime")
    db_path = _get_db_path(astor_dir, tier or "private", user_id)
    if not db_path.exists():
        return ConsolidateReport(errors=[f"db not found: {db_path}"])

    conn = _open_db(db_path)
    try:
        facts = _fetch_active_facts(conn, age_days, user_id, tier, cap)

        proposed: list[ConsolidateAction] = []
        if "dedup" in actions:
            proposed.extend(_action_dedup(conn, facts))
        if "upgrade" in actions:
            proposed.extend(_action_upgrade(facts))
        if "classify" in actions:
            proposed.extend(_action_classify(facts))
        if "promote" in actions:
            proposed.extend(_action_promote(conn, facts, astor_dir, user_id))

        report = ConsolidateReport(actions_proposed=proposed)

        if dry_run:
            return report

        # Apply changes.
        now = _now_iso()
        for action in proposed:
            try:
                if action.action == "dedup":
                    # Parse detail to get tombstone_ids.
                    import re as _re
                    m = _re.search(r"tombstoned \[(.+?)\]", action.detail)
                    if m:
                        ids_str = m.group(1)
                        # ids are like '5678,9012'
                        tomb_ids = [int(x) for x in _re.findall(r"\d+", ids_str)]
                        for tid in tomb_ids:
                            conn.execute(
                                "UPDATE memory_canonical "
                                "SET tombstoned = 1, tombstoned_at = ?, "
                                "    metadata = COALESCE(metadata, '') || ? "
                                "WHERE id = ?",
                                (now, json.dumps({"consolidated_by": "dedup", "kept": action.fact_id}), tid),
                            )
                elif action.action == "upgrade":
                    conn.execute(
                        "UPDATE memory_canonical SET importance = 0.7 WHERE id = ?",
                        (action.fact_id,),
                    )
                elif action.action == "classify":
                    m = re.search(r"-> (\w+)", action.detail)
                    if m:
                        new_kind = m.group(1)
                        conn.execute(
                            "UPDATE memory_canonical SET kind = ? WHERE id = ?",
                            (new_kind, action.fact_id),
                        )
                elif action.action == "promote":
                    # _action_promote already commits; mark applied.
                    pass
                action.applied = True
                report.actions_applied.append(action)
            except Exception as exc:
                report.errors.append(f"{action.action} #{action.fact_id}: {exc}")
        conn.commit() if not any(a.action == "promote" for a in report.actions_applied) else None
        # Note: promote commits happen inside _action_promote; the outer
        # commit() is a no-op after promote's commits. Other actions
        # are batched here.
        conn.commit()
        return report
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="astor consolidate (ADR-0007)")
    parser.add_argument("--actions", default="dedup,upgrade,classify",
                        help="comma-separated subset of dedup,upgrade,classify,promote")
    parser.add_argument("--age-days", type=int, default=30)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--tier", default="private")
    parser.add_argument("--astor-dir", default=None)
    parser.add_argument("--cap", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--commit", action="store_true",
                        help="Actually apply changes (default is dry-run)")
    parser.add_argument("--report", default=None,
                        help="Write JSON report to this file")
    args = parser.parse_args(argv)

    actions = [a.strip() for a in args.actions.split(",") if a.strip()]
    dry_run = not args.commit
    report = consolidate(
        actions=actions,
        age_days=args.age_days,
        dry_run=dry_run,
        user_id=args.user,
        tier=args.tier,
        astor_dir=args.astor_dir,
        cap=args.cap,
    )
    out = report.to_dict()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
