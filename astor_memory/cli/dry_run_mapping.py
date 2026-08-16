"""
Dry-run mapping: scan all source DBs and report where each row would land
in the 9-db layout (3 tier × 3 store). NO writes — just counts.

Run:
    python -m astor_memory.cli.dry_run_mapping [--source ~/.astor / D:/AI/memory-system]

Output: a JSON summary report mapping source_row → (tier, user_id, store, db_path).
Plus a per-source breakdown by memory_type / tier.

Lock: 2026-08-15. Per turn design: memory_type → tier map hardcoded.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from astor_memory._internal.acl_layout import (
    Tier, Store, get_db_path, get_astor_dir, FIRST_ADMIN_USER,
)


# === Memory-type → tier classifier (per turn 2026-08-15) ===
# memu memory_type determines default tier:
#   source: knowledge, skill        (agent's own learning)
#   private (admin by default): behavior, profile, event, fact(s), test
MEMU_TYPE_TIER = {
    "knowledge": Tier.SOURCE,
    "skill": Tier.SOURCE,
    "behavior": Tier.PRIVATE,
    "profile": Tier.PRIVATE,
    "event": Tier.PRIVATE,
    "fact": Tier.PRIVATE,
    "facts": Tier.PRIVATE,
    "test": Tier.PRIVATE,
}


@dataclass
class MappingReport:
    """Summary of one source → 9-db mapping."""
    source: str
    rows_total: int = 0
    rows_to_drop: int = 0
    by_target: dict = field(default_factory=lambda: defaultdict(int))  # (tier, user_id, store) → count
    sample_kept: list = field(default_factory=list)

    def to_dict(self) -> dict:
        def _k(t):
            return t.value if isinstance(t, Tier) else t
        return {
            "source": self.source,
            "rows_total": self.rows_total,
            "rows_to_drop": self.rows_to_drop,
            "by_target": {
                f"{_k(tier)}/{user_id or '-'}/{_k(store)}": count
                for (tier, user_id, store), count in self.by_target.items()
            },
            "sample_kept": self.sample_kept[:5],
        }


def map_old_user_to_admin(user_id: str | None) -> tuple[Tier, str]:
    """Map legacy user_id to (tier, current_user_id).

    Rules (per turn 2026-08-15):
    - None / 'operator' / 'the_nuts' / '' → admin private (operator = first_admin)
    - 'user_e' (or bazi-related) → user_e private
    - 'eval_*' / 'test' → drop (test data)
    - other → admin private (conservative default)
    """
    if user_id is None or user_id == "":
        return Tier.PRIVATE, FIRST_ADMIN_USER
    if user_id.lower() in {"operator", "the_nuts", "thenuts"}:
        return Tier.PRIVATE, FIRST_ADMIN_USER
    if user_id.startswith("eval_") or user_id == "test":
        return Tier.PRIVATE, "__drop__"
    if user_id.lower() in {"user_e", "user_b"}:
        return Tier.PRIVATE, "user_e"
    # Default: route to admin
    return Tier.PRIVATE, FIRST_ADMIN_USER


def scan_astor_bus_db(path: Path) -> MappingReport:
    """Scan ~/.astor/astor_bus.db (v0.2-shipped single-file).

    Default tier = PRIVATE (per turn 2026-08-15: instance data is always private,
    only 'mode/pattern' content can be promoted to public/source). Until a real
    tier-classifier is built, keep all astor_bus.db rows as private for safety.
    """
    r = MappingReport(source=str(path))
    if not path.exists():
        return r
    con = sqlite3.connect(str(path))
    for cid, content, user_id, _tier_field in con.execute(
        "SELECT id, content, user_id, tier FROM memory_canonical WHERE tombstoned=0"
    ):
        r.rows_total += 1
        # All astor_bus.db rows were written in the single-user mode before multi-tier
        # existed. Default to private <user> under the new 9-db layout.
        target_tier = Tier.PRIVATE
        target_user = FIRST_ADMIN_USER if user_id in (None, "", "operator", "the_nuts") else (
            "user_e" if (user_id or "").lower() in ("user_e", "user_b") else FIRST_ADMIN_USER
        )
        if user_id and (user_id.startswith("eval_") or user_id == "test"):
            r.rows_to_drop += 1
            continue
        r.by_target[(target_tier, target_user, "bus")] += 1
        if len(r.sample_kept) < 5:
            r.sample_kept.append({"id": cid, "user_id": user_id, "content": content[:80]})
    return r


def scan_memu_db(path: Path) -> MappingReport:
    """Scan <mem_sys>/memu/memu.db (5089 items)."""
    r = MappingReport(source=str(path))
    if not path.exists():
        return r
    con = sqlite3.connect(str(path))
    for iid, mtype, summary, user_id in con.execute(
        "SELECT id, memory_type, summary, user_id FROM memu_memory_items"
    ):
        r.rows_total += 1
        # tier from memory_type
        target_tier = MEMU_TYPE_TIER.get(mtype, Tier.PRIVATE)
        if target_tier == Tier.PRIVATE:
            # private — but if user_id is a known non-admin, route elsewhere
            if user_id and user_id.lower() in {"user_e", "user_b"}:
                target_user = "user_e"
            else:
                target_user = FIRST_ADMIN_USER
        else:
            # source — first_admin only, no user_id mapping
            target_user = None
        r.by_target[(target_tier, target_user, "bus")] += 1
    r.sample_kept = [
        {"id": r.rows_total, "summary": summary[:80]}
        for r_summary, in con.execute(
            "SELECT summary FROM memu_memory_items WHERE memory_type='skill' LIMIT 5"
        )
        for summary in [r_summary]
    ]
    return r


def scan_chroma_db(path: Path) -> MappingReport:
    """Scan <mem_sys>/mempalace_real/chroma.sqlite3 (418+45)."""
    r = MappingReport(source=str(path))
    if not path.exists():
        return r
    con = sqlite3.connect(str(path))
    seg_main = "4e33758c-8da6-433e-b2df-1c845db6fe8f"
    seg_event = "event_segment"
    # 418 docs in main segment → admin private (per kind tag distribution)
    query = """
        SELECT e.id, em_doc.string_value, em_kind.string_value
        FROM embeddings e
        LEFT JOIN embedding_metadata em_doc
          ON em_doc.id = e.id AND em_doc.key = 'chroma:document'
        LEFT JOIN embedding_metadata em_kind
          ON em_kind.id = e.id AND em_kind.key = 'kind'
        WHERE e.segment_id = ?
    """
    for eid, doc, kind in con.execute(query, (seg_main,)):
        r.rows_total += 1
        r.by_target[(Tier.PRIVATE, FIRST_ADMIN_USER, "bus")] += 1
        if len(r.sample_kept) < 5 and doc:
            r.sample_kept.append({"chroma_eid": eid, "kind": kind, "doc": doc[:80]})
    # 45 events in event_segment — mostly system events, no docs: route admin private anyway
    n_event = con.execute(
        "SELECT COUNT(*) FROM embeddings WHERE segment_id=?", (seg_event,)
    ).fetchone()[0]
    r.rows_total += n_event
    r.by_target[(Tier.PRIVATE, FIRST_ADMIN_USER, "bus")] += n_event
    return r


def scan_memory_user_the_nuts(path: Path) -> MappingReport:
    """Scan <mem_sys>/memory-bus/memory_user_the_nuts.db."""
    r = MappingReport(source=str(path))
    if not path.exists():
        return r
    con = sqlite3.connect(str(path))
    # 102 canonical + 48 unique (48 not in main bus)
    target_user_for = {"user_e", "user_b"}  # if summary mentions these -> user_e
    for iid, content, namespace in con.execute(
        "SELECT id, content, namespace FROM memory_canonical"
    ):
        r.rows_total += 1
        if "user_e" in (namespace or "").lower() or "sunny_zhang" in (content or "").lower():
            target = "user_e"
        else:
            target = FIRST_ADMIN_USER
        r.by_target[(Tier.PRIVATE, target, "bus")] += 1
        if len(r.sample_kept) < 5:
            r.sample_kept.append({"id": iid, "content": content[:80]})
    return r


def aggregate_reports(reports: list[MappingReport]) -> dict:
    """Combine per-source reports into a final target summary."""
    final = defaultdict(lambda: {"bus_rows": 0, "nest_rows": 0, "forge": "empty"})

    def _k(t):
        return t.value if isinstance(t, Tier) else t

    for rep in reports:
        for (tier, user_id, store), count in rep.by_target.items():
            key = f"{_k(tier)}/{user_id or '-'}/{_k(store)}"
            final[key]["bus_rows"] += count
            final[key]["nest_rows"] = final[key]["bus_rows"]
            final[key]["tier"] = _k(tier)
            final[key]["user_id"] = user_id
            final[key]["store"] = _k(store)
    return dict(final)


def main():
    """CLI: scan all known sources, report mapping, no writes."""
    sources = [
        Path("<runtime_dir>astor_bus.db"),
        Path("<mem_sys>/memu/memu.db"),
        Path("<mem_sys>/mempalace_real/chroma.sqlite3"),
        Path("<mem_sys>/memory-bus/memory_user_the_nuts.db"),
    ]

    reports = []
    for src in sources:
        if src.name == "astor_bus.db":
            rep = scan_astor_bus_db(src)
        elif src.name == "memu.db":
            rep = scan_memu_db(src)
        elif src.name == "chroma.sqlite3":
            rep = scan_chroma_db(src)
        elif src.name == "memory_user_the_nuts.db":
            rep = scan_memory_user_the_nuts(src)
        else:
            continue
        reports.append(rep)

    target_summary = aggregate_reports(reports)
    out = {
        "scan_mode": "DRY RUN — no writes",
        "sources": [rep.to_dict() for rep in reports],
        "target_summary": target_summary,
        "total_rows_to_migrate": sum(rep.rows_total for rep in reports),
        "total_rows_to_drop": sum(rep.rows_to_drop for rep in reports),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
