"""Backfill L0 abstract + L1 overview for canonical facts that lack them.

astor has carried L0/L1 fields since v1.6.0 (2026-08-25) -- the extractor
derives them, insert_candidate() accepts them, and store.py writes them
into the metadata JSON as __abstract__ / __overview__. Measured on the
live runtime on 2026-11-01: 6884 canonical facts, 29 with L0 (0.4%), 0
with L1. The write path never passed the values through, so the fields
were empty for every fact written since then.

This derives the same values the regex path of the extractor would
produce (first sentence for L0, first 240 chars for L1) and writes them
into the metadata JSON of the canonical row. It is a metadata-only
UPDATE -- no re-embedding, no index rebuild, no content change.

Facts whose content is already short (say under 200 chars) get an L0
that is the whole fact, which is a pointless copy; those are skipped
unless --force. Long facts are the ones where an L0 actually saves
tokens.

Usage:
    python tools/backfill_l0l1.py --dry-run
    python tools/backfill_l0l1.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# Same resolution the rest of the tooling uses: $ASTOR_DIR first, then the
# install record, then the default. Hard-coding a path here would put one
# machine's layout into a public repository.
from astor_memory.config import get_default_astor_dir

RUNTIME = get_default_astor_dir()

_SENT_SPLIT = re.compile(r'[.!?。！？\n]')


def derive_abstract(content: str, cap: int = 320) -> str:
    """L0: first sentence, capped at ~80 tokens. Same rule as the
    extractor regex path and llm_extract._derive_abstract()."""
    if not content:
        return ''
    parts = _SENT_SPLIT.split(content.strip(), maxsplit=1)
    abstract = parts[0].strip()
    if len(abstract) > cap:
        abstract = abstract[:cap - 3] + '...'
    return abstract


def derive_overview(content: str, cap: int = 240) -> str:
    """L1: leading digest of the fact."""
    if not content:
        return ''
    return content[:cap].strip()


def find_dbs(root: Path) -> list[Path]:
    out: list[Path] = []
    for pattern in ("public/memory/astor_bus_*.db",
                    "source/memory/astor_bus_*.db",
                    "users/*/memory/astor_bus_*.db"):
        out.extend(sorted(root.glob(pattern)))
    return [p for p in out if "archive" not in p.parts]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-length", type=int, default=200,
                    help="only backfill facts whose content is at least this long")
    ap.add_argument("--sessions", action="store_true",
                    help="also backfill __session_id__ from origin_session_id")
    ap.add_argument("--runtime", default=str(RUNTIME))
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        print("[ERR] pass --dry-run or --apply")
        return 1

    dbs = find_dbs(Path(args.runtime))
    if not dbs:
        print(f"[ERR] no canonical dbs under {args.runtime}")
        return 1

    total_scan = total_upd = 0
    for db in dbs:
        con = sqlite3.connect(str(db))
        try:
            if args.sessions:
                rows = con.execute(
                    "SELECT id, content, metadata, origin_session_id "
                    "FROM memory_canonical "
                    "WHERE tombstoned = 0 AND content IS NOT NULL AND content <> ''"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id, content, metadata FROM memory_canonical "
                    "WHERE tombstoned = 0 AND content IS NOT NULL AND content <> ''"
                ).fetchall()
        except sqlite3.Error as exc:
            print(f"[SKIP] {db.name}: {exc}")
            con.close()
            continue

        upd: list[tuple[str, int]] = []
        for row in rows:
            fid, content, meta_raw = row[0], row[1], row[2]
            origin_sid = row[3] if args.sessions and len(row) > 3 else None
            total_scan += 1
            try:
                meta = json.loads(meta_raw or "{}")
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            has_l0 = bool(meta.get("__abstract__"))
            has_l1 = bool(meta.get("__overview__"))
            need_sid = (args.sessions and origin_sid
                        and not meta.get("__session_id__"))
            if has_l0 and has_l1 and not need_sid:
                continue
            if len(content) < args.min_length and not need_sid:
                continue
            changed = False
            if not has_l0 and len(content) >= args.min_length:
                meta["__abstract__"] = derive_abstract(content)
                changed = True
            if not has_l1 and len(content) >= args.min_length:
                meta["__overview__"] = derive_overview(content)
                changed = True
            if need_sid:
                # origin_session_id is the conversation a fact was written
                # in. The extractor stores the same thing as __session_id__
                # in the metadata; when one path filled it and the other
                # did not, the fact loses its session grouping.
                meta["__session_id__"] = str(origin_sid)[:128]
                changed = True
            if changed:
                upd.append((json.dumps(meta, ensure_ascii=False), fid))

        if upd and args.apply:
            con.executemany(
                "UPDATE memory_canonical SET metadata = ? WHERE id = ?", upd)
            con.commit()
        con.close()

        total_upd += len(upd)
        print(f"  {db.relative_to(Path(args.runtime))}: scanned {len(rows)}, "
              f"{'updated' if args.apply else 'to update'} {len(upd)}")

    print(f"\n  total scanned {total_scan}, "
          f"{'updated' if args.apply else 'to update'} {total_upd}")
    if args.dry_run:
        print("  (dry run -- nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
