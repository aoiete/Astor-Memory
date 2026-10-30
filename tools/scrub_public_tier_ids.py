"""Clean the identifiers out of facts that already reached the shared tier.

The code fix stops this happening again. It does not undo what is
already there: nine lesson facts in the live public tier carry a
message id, a bot account id, a cron job id, or session ids like
`weixin 20261005_085110`.

The lessons are worth keeping -- they are the method, and the method
is the point of the shared tier. The identifiers are not: they name
this machine's messages, accounts and jobs, and every peer in the
federation can read the public tier.

So this rewrites the content in place, replacing the identifiers with
placeholders, and leaves the fact, its kind and its visibility alone.
It is dry-run by default: `--apply` is what writes.

Run with the project interpreter:
    D:/AI/PY-311/Scripts/python.exe tools/scrub_public_tier_ids.py
    D:/AI/PY-311/Scripts/python.exe tools/scrub_public_tier_ids.py --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.visibility_classifier import has_pii  # noqa: E402

DEFAULT_DB = Path(r"D:\AI\Astor-Memory-Runtime\public\memory\astor_bus_public.db")

# A session id in the text: the platform name, then the id. The id is a
# timestamp, an underscore, a short hex id and sometimes a second
# suffix -- "weixin 20261005_085110_6e87fc76". The platform name is
# what makes it identifiable; a bare timestamp is a date.
SESSION = re.compile(
    r"\b(weixin|discord|telegram|feishu|slack)\s+"
    r"(\d{8}(?:_[0-9a-f]{4,}){1,3})\b", re.I)

# An id that names itself: the key says what it is. The value runs to
# the end of the token -- "session_id=20261009_103725_0844a80f" is one
# id in two parts, and taking only the first part leaves the rest of
# it in the text.
KEYED = re.compile(
    r"\b((?:message|msg|chat|session|conversation|bot|account|user|thread|"
    r"group|channel|cron|job)[_\s-]?id)\s*=\s*"
    r"([0-9a-f]{8,}(?:_[0-9a-f]{4,}){0,3}|[0-9]{8,}(?:_[0-9a-f]{4,}){0,3})", re.I)

# A platform identifier with the word in front of it: "bot 8263b17ef000",
# "admin 微信同样失败". The word is what says it is an identifier, so a
# bare hex string without one -- a commit sha -- is left alone.
CONTEXT_ID = re.compile(
    r"\b(bot|account|acct|admin|chat|session|user|job)\s+"
    r"([0-9a-f]{10,}(?:_[0-9a-f]{4,}){0,3})(?=[,\s]|$)", re.I)

# A bare platform identifier inside a [STATE-CONST] block. The
# distiller strips the keys, so "[STATE:8]=8263b17ef000" is what a bot
# id looks like in the shared tier -- the key said "bot id" once, in
# the text the block was built from, and that text is gone.
#
# Only inside the block: a twelve-character hex string in free prose
# is usually a commit sha, which is worth sharing, and the rule that
# catches it would take that with it.
STATE_BLOCK_ID = re.compile(
    r"(\[[A-Za-z0-9_:.-]+\]=)([0-9a-f]{10,}(?:_[0-9a-f]{4,}){0,3})(?=[,\s]|$)")

# A bare platform account id: "8263b17ef000@im.bot".
WECHAT_ACCT = re.compile(r"\bo[A-Za-z0-9_-]{10,}@im\.(?:bot|wechat)\b")


def scrub(text: str) -> tuple[str, list[str]]:
    """Replace the identifiers, and say what was replaced."""
    found: list[str] = []

    def _session(m):
        found.append(f"session:{m.group(1)}")
        return f"{m.group(1)} [session]"

    def _keyed(m):
        found.append(f"keyed:{m.group(1)}")
        return f"{m.group(1)}=[id]"

    def _acct(m):
        found.append("wechat_account")
        return "[account]"

    def _state_id(m):
        found.append("state_block_id")
        return f"{m.group(1)}[id]"

    def _context_id(m):
        found.append(f"context_id:{m.group(1)}")
        return f"{m.group(1)} [id]"

    out = SESSION.sub(_session, text)
    out = KEYED.sub(_keyed, out)
    out = WECHAT_ACCT.sub(_acct, out)
    out = CONTEXT_ID.sub(_context_id, out)
    # last: inside the state block, where the key is gone
    out = STATE_BLOCK_ID.sub(_state_id, out)
    return out, found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: dry run)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"  no such db: {db}")
        return 1

    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT id, content FROM memory_canonical WHERE tombstoned = 0"
    ).fetchall()

    changed = []
    for fid, content in rows:
        new, found = scrub(content or "")
        if found and new != content:
            changed.append((fid, found, content, new))

    print(f"  {len(rows)} live facts, {len(changed)} would change")
    for fid, found, old, new in changed:
        print(f"    fact {fid}: {len(found)} identifier(s) -> {sorted(set(found))}")
        for line_old, line_new in zip(old.split("\n"), new.split("\n")):
            if line_old != line_new:
                print(f"       - {line_old[:150]}")
                print(f"       + {line_new[:150]}")
        print()

    if not args.apply:
        print("  dry run. --apply to write.")
        return 0

    con = sqlite3.connect(str(db))
    for fid, found, old, new in changed:
        con.execute("UPDATE memory_canonical SET content = ? WHERE id = ?",
                    (new, fid))
    con.commit()
    con.close()
    print(f"  rewrote {len(changed)} facts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
