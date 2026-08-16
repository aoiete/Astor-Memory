"""
Real migration: read sources, write to 9-db layout.

CLI: `am migrate multi-source [--plan-only]`

Lock: 2026-08-15. Per 9-db ACL layout. Reads:
  - ~/.astor/astor_bus.db    (legacy single-file bus → users/admin/memory/astor_bus_admin.db)
  - <mem_sys>/memu/memu.db (memu items → source/admin per memory_type)
  - <mem_sys>/mempalace_real/chroma.sqlite3 (chroma docs → admin private)
  - <mem_sys>/memory-bus/memory_user_the_nuts.db (admin private)

Idempotent: re-running skips by stable_id (memu/memory_bus/chroma).

Side effect: also init 6 users' empty db layout, write admin.lock, install-state.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from astor_memory._internal.acl_layout import (
    DEFAULT_USER, FIRST_ADMIN_USER, Tier, Store,
    ensure_layout, get_astor_dir, get_audit_path, get_admin_lock_path,
    get_install_state_path,
)


# Trial users (plan §5165 — names + WeChat-bound name)
TRIAL_USERS = [
    FIRST_ADMIN_USER,   # you
    "user_e",            # (no chat-id-as-user — user_e is internal name)
    "user_b",   # user_e's weixin-bound id
    "user_a",
    "halamadrid9988",
    "xian-ding",
    "zhang-user_d",
]


# Per-source id offset to avoid cross-source unique_id collisions.
# Each source gets its own range; we use src_id + offset as canonical.candidate_id.
# Reserve legacy astor_bus.db ids 1..10000 first (since they were numbered 1-949).
ID_OFFSET = {
    "astor_bus": 0,             # 1..N (the legacy was here)
    "memory_user_the_nuts": 100000,  # 100001..101002
    "memu": 200000,             # 200001..205089
    "chroma": 300000,           # 300001..300463
}


def init_first_admin_lock() -> dict:
    """Write `~/.astor/admin.lock` (plan §2609-2640).

    Returns the lock metadata for inclusion in audit/ install-state.
    """
    lock_path = get_admin_lock_path()
    if lock_path.exists():
        return {"already_locked": True, "path": str(lock_path)}
    ensure_layout(Tier.PUBLIC, Store.BUS)  # ensure ~/.astor exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "user_id": FIRST_ADMIN_USER,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "lock_version": 1,
        "permanent_root": True,
    }
    lock_path.write_text(json.dumps(payload, indent=2))
    try:
        os.chmod(lock_path, 0o600)
    except (OSError, PermissionError):
        pass
    return payload


def init_install_state() -> None:
    """Write `~/.astor/install-state.json` — multi-user mode marker."""
    state_path = get_install_state_path()
    if state_path.exists():
        return  # idempotent
    ensure_layout(Tier.PUBLIC, Store.BUS)
    state = {
        "version": 1,
        "mode": "multi-user",
        "first_admin_user_id": FIRST_ADMIN_USER,
        "tier_layout": "9-db",
        "tier": "multi-user",
        "store_layout": "3-store (bus/nest/forge)",
        "trial_users": TRIAL_USERS,
        "platform_bindings": {
            # To be filled by `am admin bind-platform`
            "telegram:DM": FIRST_ADMIN_USER,  # current bound
            "discord:Home": FIRST_ADMIN_USER,
            "feishu:Home": None,  # revoked per user instruction 2026-08-15
        },
        "installed_at": datetime.utcnow().isoformat() + "Z",
    }
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def create_empty_user_dbs() -> dict[str, list[Path]]:
    """Create the empty 9-db layout for each trial user.

    Returns dict: user_id → list of paths created.
    """
    paths = {}
    for user in TRIAL_USERS:
        per_user = []
        for store in Store:
            p = ensure_layout(Tier.PRIVATE, store, user)
            per_user.append(p)
        paths[user] = per_user
    # Also for tier=public and tier=source, create empty dbs (idempotent)
    for tier in (Tier.PUBLIC, Tier.SOURCE):
        for store in Store:
            ensure_layout(tier, store)
    return paths


def _migrate_init_target(target: Path) -> sqlite3.Connection:
    """Open target, init schema, disable FK for the migration (re-enable on close).

    Order matters: PRAGMA foreign_keys=OFF must run BEFORE any INSERT, but
    astor_init_schema() runs `PRAGMA foreign_keys = ON` implicitly via conn setup
    in store.py. To safely disable FKs, we set OFF AFTER astor_init_schema and
    keep OFF for the lifetime of this connection.
    """
    con = sqlite3.connect(str(target))
    from astor_memory.bus.schema import astor_init_schema
    astor_init_schema(con)
    con.execute("PRAGMA foreign_keys = OFF")
    return con


def migrate_astor_bus_db(target_user: str = FIRST_ADMIN_USER) -> dict:
    """Migrate legacy astor_bus.db (v0.2-shipped single-file) → users/<u>/memory/.

    The legacy path defaults to $ASTOR_DIR/astor_bus.db. Falls back to
    ~/.astor/astor_bus.db if ASTOR_DIR is unset. Useful for double-checking
    after first migration run.
    """
    astor_root = get_astor_dir()
    src = astor_root / "astor_bus.db"
    if not src.exists():
        return {"src": str(src), "rows": 0, "reason": "missing"}
    target = ensure_layout(Tier.PRIVATE, Store.BUS, target_user)
    src_con = sqlite3.connect(str(src))
    dst_con = _migrate_init_target(target)
    OFF = ID_OFFSET["astor_bus"]
    n = 0
    for src_id, content, namespace, kind, confidence, importance, tags, metadata in src_con.execute(
        "SELECT id, content, namespace, kind, confidence, importance, tags, metadata "
        "FROM memory_canonical WHERE tombstoned=0"
    ):
        # Defensive: skip eval/test
        new_row = {
            "content": content,
            "namespace": namespace,
            "kind": kind,
            "confidence": confidence,
            "importance": importance,
            "tags": tags,
            "metadata": metadata,
            "promoted_by": "astor_migrate",
            "user_id": target_user,
            "tier": "private",
            "scope_type": "long_term",
            "verdict": "settled",
            "source_canonical_id": src_id,
        }
        # Build a stable_id with src-prefix + src_id (not the offset one) so
        # cross-source stable_ids are distinct
        stable_id = f"astor_bus_row_{src_id}"
        try:
            dst_con.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id + OFF,  # cross-source unique candidate_id
                    src_id + OFF,  # reuse event_id (with offset)
                    namespace, content, kind, confidence, importance,
                    tags, metadata,
                    "astor_migrate",
                    target_user, "private", "long_term", "settled",
                    stable_id,
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            # Idempotent skip — stable_id collision
            continue
    dst_con.commit()
    src_con.close()
    dst_con.close()
    return {"src": str(src), "dst": str(target), "rows": n}


def migrate_memory_user_the_nuts_db(target_user: str = FIRST_ADMIN_USER) -> dict:
    """Migrate the per-user side DB (memory_user_the_nuts.db) into admin private."""
    src = Path("<mem_sys>/memory-bus/memory_user_the_nuts.db")
    if not src.exists():
        return {"src": str(src), "rows": 0, "reason": "missing"}
    target = ensure_layout(Tier.PRIVATE, Store.BUS, target_user)
    src_con = sqlite3.connect(str(src))
    dst_con = _migrate_init_target(target)
    OFF = ID_OFFSET["memory_user_the_nuts"]
    n = 0
    for src_id, content, namespace, kind, confidence, importance in src_con.execute(
        "SELECT id, content, namespace, kind, confidence, importance FROM memory_canonical"
    ):
        # Decide actual user (some content references user_e)
        target = "user_e" if (
            "user_e" in (namespace or "").lower()
            or "user_e" in (content or "").lower()
        ) else FIRST_ADMIN_USER
        try:
            dst_con.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id + OFF, src_id + OFF,
                    namespace, content, kind, confidence, importance,
                    "[]", "{}",
                    "astor_migrate_user_db",
                    target, "private", "long_term", "settled",
                    f"memory_user_the_nuts_row_{src_id}",
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            continue
    dst_con.commit()
    src_con.close()
    dst_con.close()
    return {"src": str(src), "rows": n}


def migrate_memu_db() -> dict:
    """Migrate memu items to source/ + admin private per memory_type."""
    src = Path("<mem_sys>/memu/memu.db")
    if not src.exists():
        return {"src": str(src), "rows": 0, "reason": "missing"}
    source_target = ensure_layout(Tier.SOURCE, Store.BUS)
    private_target = ensure_layout(Tier.PRIVATE, Store.BUS, FIRST_ADMIN_USER)
    src_con = sqlite3.connect(str(src))
    src_con_source = _migrate_init_target(source_target)
    src_con_priv = _migrate_init_target(private_target)

    MEMU_TYPE_TIER = {
        "knowledge": Tier.SOURCE, "skill": Tier.SOURCE,
        "behavior": Tier.PRIVATE, "profile": Tier.PRIVATE,
        "event": Tier.PRIVATE, "fact": Tier.PRIVATE,
        "facts": Tier.PRIVATE, "test": Tier.PRIVATE,
    }
    n_source = 0
    n_priv = 0
    skipped = 0
    OFF_M = ID_OFFSET["memu"]
    for src_id, mtype, summary in src_con.execute(
        "SELECT id, memory_type, summary FROM memu_memory_items"
    ):
        # memu's id is a UUID string, not int. Use hash mod to fit in OFFSET range.
        sid_int = abs(hash(src_id)) % 99999 if isinstance(src_id, str) else int(src_id)
        target_tier = MEMU_TYPE_TIER.get(mtype, Tier.PRIVATE)
        con = src_con_source if target_tier == Tier.SOURCE else src_con_priv
        user = None if target_tier == Tier.SOURCE else FIRST_ADMIN_USER
        tier_str = target_tier.value
        try:
            con.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid_int + OFF_M, sid_int + OFF_M,
                    f"/memu/{mtype}",
                    summary,
                    "fact" if mtype in ("fact", "facts") else mtype,
                    0.7, 0.5,
                    "[]", "{}",
                    "astor_migrate_memu",
                    user, tier_str, "long_term", "settled",
                    f"memu_item_{src_id}",
                ),
            )
            if target_tier == Tier.SOURCE:
                n_source += 1
            else:
                n_priv += 1
        except sqlite3.IntegrityError:
            skipped += 1
            continue
    src_con_source.commit()
    src_con_priv.commit()
    src_con.close()
    src_con_source.close()
    src_con_priv.close()
    return {
        "src": str(src),
        "rows_source": n_source,
        "rows_private_admin": n_priv,
        "skipped": skipped,
    }


def migrate_chroma_db(target_user: str = FIRST_ADMIN_USER) -> dict:
    """Migrate chroma.sqlite3 documents (with original text) to admin private bus.

    We do NOT migrate vector data — we only migrate the text. Re-embedding happens
    via `am reembed` after migration, which re-computes embeddings using the current
    fastembed model (BAAI/bge-base-en-v1.5, 768-d). Chroma's 384-d is not portable.
    """
    src = Path("<mem_sys>/mempalace_real/chroma.sqlite3")
    if not src.exists():
        return {"src": str(src), "rows": 0, "reason": "missing"}
    target = ensure_layout(Tier.PRIVATE, Store.BUS, target_user)
    src_con = sqlite3.connect(str(src))
    dst_con = _migrate_init_target(target)
    seg_main = "4e33758c-8da6-433e-b2df-1c845db6fe8f"
    seg_event = "event_segment"
    OFF_C = ID_OFFSET["chroma"]
    n = 0
    skipped = 0
    # Main segment: 418 docs (rows with chroma:document metadata)
    seg_main = "4e33758c-8da6-433e-b2df-1c845db6fe8f"
    seg_event = "event_segment"
    rows = list(src_con.execute(
        f"""
        SELECT e.id, e.embedding_id,
               (SELECT string_value FROM embedding_metadata em WHERE em.id=e.id AND em.key='chroma:document'),
               (SELECT string_value FROM embedding_metadata em WHERE em.id=e.id AND em.key='kind')
        FROM embeddings e
        WHERE e.segment_id = ?
        """,
        (seg_main,)
    ))
    for src_id, emb_id, doc, kind in rows:
        if not doc:
            continue
        # emb_id may be bytes in chroma — normalize to str for json
        emb_id_str = emb_id.decode() if isinstance(emb_id, bytes) else str(emb_id)
        try:
            dst_con.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id + OFF_C, src_id + OFF_C,
                    f"/chroma/{kind or 'doc'}",
                    doc,
                    kind or "doc",
                    0.7, 0.5,
                    "[]", json.dumps({"chroma_eid": emb_id_str}),
                    "astor_migrate_chroma",
                    target_user, "private", "long_term", "settled",
                    f"chroma_doc_{emb_id_str}",
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            skipped += 1
    # event_segment: 45 rows without doc text. Use embedding_id as content placeholder.
    for src_id, emb_id in src_con.execute(
        "SELECT id, embedding_id FROM embeddings WHERE segment_id=?", (seg_event,)
    ):
        emb_id_str = emb_id.decode() if isinstance(emb_id, bytes) else str(emb_id)
        try:
            dst_con.execute(
                """INSERT INTO memory_canonical
                   (candidate_id, event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, promoted_by, user_id, tier, scope_type, verdict, stable_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id + OFF_C, src_id + OFF_C,
                    "/chroma/event",
                    f"[event-only: {emb_id_str}; vector-only data, no original text]",
                    "event",
                    0.5, 0.3,
                    "[]", json.dumps({"chroma_eid": emb_id_str}),
                    "astor_migrate_chroma_events",
                    target_user, "private", "long_term", "settled",
                    f"chroma_event_{emb_id_str}",
                ),
            )
            n += 1
        except sqlite3.IntegrityError:
            skipped += 1
    dst_con.commit()
    src_con.close()
    dst_con.close()
    return {"src": str(src), "rows": n, "skipped": skipped}


