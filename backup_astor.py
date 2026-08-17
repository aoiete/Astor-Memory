#!/usr/bin/env python3
"""
ASTOR backup (08-16 ship) — mirrors astor runtime + source code into
Google Drive for portable restoration.

Replaces the older `backup_portable_recovery.py` (07-03 ship) which
backed up the dead 3-store (bus/memu/palace, archived to
<mem_sys>/.archived-2026-08-16/).

Layout produced:
  F:/Google Drive/aoiete/AI stuff/Hermesbackup/ASTOR_BACKUP_<ts>/
    MANIFEST.md          — file inventory
    runtime/             — Astor runtime data (4 tiers × 3 stores SQLite + audit + lex)
    source/              — <source_dir> source code
    cron/                — Cron job definitions (jobs.json snapshot)

Retention: keep last 2 daily directories, prune older.

Usage (called by cron):
    python <source_dir>backup_astor.py
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_BACKUP = Path(r"F:/Google Drive/aoiete/AI stuff/Hermesbackup")
RETENTION_DAYS = 2
ASTOR_RUNTIME = Path(r"<runtime_dir>")
ASTOR_SOURCE = Path(r"<source_dir>")
HERMES_CRON_JOBS = Path(r"<home_dir>AppData/Local/hermes/cron/jobs.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _copy_tree(src: Path, dst: Path, ignore_hidden: bool = True) -> int:
    """Copy directory tree, return count of files copied."""
    if not src.exists():
        return 0
    count = 0
    dst.mkdir(parents=True, exist_ok=True)
    for src_file in src.rglob("*"):
        if src_file.is_file():
            # Skip __pycache__, .pyc, hidden
            if ignore_hidden and any(part.startswith(".") or part == "__pycache__" for part in src_file.parts):
                continue
            rel = src_file.relative_to(src)
            dst_file = dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            count += 1
    return count


def _prune_old_backups(backup_root: Path) -> int:
    """Remove ASTOR_BACKUP_* directories older than RETENTION_DAYS."""
    if not backup_root.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - (RETENTION_DAYS * 86400)
    removed = 0
    for sub in backup_root.iterdir():
        if not sub.is_dir():
            continue
        if not sub.name.startswith("ASTOR_BACKUP_"):
            continue
        # Use mtime
        if sub.stat().st_mtime < cutoff:
            print(f"  [prune] removing {sub}")
            shutil.rmtree(sub, ignore_errors=True)
            removed += 1
    return removed


def main() -> int:
    ts = _now_iso()
    out_dir = ROOT_BACKUP / f"ASTOR_BACKUP_{ts}"
    print(f"=== ASTOR backup wrapper start at {ts} ===")
    print(f"Output: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Runtime (public/source/users/repos tiers + audit + lex)
    print("\n[1/3] Mirroring ASTOR runtime...")
    rt_files = _copy_tree(ASTOR_RUNTIME, out_dir / "runtime")
    print(f"  copied {rt_files} runtime files")

    # 2. Source code
    print("\n[2/3] Mirroring ASTOR source code...")
    src_files = _copy_tree(ASTOR_SOURCE, out_dir / "source")
    print(f"  copied {src_files} source files")

    # 3. Cron jobs snapshot
    print("\n[3/3] Copying cron jobs snapshot...")
    if HERMES_CRON_JOBS.exists():
        shutil.copy2(HERMES_CRON_JOBS, out_dir / "cron_jobs.json")
        print("  copied jobs.json")

    # 4. Manifest
    manifest = {
        "created_at": ts,
        "astor_runtime": str(ASTOR_RUNTIME),
        "astor_source": str(ASTOR_SOURCE),
        "files_total": rt_files + src_files + 1,
    }
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n[manifest] total {manifest['files_total']} files")

    # 5. Prune old backups
    print(f"\n[prune] removing ASTOR_BACKUP_* older than {RETENTION_DAYS} days...")
    pruned = _prune_old_backups(ROOT_BACKUP)
    print(f"  pruned {pruned} old backups")

    print(f"\n=== ASTOR backup wrapper end: {out_dir} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())