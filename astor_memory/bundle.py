"""astor_memory.bundle — the memory state as one portable file.

MemT-1.0 showed one mechanism worth copying: a model can save its memory
state to a snapshot and load it back, so the memory travels as a file
rather than living in a system someone has to keep running.

Astor already has `am export`, which writes the memory as Markdown for a
person to read. That is a projection: it cannot be loaded back. This
module is the machine-readable counterpart. It packs the SQLite files
that hold the memory plus a manifest describing them, and it can put
them back.

What it packs, per scope (tier, user_id):
    bus   the facts, the events and the audit trail
    nest  the embeddings, so a restore does not need a re-embed pass
    forge the extraction working set

What it deliberately does not pack:
    the lex index, which is a derived cache that rebuilds from the bus
    the admin lock and the passcode, which belong to the machine
    the peer relationships, which `am peer export-config` already covers

A bundle never leaves the tier it came from. `private` and `source` stay
on this machine unless somebody copies the file themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "manifest.json"
BUNDLE_VERSION = 1

# The stores that hold memory. `lex` is a derived cache and is left out on
# purpose: it rebuilds from the bus, and a stale copy in a bundle is worse
# than no copy at all.
STORES = ("bus", "nest", "forge")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_counts(db: Path) -> dict:
    """Row count per table, so the manifest can prove what was packed."""
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    except sqlite3.Error:
        return out
    try:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            try:
                out[name] = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            except sqlite3.Error:
                out[name] = -1
    finally:
        conn.close()
    return out


def _rel_path(tier: str, user_id: str | None, store: str) -> str:
    """The path of one db inside ASTOR_DIR, in the 9-db layout.

    Mirrors _internal.acl_layout.get_db_path, but resolves against the
    directory the caller passed instead of the ASTOR_DIR env var, so a
    bundle can be made for a runtime other than the current one.
    """
    if tier == "private":
        if not user_id:
            raise ValueError("tier=private requires user_id")
        top = "users/%s" % user_id
        suffix = user_id
    elif tier == "repo":
        if not user_id:
            raise ValueError("tier=repo requires repo_id")
        top = "repos/%s" % user_id
        suffix = user_id
    else:
        top = tier
        suffix = tier
    return "%s/memory/astor_%s_%s.db" % (top, store, suffix)


def _scope_files(astor_dir: Path, tier: str, user_id: str | None, store: str) -> list[Path]:
    """The db files of one (tier, user_id, store), in the 9-db layout."""
    try:
        rel = _rel_path(tier, user_id, store)
    except ValueError:
        return []
    db = astor_dir / rel
    return [db] if db.exists() else []


def export_bundle(
    astor_dir: str | os.PathLike,
    out_path: str | os.PathLike,
    tiers: list[str] | None = None,
    user_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Pack the memory state into one tar.gz. Returns a report dict.

    tiers     which tiers to pack, default public only
    user_ids  private users to pack, default every user on disk
    """
    astor_dir = Path(astor_dir).expanduser().resolve()
    out_path = Path(out_path).expanduser().resolve()
    tiers = tiers or ["public"]
    user_ids = user_ids if user_ids is not None else _all_users(astor_dir)

    files: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="astor-bundle-") as tmp:
        tmpdir = Path(tmp)
        for tier in tiers:
            for user_id in (user_ids if tier == "private" else [None]):
                for store in STORES:
                    for db in _scope_files(astor_dir, tier, user_id, store):
                        name = _arc_name(tier, user_id, store, db)
                        target = tmpdir / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(db, target)
                        files.append({
                            "arcname": name,
                            "tier": tier,
                            "user_id": user_id,
                            "store": store,
                            "bytes": db.stat().st_size,
                            "sha256": _sha256(db),
                            "rows": _row_counts(db),
                        })

        manifest = {
            "bundle_version": BUNDLE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "astor_version": _astor_version(),
            "tiers": tiers,
            "users": user_ids,
            "stores": list(STORES),
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files),
        }
        manifest_path = tmpdir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if dry_run:
            return {"manifest": manifest, "out": str(out_path), "dry_run": True}

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tf:
            for f in files:
                tf.add(tmpdir / f["arcname"], arcname=f["arcname"])
            tf.add(manifest_path, arcname=MANIFEST_NAME)

    return {"manifest": manifest, "out": str(out_path), "dry_run": False}


def inspect_bundle(path: str | os.PathLike) -> dict:
    """Read a bundle's manifest without unpacking it."""
    path = Path(path).expanduser()
    with tarfile.open(path, "r:gz") as tf:
        member = tf.getmember(MANIFEST_NAME)
        fh = tf.extractfile(member)
        manifest = json.loads(fh.read().decode("utf-8"))
    manifest["bundle_path"] = str(path)
    manifest["bundle_bytes"] = path.stat().st_size
    return manifest


def import_bundle(
    path: str | os.PathLike,
    astor_dir: str | os.PathLike,
    dry_run: bool = False,
    verify: bool = True,
) -> dict:
    """Put a bundle back. Returns a report dict.

    The bus files are replaced whole, so a restore is exactly the state
    that was packed. The lex index is not in the bundle and is not
    touched; it rebuilds from the restored bus.
    """
    astor_dir = Path(astor_dir).expanduser().resolve()
    path = Path(path).expanduser().resolve()
    manifest = inspect_bundle(path)

    if manifest.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError(
            "bundle version %r is not supported (this build reads %d)"
            % (manifest.get("bundle_version"), BUNDLE_VERSION)
        )

    plan = []
    for f in manifest["files"]:
        target = astor_dir / f["arcname"]
        plan.append({
            "arcname": f["arcname"],
            "target": str(target),
            "bytes": f["bytes"],
            "sha256": f["sha256"],
        })

    if dry_run:
        return {"manifest": manifest, "plan": plan, "dry_run": True, "restored": 0}

    restored = 0
    with tempfile.TemporaryDirectory(prefix="astor-restore-") as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(path, "r:gz") as tf:
            for f in manifest["files"]:
                member = tf.getmember(f["arcname"])
                src = tmpdir / f["arcname"]
                src.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as fh, src.open("wb") as out:
                    shutil.copyfileobj(fh, out)
                if verify:
                    got = _sha256(src)
                    if got != f["sha256"]:
                        raise ValueError(
                            "checksum mismatch for %s: manifest says %s, file is %s"
                            % (f["arcname"], f["sha256"], got)
                        )
                target = astor_dir / f["arcname"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                restored += 1

    return {"manifest": manifest, "plan": plan, "dry_run": False, "restored": restored}


def _arc_name(tier: str, user_id: str | None, store: str, db: Path | None = None) -> str:
    """The path of a db inside the bundle, and inside ASTOR_DIR on restore."""
    return _rel_path(tier, user_id, store)


# Users that exist to probe the system, not to remember anything. Packing
# them would put test residue into every backup.
TEST_USERS = {"__aclprobe__", "hook-audit", "hook-audit-test", "demo_external_agent"}


def _all_users(astor_dir: Path) -> list[str]:
    users_dir = astor_dir / "users"
    if not users_dir.exists():
        return []
    return sorted(p.name for p in users_dir.iterdir()
                  if p.is_dir() and p.name not in TEST_USERS)


def _astor_version() -> str:
    from . import __version__
    return __version__
