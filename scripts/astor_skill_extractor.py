"""astor_skill_extractor.py — Memmy-style skill extraction pipeline.

Scans astor bus facts, classifies them into discrete kind buckets
(rule / decision / lesson / user_preference / failure_pattern), and
suggests which should be promoted to skill-tier.

Stage 1 (this script): deterministic rule-based classifier + cluster finder.
Stage 2 (future): LLM-assisted classification for ambiguous cases.

Usage:
  python astor_skill_extractor.py --tier private --user admin --min-imp 0.7
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


# --- Pattern rules for kind classification ---
# (pattern, target_kind, confidence, rationale)
# Order matters: more specific patterns first.

RULES: list[tuple[re.Pattern, str, float, str]] = [
    # Negative patterns: MUST be FIRST to catch rule references before positive rules
    # These override decision/user_preference positives for "用户偏好是 X" / "用户偏好: ..." patterns
    (re.compile(r"用户(偏好|决定|授权|锁定|选择)是\s+\S"), "rule", 0.95, "rule referencing user pref (用户偏好是 X)"),
    (re.compile(r"^用户(偏好|决定|授权|锁定|选择)\s*[:：]\s*\S"), "rule", 0.95, "rule pref declaration at start"),
    (re.compile(r"用户(偏好|决定|授权|锁定|选择)\s*[:：]\s*\S"), "rule", 0.95, "rule pref declaration anywhere"),
    # R-class / locked: R-class is rule
    (re.compile(r"^[\s【]*R-class\s*\(", re.IGNORECASE), "rule", 0.9, "starts with 'R-class (' (allow 【 prefix)"),
    (re.compile(r"^[\s【]*R-class\s+(canonical|locked)", re.IGNORECASE), "rule", 0.9, "starts with 'R-class canonical/locked' (allow 【 prefix)"),
    (re.compile(r"\(locked\s+\d{4}-\d{2}-\d{2}", re.IGNORECASE), "rule", 0.85, "contains '(locked YYYY-MM-DD'"),
    # Decision: user explicit choice (require "用户"+decision verb combo)
    (re.compile(r"^(用户决策|用户决定|用户授权|用户偏好|用户选择|用户锁定|用户回复)"), "decision", 0.9, "starts with '用户决策/决定/授权/偏好/选择'"),
    (re.compile(r"用户(决定|决策|授权|锁定|选择|chose)"), "decision", 0.7, "user + decision verb combo"),
    # user_preference: explicit preference (NOT inside quote OR part of "用户偏好是 X" reference pattern)
    # Two-stage filter:
    # 1. Reject if surrounded by ASCII quote or typographic quote (preceded/followed)
    # 2. Reject if follows pattern "用户偏好是 <word>" — that's a rule referencing preference, not expressing one
    (re.compile(r"(?<![\"'`“‘])用户(偏好|喜欢|倾向|允许)(?![\"'`”’])"), "user_preference", 0.85, "user pref statement (not quoted)"),
    (re.compile(r"(?<![\"'`“‘])用户(从不|总是|经常|不要|应该)(?![\"'`”’])"), "user_preference", 0.7, "user habit statement (not quoted)"),
    # failure_pattern: fact primarily about a failure (require at start of content, not just reference)
    (re.compile(r"^(\s*)(\b(失败|错了|误判|bug|err|error)\b.*(silent|drop|false positive))", re.IGNORECASE), "failure_pattern", 0.85, "starts with failure + diagnosis"),
    (re.compile(r"^(\s*)失败模式"), "failure_pattern", 0.95, "starts with 失败模式"),
    (re.compile(r"^(\s*)(误判|tombstone|错判)"), "failure_pattern", 0.8, "starts with 误判/tombstone/错判"),
    # lesson: learned something
    (re.compile(r"^(\[LESSON\]|\[教训\])"), "lesson", 0.95, "starts with [LESSON]/[教训]"),
    (re.compile(r"(教训|经验教训|key takeaway|takeaway\s*:)"), "lesson", 0.7, "contains 教训/经验教训/takeaway"),
    (re.compile(r"\[ship log\s+v\d+(\.\d+)+\s+final"), "lesson", 0.7, "starts with [ship log vX.Y final"),
    (re.compile(r"(session.*终局|终局 audit|verified\s*$)", re.IGNORECASE), "lesson", 0.7, "session audit/终局"),
    (re.compile(r"\bR-class\s+(canonical|locked)", re.IGNORECASE), "lesson", 0.65, "contains R-class canonical/locked"),
]


def classify_one(content: str) -> tuple[str, float, str]:
    """Classify a single fact. Returns (kind, confidence, rationale)."""
    for pat, kind, conf, rationale in RULES:
        if pat.search(content):
            return kind, conf, rationale
    return "fact", 0.5, "no rule matched (default)"


def find_skill_clusters(
    facts: Iterable[dict],
    min_count: int = 3,
    min_avg_conf: float = 0.7,
) -> list[dict]:
    """Find clusters of facts that should be promoted to skill-tier.

    A "cluster" is a group of facts with similar content (cosine over
    keywords) appearing >= min_count times — this is the "procedural"
    pattern Memmy detects.

    For Stage 1 (this script), we use a simple co-occurrence heuristic:
    facts sharing >= 2 keywords in top-5.
    """
    keyword_groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in facts:
        kws = set((f.get("keywords") or [])[:5])
        if len(kws) >= 2:
            # use frozen 2-keyword prefix as cluster key
            key = tuple(sorted(kws)[:2])
            keyword_groups[key].append(f)

    clusters = []
    for key, group in keyword_groups.items():
        if len(group) < min_count:
            continue
        avg_conf = sum(f.get("confidence") or 0.5 for f in group) / len(group)
        if avg_conf < min_avg_conf:
            continue
        clusters.append({
            "keywords": list(key),
            "count": len(group),
            "avg_confidence": round(avg_conf, 3),
            "sample_fact_ids": [f["id"] for f in group[:5]],
            "recommended_kind": "rule" if avg_conf > 0.85 else "lesson",
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def _resolve_db(tier: str, user_id: str | None) -> Path:
    import os
    astor_dir = os.environ.get("ASTOR_DIR", r"D:\AI\Astor-Memory-Runtime")
    base = Path(astor_dir)
    if tier == "public":
        return base / "public" / "memory" / "astor_bus_public.db"
    if tier == "source":
        return base / "source" / "memory" / "astor_bus_source.db"
    if tier == "private":
        if not user_id:
            raise ValueError("private tier requires --user")
        return base / "users" / user_id / "memory" / f"astor_bus_{user_id}.db"
    raise ValueError(f"unknown tier: {tier}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="admin")
    ap.add_argument("--tier", default="private", choices=["public", "source", "private"])
    ap.add_argument("--min-imp", type=float, default=0.5, help="min importance to include")
    ap.add_argument("--min-cluster-count", type=int, default=3)
    ap.add_argument("--min-cluster-conf", type=float, default=0.7)
    ap.add_argument("--dry-run", action="store_true", help="show plan, do not update DB")
    ap.add_argument("--out", help="output JSON file (default stdout)")
    ap.add_argument("--db", help="override DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else _resolve_db(args.tier, args.user)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # Pull all active facts with min importance
    rows = con.execute("""
        SELECT id, content, kind, confidence, importance, keywords
        FROM memory_canonical
        WHERE tombstoned=0 AND importance >= ?
        ORDER BY importance DESC
    """, (args.min_imp,)).fetchall()

    # 1) Classify each
    classification = []
    for r in rows:
        new_kind, conf, rationale = classify_one(r["content"] or "")
        old_kind = r["kind"]
        if new_kind != old_kind:
            classification.append({
                "id": r["id"],
                "old_kind": old_kind,
                "new_kind": new_kind,
                "confidence": conf,
                "rationale": rationale,
                "importance": r["importance"],
                "snippet": (r["content"] or "")[:120],
            })

    # 2) Find skill clusters
    facts_dicts = [
        {"id": r["id"], "keywords": json.loads(r["keywords"] or "[]"),
         "confidence": r["confidence"], "content": r["content"]}
        for r in rows
    ]
    clusters = find_skill_clusters(
        facts_dicts,
        min_count=args.min_cluster_count,
        min_avg_conf=args.min_cluster_conf,
    )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db_path),
        "facts_scanned": len(rows),
        "reclassifications_proposed": len(classification),
        "reclassifications": classification,
        "skill_clusters_found": len(clusters),
        "skill_clusters": clusters,
    }

    out_str = json.dumps(result, indent=1, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(out_str)

    # Apply reclassifications if not dry-run
    if not args.dry_run and classification:
        print(f"\nApplying {len(classification)} reclassifications...", file=sys.stderr)
        for c in classification:
            con.execute(
                "UPDATE memory_canonical SET kind=? WHERE id=?",
                (c["new_kind"], c["id"])
            )
        con.commit()
        print("Done.", file=sys.stderr)

    con.close()


if __name__ == "__main__":
    main()
