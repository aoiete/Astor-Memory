"""Score the shipped tier rule against the adversarial and positive sets.

The sets live in the test file (`tests/test_plural_leak_surface.py`), so
there is one source of truth. This tool measures the rule as it stands
and prints the three numbers that decide any change to it:

  leaks     adversarial private sentences that still reach the shared tier
  false     method notes the rule wrongly blocks
  corpus    facts already in the shared tier the rule would push back

Read-only: it never writes to any database.

    ASTOR_DIR=/path/to/runtime python tools/check_plural_guard_matrix.py
"""
import os
import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "tests"))

import astor_memory.nest.visibility_classifier as VC
from astor_memory.nest.visibility_classifier import classify_visibility
from test_plural_leak_surface import ADVERSARIAL, POSITIVE

RT = Path(os.environ.get("ASTOR_DIR") or Path.home() / ".astor")


def corpus():
    rows = []
    for db in sorted(RT.glob("**/*.db")):
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name='memory_canonical'").fetchone():
                continue
            cols = {r[1] for r in con.execute("PRAGMA table_info(memory_canonical)")}
            if not {"content", "visibility"} <= cols:
                continue
            for kind, vis, content in con.execute(
                    "SELECT kind, visibility, content FROM memory_canonical"):
                if content:
                    rows.append((kind or "", vis or "", content))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return rows


def main() -> int:
    if not RT.exists():
        print(f"runtime not found: {RT}")
        return 1
    rows = corpus()
    commons = [c for k, v, c in rows
               if v == "commons" and k in VC.AUTO_COMMONS_KINDS]
    print(f"runtime {RT}")
    print(f"facts {len(rows)}   shared-tier shareable-kind {len(commons)}")
    print()

    leaked = [t for t in ADVERSARIAL
              if classify_visibility(text=t, kind="lesson", hint=None,
                                     admin_allow_commons=True)["visibility"] == "commons"]
    blocked = [t for t in POSITIVE
               if classify_visibility(text=t, kind="lesson", hint=None,
                                      admin_allow_commons=True)["visibility"] == "personal"]
    push = [c for c in commons if VC.has_first_person(c)]

    print(f"leaks  {len(leaked)}/{len(ADVERSARIAL)}")
    print(f"false  {len(blocked)}/{len(POSITIVE)}")
    print(f"corpus-pushback {len(push)}/{len(commons)}")
    if leaked:
        print("\nleaking:")
        for t in leaked:
            print("   -", t[:90])
    if blocked:
        print("\nwrongly blocked:")
        for t in blocked:
            print("   -", t[:90])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