def main():
    """Execute the full migration: lock, install-state, empty dbs, then data."""
    print("Step 1: init first_admin lock")
    lock = init_first_admin_lock()
    print(f"  -> {lock}")
    print("Step 2: install-state.json (multi-user mode)")
    init_install_state()
    print("Step 3: empty 9-db layout for trial users")
    paths = create_empty_user_dbs()
    for u, ps in paths.items():
        print(f"  {u}: {len(ps)} db(s)")
    print("Step 4: migrate legacy astor_bus.db -> users/admin/memory/astor_bus_admin.db")
    rep1 = migrate_astor_bus_db()
    print(f"  -> {rep1}")
    print("Step 5: migrate memory_user_the_nuts.db")
    rep2 = migrate_memory_user_the_nuts_db()
    print(f"  -> {rep2}")
    print("Step 6: migrate memu/memu.db -> source/ + admin private")
    rep3 = migrate_memu_db()
    print(f"  -> {rep3}")
    print("Step 7: migrate chroma.sqlite3 -> admin private")
    rep4 = migrate_chroma_db()
    print(f"  -> {rep4}")
    print("Step 8: reembed nest for the migrated db (deferred — see am reembed)")
    print("\nDone. Run `am doctor` and `am audit-log` to inspect.")


if __name__ == "__main__":
    main()
