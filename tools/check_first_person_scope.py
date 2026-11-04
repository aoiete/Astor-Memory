"""Measure the first-person signal's reach in the shared tier.

Uses the package's own `has_first_person`, so the numbers cannot drift
from the shipped rule.

Read-only: it never writes to any database.

    ASTOR_DIR=/path/to/runtime python tools/check_first_person_scope.py
"""
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC))

from astor_memory.nest.visibility_classifier import (  # noqa: E402
    AUTO_COMMONS_KINDS, has_pii, has_emotion, has_geographic, has_first_person,
)

BRANCHES = [
    ("我 (singular)", re.compile(r'我(?!们|自己)')),
    ("我的", re.compile(r'我的')),
    ("我们 (plural)", re.compile(r'我们')),
    ("我自己", re.compile(r'我自己')),
    ("my", re.compile(r'\bmy\b')),
    ("our (plural)", re.compile(r'\bour\b')),
    ("mine", re.compile(r'\bmine\b')),
    ("us (plural)", re.compile(r'\bus\b')),
    ("me", re.compile(r'\bme\b')),
    ("家里", re.compile(r'家里')),
    ("family", re.compile(r'family')),
    ("home (bare substring)", re.compile(r'home')),
    ("home address", re.compile(r'home address')),
]

def runtime_dir() -> Path:
    root = os.environ.get("ASTOR_DIR") or os.environ.get("ASTOR_DIR_NAME")
    if root:
        return Path(root)
    return Path.home() / ".astor"


def collect(runtime: Path):
    rows = []
    for db in sorted(runtime.glob("**/*.db")):
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
    runtime = runtime_dir()
    if not runtime.exists():
        print(f"runtime not found: {runtime}")
        return 1
    rows = collect(runtime)
    shareable = [c for k, v, c in rows if k in AUTO_COMMONS_KINDS]
    personal = [c for _k, v, c in rows if v == "personal"]

    def blocked_alone(text):
        return (has_first_person(text)
                and not (has_pii(text) or has_emotion(text)
                         or has_geographic(text)))

    blocked = [c for c in shareable if blocked_alone(c)]
    priv = [c for c in personal if blocked_alone(c)]

    counts = Counter()
    for content in blocked:
        for name, rx in BRANCHES:
            if rx.search(content):
                counts[name] += 1

    print(f"runtime            : {runtime}")
    print(f"facts scanned      : {len(rows)}")
    print(f"shareable-kind     : {len(shareable)}")
    print(f"personal           : {len(personal)}")
    print()
    print(f"shareable facts blocked by first_person alone : {len(blocked)}")
    print(f"personal facts blocked by first_person alone  : {len(priv)}")
    print()
    print("branch distribution (a fact can hit several):")
    for name, _ in BRANCHES:
        n = counts.get(name, 0)
        pct = (n / len(blocked) * 100) if blocked else 0.0
        print(f"  {name:<24}{n:>5}  {pct:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
