"""Does the live server carry the fix?

The distiller fix is in the runtime copy; the running server has to be
the process that loaded it. This writes a fact that names an id and
reads back what the public tier stored.

The identifiers here are fabricated -- a bare number is a hash as
often as it is an id, and the point of the check is the shape.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

DB = Path(r"D:\AI\Astor-Memory-Runtime\public\memory\astor_bus_public.db")
BASE = "http://127.0.0.1:7803"

PAYLOAD = (
    "LESSON [R-class locked 2026-10-30] the live server check: a fact that "
    "names a message (message_id=7519050235899700000) and a bot "
    "(bot_id=8263b17ef000) and a session (weixin 20261005_085110_6e87fc76) "
    "must not keep any of them, while the script name "
    "scripts/fortune_daily_push_weixin.py must survive."
)


def post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}


def main() -> int:
    # 1. the write must be refused
    st, body = post("/v1/write", {
        "text": PAYLOAD, "kind": "lesson", "tier": "public",
        "visibility_hint": "commons", "user_id": "admin",
        "namespace": "leaktest",
    })
    print(f"  write -> HTTP {st}")
    if st >= 400:
        print(f"    {str(body.get('error'))}: "
              f"{str(body.get('detail'))[:140]}")

    # 2. nothing of it may be in the public tier
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT id, content FROM memory_canonical WHERE tombstoned = 0 "
        "AND content LIKE '%message_id=%'").fetchall()
    con.close()
    leaked = [r for r in rows if "7519050235899700000" in (r[1] or "")]

    print(f"  public tier rows naming that id: {len(leaked)}")

    # 3. the distiller itself, on the running code
    sys.path.insert(0, r"D:\AI\Astor-Memory-Runtime")
    from astor_memory.nest.distiller import distill
    cleaned, _, _ = distill(PAYLOAD)
    gone = "7519050235899700000" not in cleaned and "8263b17ef000" not in cleaned
    kept = "fortune_daily_push_weixin.py" in cleaned
    print(f"  distiller on the runtime copy: id gone={gone}, script kept={kept}")

    ok = not leaked and gone and kept
    print()
    print("  PASS" if ok else "  FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
