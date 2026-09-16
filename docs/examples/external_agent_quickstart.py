#!/usr/bin/env python3
"""
external_agent_quickstart.py — minimal end-to-end demo of ANY non-Hermes
agent (Claude Code, Codex, custom script, etc.) using astor-memory.

What this proves:
  1. AstorClient is agent-agnostic — no Hermes dependency
  2. read() returns hits that include the metadata + tags we set
  3. write() persists cross-session (a later script can recall it)
  4. forget() soft-deletes (audit trail preserved)

Install:
    pip install astor-memory
    # OR (local source):
    #   PYTHONPATH=/path/to/astor-memory python external_agent_quickstart.py

Run:
    python external_agent_quickstart.py
    # expected: 3 facts returned from /v1/read, no errors

Reference:
    https://github.com/<repo-owner>/ASTOR-Memory  (docs/agent-adapters.md § Tier B)
"""
from __future__ import annotations

import os
import sys
import time

# Import is the same shape as astor-memory ships it. Either `pip install` or
# PYTHONPATH=<source>/src works.
try:
    from astor_memory.client import AstorClient
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from astor_memory.client import AstorClient  # type: ignore


BASE_URL = os.environ.get("ASTOR_BASE_URL", "http://127.0.0.1:7803")
# IMPORTANT: each external agent picks its own user_id so ACL keeps facts
# scoped. Never reuse 'admin' for non-admin agents in production.
# Demo uses 'admin' to bypass cross_user_forbidden; switch ASTOR_AGENT_USER
# to your agent's namespace for real deployments.
USER_ID = os.environ.get("ASTOR_AGENT_USER", "admin")
# tier: 'public' is shared bus; 'private' is per-user (recommended for
# external agents so they don't pollute admin's private store).
TIER = os.environ.get("ASTOR_AGENT_TIER", "public")


def main() -> int:
    client = AstorClient(
        base_url=BASE_URL,
        user_id=USER_ID,
        tier=TIER,
        transport="direct",  # "direct" = CLI/script (NOT through bot platform)
        agent_id="external_agent_quickstart",
    )

    print("== health ==")
    print(client.health())

    print("\n== write 3 demo facts ==")
    fid_quick = client.write(
        content="external_agent_demo: wrote via AstorClient on " + time.strftime("%Y-%m-%d"),
        kind="fact",
        importance=0.6,
        tags=["demo", "external_agent", "quickstart"],
    )
    print("  quickstart fact id:", fid_quick)

    fid_pref = client.write(
        content="user prefers concise replies and dislikes filler emoji",
        kind="user_preference",
        importance=0.85,
        tags=["demo", "preference"],
    )
    print("  preference fact id:", fid_pref)

    fid_lesson = client.write(
        content="SDK error 'Trade is not unlocked' → call unlock_trade(.env MOOMOO_PASSWORD) first",
        kind="lesson",
        importance=0.9,
        tags=["demo", "sdk-error", "moomoo"],
    )
    print("  lesson fact id:", fid_lesson)

    print("\n== read 'preference' ==")
    hits = client.read(query="preferences concise replies", top_k=5)
    for h in hits:
        print(f"  hit#{h.id} kind={h.kind} imp={h.importance:.2f} score={h.score} tags={h.tags}")
        print(f"    content: {h.content[:120]}")

    print("\n== read 'sdk error' ==")
    hits = client.read(query="moomoo unlock trade error", top_k=5)
    for h in hits:
        print(f"  hit#{h.id} kind={h.kind} imp={h.importance:.2f} tags={h.tags}")
        print(f"    content: {h.content[:120]}")

    print("\n== forget the quickstart fact (soft delete) ==")
    ok = client.forget(fact_id=fid_quick)
    print(f"  forget({fid_quick}) -> {ok}")

    print("\n== verify it's gone from read ==")
    hits = client.read(query="quickstart external_agent_demo", top_k=5)
    print(f"  remaining hits: {len(hits)}")

    print("\nDONE. Cross-session persistence: re-run in 5 min — preference+lesson "
          "still recallable; quickstart gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
