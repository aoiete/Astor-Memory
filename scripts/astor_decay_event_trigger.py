#!/usr/bin/env python
"""
astor_decay_event_trigger.py — v1.15.3 (2026-09-22, MemTensor learning).

Incremental decay-sweep wrapper for event-driven / cron use. Reads the
last sweep's max canonical id from a small state file, then calls
`am decay-sweep run --since-canonical-id <last_id>` so we only sweep
new facts since the last run instead of full-table every cron tick.

Inspired by MemTensor/Metis (微信 article 2026-09-22): "online memory
maintenance is gradient-free — only one forward pass per update".
Applied to astor's decay lifecycle: instead of a 24h full scan, sweep
only what has been written since the last tick.

State file: <ASTOR_DIR>/astor/metrics/decay_last_sweep.json
  - {"last_canonical_id": int, "last_run_iso": iso8601, "tier": str}

Usage:
    astor_decay_event_trigger.py [--tier public] [--user-id <id>] [--max-importance 0.5]
                                  [--low-access-count 0] [--execute] [--state-file PATH]
    # default: dry-run, public tier, max-importance=0.5

Exit codes:
    0 = sweep completed (or 0 new facts — nothing to do)
    1 = error (state file corrupt, am CLI failed, etc.)
    2 = no ASTOR_DIR set / no bus db reachable
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _resolve_state_file(state_file: str | None) -> Path:
    """Resolve state file path. Default = <ASTOR_DIR>/astor/metrics/decay_last_sweep.json."""
    if state_file:
        return Path(state_file)
    astor_dir = os.environ.get("ASTOR_DIR") or str(Path.home() / ".astor")
    return Path(astor_dir) / "astor" / "metrics" / "decay_last_sweep.json"


def _load_state(state_path: Path) -> dict:
    """Load state file or return empty defaults."""
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # v1.15.3: corrupt state = start from 0 (full sweep fallback).
            return {"last_canonical_id": 0, "last_run_iso": None, "tier": None}
    return {"last_canonical_id": 0, "last_run_iso": None, "tier": None}


def _save_state(state_path: Path, last_id: int, tier: str) -> None:
    """Persist state atomically (write to .tmp + rename)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_canonical_id": last_id,
        "last_run_iso": datetime.now(timezone.utc).isoformat(),
        "tier": tier,
    }
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)


def _query_max_canonical_id(tier: str, user_id: str | None) -> int:
    """Read max(id) from memory_canonical via direct SQLite (skip the LLM path)."""
    astor_dir = os.environ.get("ASTOR_DIR") or str(Path.home() / ".astor")
    if tier == "public":
        db = Path(astor_dir) / "public" / "memory" / "astor_bus_public.db"
    elif tier == "source":
        db = Path(astor_dir) / "source" / "memory" / "astor_bus_source.db"
    elif tier == "private":
        if not user_id:
            print("[decay-trigger] private tier requires --user-id", file=sys.stderr)
            return 0
        db = Path(astor_dir) / "users" / user_id / "memory" / f"astor_bus_{user_id}.db"
    else:
        print(f"[decay-trigger] unknown tier: {tier}", file=sys.stderr)
        return 0
    if not db.exists():
        print(f"[decay-trigger] bus db not found: {db}", file=sys.stderr)
        return 0
    import sqlite3
    try:
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM memory_canonical").fetchone()
            return int(row[0])
    except sqlite3.Error as e:
        print(f"[decay-trigger] sqlite error on {db}: {e}", file=sys.stderr)
        return 0


def _run_am_decay_sweep(tier: str, user_id: str | None, since_id: int,
                         max_importance: float, low_access_count: int,
                         execute: bool) -> tuple[int, str]:
    """Invoke `am decay-sweep run` and return (rc, stdout)."""
    cmd = [
        sys.executable, "-m", "astor_memory.cli.main",
        "decay-sweep", "run",
        "--tier", tier,
        "--since-canonical-id", str(since_id),
        "--max-importance", str(max_importance),
        "--low-access-count", str(low_access_count),
        "--limit", "5000",  # higher limit because incremental sweep range is smaller
    ]
    if user_id:
        cmd += ["--user-id", user_id]
    if execute:
        cmd.append("--execute")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return (1, "timeout after 120s")
    except FileNotFoundError as e:
        return (1, f"failed to launch am: {e}")
    out = (proc.stdout or "") + (proc.stderr or "")
    return (proc.returncode, out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Incremental decay-sweep trigger (MemTensor learning v1.15.3)",
    )
    p.add_argument("--tier", default="public", choices=["public", "source", "private"],
                   help="Tier to sweep (default public)")
    p.add_argument("--user-id", default=None,
                   help="User id for private tier sweeps")
    p.add_argument("--max-importance", type=float, default=0.5,
                   help="Soft-tombstone threshold (default 0.5)")
    p.add_argument("--low-access-count", type=int, default=0,
                   help="Max access_count to qualify (default 0)")
    p.add_argument("--execute", action="store_true",
                   help="Actually tombstone; default is dry-run report only")
    p.add_argument("--state-file", default=None,
                   help="Override state file path (default: <ASTOR_DIR>/astor/metrics/decay_last_sweep.json)")
    args = p.parse_args(argv)

    if not os.environ.get("ASTOR_DIR"):
        print("[decay-trigger] ASTOR_DIR not set — exiting", file=sys.stderr)
        return 2

    state_path = _resolve_state_file(args.state_file)
    state = _load_state(state_path)

    # Tier change = full re-sweep for new tier (don't reuse stale id from another tier)
    if state.get("tier") and state.get("tier") != args.tier:
        since_id = 0
    else:
        since_id = int(state.get("last_canonical_id") or 0)

    # Read current max id so we can advance the cursor even if 0 rows swept.
    cur_max = _query_max_canonical_id(args.tier, args.user_id)
    if cur_max == 0:
        print("[decay-trigger] no canonical bus db reachable or empty tier", file=sys.stderr)
        return 2

    if since_id >= cur_max:
        # Nothing new since last sweep — common case, no work needed.
        print(f"[decay-trigger] nothing new (since_id={since_id} cur_max={cur_max})")
        return 0

    print(f"[decay-trigger] sweeping tier={args.tier} since_id={since_id} → cur_max={cur_max}")
    rc, out = _run_am_decay_sweep(args.tier, args.user_id, since_id,
                                   args.max_importance, args.low_access_count, args.execute)
    print(out)
    if rc != 0:
        print(f"[decay-trigger] am decay-sweep failed rc={rc}", file=sys.stderr)
        return 1

    # Advance cursor regardless of dry/execute — cursor tracks "what we've scanned",
    # not "what we tombstoned". A future sweep can revisit low-importance rows if
    # --max-importance is tightened, so we never want to lose the cursor position.
    _save_state(state_path, cur_max, args.tier)
    print(f"[decay-trigger] cursor advanced: {since_id} → {cur_max}")
    return 0


if __name__ == "__main__":
    sys.exit(main())