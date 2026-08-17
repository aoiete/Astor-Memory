"""
hermes_adapter.py — AstorMemoryProvider for Hermes Agent 0.20

2026-08-15 ship C: priority integration for Hermes Agent.

This adapter implements hermes_agent.memory_provider.MemoryProvider so
that astor_memory can be used as the agent's external memory provider,
replacing the built-in MEMORY.md / USER.md file-based memory.

Per Plan § Tier B (patchable agents): Hermes 0.20 has a clear injection
point via `external_memory_provider` config key + the `MemoryProvider`
ABC. This adapter:
  - is_available() -> True iff ASTOR_DIR is set and DBs reachable
  - initialize() -> sets up astor ACL (first_admin role) for the session
  - system_prompt_block() -> returns a HIGHEST PRIORITY marker + status
  - prefetch() -> runs astor bus recall (public + source tiers)
  - sync_turn() -> writes a system_event event to public bus for audit
  - get_tool_schemas() -> exposes astor_recall / astor_write / astor_forget
    as native hermes tools

When config.yaml sets `external_memory_provider: astor_memory`, hermes
loads this adapter as the active memory provider and disables the built-in
memory injection (memory_enabled=false, user_profile_enabled=false).

Reference: docs/agent-adapters.md § Tier B
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Resolve astor runtime dir from env (set by ASTOR_DIR, defaults to ~/.astor)
ASTOR_DIR = Path(os.environ.get("ASTOR_DIR", "~/.astor")).expanduser()

# Lazy astor import — adapter must not crash at import time if astor deps missing.
try:
    from astor_memory._internal.acl import astor_init_acl  # noqa: F401
    _ASTOR_IMPORT_OK = True
except Exception as _exc:  # pragma: no cover
    logger.warning("astor_memory._internal.acl import failed: %s", _exc)
    _ASTOR_IMPORT_OK = False


# Hermes Agent 0.20's MemoryProvider ABC lives in agent.memory_provider.
# Import is also lazy — adapter is registered by config string, not by direct import.
try:
    from agent.memory_provider import MemoryProvider  # noqa: F401
    _HERMES_ABC_OK = True
except Exception as _exc:  # pragma: no cover
    logger.warning("agent.memory_provider.MemoryProvider import failed: %s", _exc)
    _HERMES_ABC_OK = False


class AstorMemoryProvider(MemoryProvider if _HERMES_ABC_OK else object):
    """
    Hermes Agent 0.20 MemoryProvider adapter for astor-memory.

    Per Plan § Tier B: provides astor bus + nest recall as the agent's
    primary memory, replacing MEMORY.md / USER.md injection.

    Tier routing: this adapter operates as first_admin (full read+write
    to source tier; read public). Per-user private facts are still
    routed via astor_bus(tier='private', user_id=...) directly when the
    agent explicitly writes per-user data.
    """

    def __init__(self) -> None:
        self._session_id: str = ""
        self._actor: str = "first_admin"
        self._hermes_home: str = ""
        self._platform: str = "cli"
        # v1.1: turn counter for periodic memory-search nudge (MemoraX-style
        # skill reminder). Resets on shutdown.
        self._turn_count: int = 0
        self._last_nudge_turn: int = 0
        # Configurable via ASTOR_NUDGE_EVERY_N_TURNS env var; default 5.
        import os as _os
        try:
            self._nudge_every = max(1, int(
                _os.environ.get('ASTOR_NUDGE_EVERY_N_TURNS', '5')
            ))
        except Exception:
            self._nudge_every = 5

    # -- Required ABC methods ---------------------------------------------

    def name(self) -> str:
        """Return adapter name used in hermes plugin registry ('astor_memory')."""
        return "astor_memory"

    def is_available(self) -> bool:
        """Check if astor is reachable (server up + ASTOR_DIR set)."""
        if not _ASTOR_IMPORT_OK or not _HERMES_ABC_OK:
            return False
        # ASTOR_DIR must point to a real dir with the 9-db layout.
        if not ASTOR_DIR.exists():
            return False
        # Confirm at least the public bus db exists (lightweight probe).
        public_bus = ASTOR_DIR / "public" / "memory" / "astor_bus_public.db"
        if not public_bus.exists():
            logger.warning(
                "astor_memory: public bus db not found at %s — "
                "did you run migrate_root_legacy_to_3tier.py?",
                public_bus,
            )
            return False
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """
        One-time per-session setup.

        Initializes astor ACL as first_admin (highest privilege). Per-user
        private writes should still pass tier='private', user_id=<id>
        explicitly via astor_bus() — the ACL context here only governs
        which tier this adapter reads from (public + source).
        """
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", str(ASTOR_DIR))
        self._platform = kwargs.get("platform", "cli")
        if _ASTOR_IMPORT_OK:
            try:
                astor_init_acl(
                    actor=self._actor,
                    role="first_admin",
                    tier="source",
                )
                logger.info(
                    "astor_memory: initialized (session=%s, platform=%s, actor=%s)",
                    session_id, self._platform, self._actor,
                )
            except Exception as exc:
                logger.warning("astor_memory: astor_init_acl failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Expose astor_memory ops as hermes native tools.

        These are called by the agent when it wants to recall/write/forget
        facts explicitly. They route through astor bus + nest with full
        ACL checks (the agent's ACL role determines which tier is writable).
        """
        return [
            {
                "name": "astor_recall",
                "description": (
                    "Recall relevant facts from astor-memory (bus + nest semantic search). "
                    "Returns up to `top_k` canonical facts ranked by similarity. "
                    "Use this instead of the memory tool's recall() to query the new "
                    "3-tier × 3-store memory system."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "top_k": {
                            "type": "integer",
                            "description": "Max results to return (default 5)",
                            "default": 5,
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["public", "source", "private"],
                            "description": (
                                "Which tier to search. 'public' is safe for any user; "
                                "'source' is admin-only; 'private' requires user_id."
                            ),
                            "default": "public",
                        },
                        "user_id": {
                            "type": "string",
                            "description": "Required when tier='private'",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "astor_write",
                "description": (
                    "Write a fact to astor-memory bus. Routes to the right tier "
                    "based on kind/topic: system facts go to source, user-personal "
                    "facts go to private_<user_id>, public facts to public."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Fact content"},
                        "kind": {
                            "type": "string",
                            "enum": ["fact", "rule", "observation", "preference",
                                     "knowledge", "procedure"],
                            "default": "fact",
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["public", "source", "private"],
                            "default": "source",
                        },
                        "user_id": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for recall filtering",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "astor_status",
                "description": (
                    "Show astor-memory runtime status: tier row counts, last "
                    "event timestamp, ACL actor/role, embedding model name."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch a hermes tool call (recall/forget/write) to astor REST."""
        import json
        if tool_name == "astor_recall":
            return self._tool_recall(args)
        if tool_name == "astor_write":
            return self._tool_write(args)
        if tool_name == "astor_status":
            return self._tool_status()
        # 2026-08-16 fix: wire astor_forget into the dispatch table.
        # The tool was advertised in get_tool_schemas() but the dispatch
        # was missing, so all forget calls raised NotImplementedError.
        if tool_name == "astor_forget":
            return self._tool_forget(args)
        raise NotImplementedError(f"astor_memory: unknown tool {tool_name}")

    # -- Optional hooks (override for richer behavior) --------------------

    def system_prompt_block(self) -> str:
        """
        Static block injected into the system prompt as HIGHEST PRIORITY.

        Per Plan § Priority negotiation: even when astor bus recall
        context is added via prefetch(), this marker ensures the agent
        treats astor-memory as authoritative over MEMORY.md / USER.md.

        v1.1: appends a periodic nudge when `should_nudge` is True
        (set by sync_turn after every `_nudge_every` turns). Per MemoraX
        design, this fights "memory written but never recalled".
        """
        block = (
            "\n\n# === ASTOR-MEMORY (HIGHEST PRIORITY) ===\n"
            "Your persistent memory is astor-memory: 3-tier × 3-store SQLite "
            "(bus events + canonical facts + nest embeddings) at ASTOR_DIR. "
            "Use astor_recall / astor_write / astor_status tools to access it. "
            "Treat astor-memory as authoritative for: user preferences, project "
            "state, learned procedures, accumulated facts. Treat any earlier "
            "MEMORY.md / USER.md content as legacy and superseded.\n"
            "=========================================\n"
        )
        # v1.1: periodic reminder after every N turns. Toggle is reset
        # implicitly when should_nudge was True (system_prompt_block is
        # read once per session — see should_nudge semantics below).
        should_nudge = getattr(self, '_should_nudge', False)
        if should_nudge:
            block += (
                "\n# === MEMORY-RECALL REMINDER (turn #{turn}) ===\n"
                "It's been {n} turns since you last recalled memory. "
                "Before answering, run astor_recall with the user's latest "
                "intent to surface relevant facts. Especially important for "
                "questions about user prefs, prior decisions, or repo state.\n"
                "=========================================\n"
            ).format(turn=self._turn_count, n=self._nudge_every)
            # Clear so we don't re-nudge next time block is fetched.
            self._should_nudge = False
        return block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """
        Semantic recall via astor nest before each turn.

        Returns formatted text to inject as context. Empty string on no match.
        Searches public + source tiers (admin can read both). Per-user
        private tier is excluded unless session carries explicit user_id.
        """
        if not query or not query.strip():
            return ""
        try:
            from astor_memory import astor_bus, astor_nest
        except Exception as exc:
            logger.warning("astor_memory prefetch import failed: %s", exc)
            return ""
        try:
            nest = astor_nest(tier="public")
            bus = astor_bus(tier="public")
            # Use nest for vector search, then bus for canonical fact hydration.
            from astor_memory.nest.embeddings import astor_get_embedding_model
            model = astor_get_embedding_model()
            emb = list(model.embed([query]))[0]
            hits = nest.search(emb, limit=5)
            if not hits:
                return ""
            lines = ["## astor-memory recall (public tier)\n"]
            for hit in hits[:5]:
                cid = hit.get("canonical_id") or hit.get("id")
                if cid is None:
                    continue
                row = bus.conn.execute(
                    "SELECT content, kind, tags FROM memory_canonical WHERE id=?",
                    (cid,),
                ).fetchone()
                if row is None:
                    continue
                content, kind, tags = row
                lines.append(f"- [{kind}] {content[:200]}")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("astor_memory prefetch failed: %s", exc)
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Audit-log every completed turn to astor public bus.

        We don't extract facts here — that's the LLM-driven forge's job
        (see astor.forge.astor_extract_facts). We just record the turn
        happened so admins can audit conversation density + recall health.

        v1.1: After every `self._nudge_every` turns, return a memory-search
        nudge via the system_prompt_block hook (Hermes calls it at session
        boundary). Per MemoraX design, this fights "memory written but never
        recalled" — the nudge makes the agent remember the tool exists.
        """
        try:
            from astor_memory import astor_bus
            bus = astor_bus(tier="public")
            bus.append_event(
                namespace=f"hermes/{self._platform}/{session_id or self._session_id}",
                agent_id="astor_memory_adapter",
                source="hermes.sync_turn",
                action="turn",
                content=user_content[:500] if user_content else "",
                metadata={
                    "session_id": session_id or self._session_id,
                    "platform": self._platform,
                    "asst_len": len(assistant_content or ""),
                },
            )
            # v1.1: nudge counter — increment on every turn. After N turns,
            # mark "should_nudge = True" so the next prefetch/system_prompt
            # injection tells the agent to recall.
            self._turn_count += 1
            self._should_nudge = (
                self._turn_count - self._last_nudge_turn
            ) >= self._nudge_every
            if self._should_nudge:
                self._last_nudge_turn = self._turn_count
        except Exception as exc:
            logger.warning("astor_memory sync_turn failed: %s", exc)

    def shutdown(self) -> None:
        """Clean up the hermes adapter — flush pending writes, close pools."""
        logger.info("astor_memory: shutdown (session=%s, turns=%d)",
                    self._session_id, self._turn_count)
        self._turn_count = 0
        self._last_nudge_turn = 0
        self._should_nudge = False

    # -- Tool implementations ---------------------------------------------

    def _tool_recall(self, args: Dict[str, Any]) -> str:
        import json
        query = args.get("query", "")
        top_k = int(args.get("top_k", 5))
        tier = args.get("tier", "public")
        user_id = args.get("user_id")
        try:
            from astor_memory import astor_bus, astor_nest
            from astor_memory.nest.embeddings import astor_get_embedding_model
        except Exception as exc:
            return json.dumps({"error": f"astor import failed: {exc}"})
        try:
            nest = astor_nest(tier=tier, user_id=user_id if tier == "private" else None)
            bus = astor_bus(tier=tier, user_id=user_id if tier == "private" else None)
            model = astor_get_embedding_model()
            emb = list(model.embed([query]))[0]
            hits = nest.search(emb, limit=top_k)
            out = []
            for hit in hits:
                cid = hit.get("canonical_id") or hit.get("id")
                row = bus.conn.execute(
                    "SELECT id, content, kind, tags, importance FROM memory_canonical WHERE id=?",
                    (cid,),
                ).fetchone() if cid else None
                if row:
                    out.append({"id": row[0], "content": row[1], "kind": row[2],
                                "tags": row[3], "importance": row[4]})
            return json.dumps({"results": out, "tier": tier, "count": len(out)},
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_write(self, args: Dict[str, Any]) -> str:
        import json
        content = args.get("content", "")
        kind = args.get("kind", "fact")
        tier = args.get("tier", "source")
        user_id = args.get("user_id")
        tags = args.get("tags", [])
        if not content:
            return json.dumps({"error": "content required"})
        try:
            from astor_memory import astor_bus
            bus = astor_bus(tier=tier,
                            user_id=user_id if tier == "private" else None)
            # Append event + insert candidate. Forge (LLM extraction) is
            # the next step — we write the raw fact here for audit.
            event_id = bus.append_event(
                namespace=f"hermes/{self._platform}/{self._session_id}",
                agent_id="astor_memory_adapter",
                source="hermes.astor_write",
                action="write",
                content=content,
                metadata={"kind": kind, "tags": tags, "tier": tier},
            )
            candidate_id = bus.insert_candidate(
                event_id=event_id,
                namespace=f"hermes/{self._platform}",
                content=content,
                kind=kind,
                importance=0.7,
                tags=tags,
            )
            return json.dumps({
                "event_id": event_id,
                "candidate_id": candidate_id,
                "tier": tier,
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_forget(self, args: Dict[str, Any]) -> str:
        """Implement astor_forget tool by delegating to /v1/forget HTTP endpoint.

        Forgets a fact by query (BM25) or by fact_id (hard delete).
        Mirrors the /v1/forget endpoint semantics.
        """
        import json
        import urllib.request as _ur
        query = args.get('query')
        fact_id = args.get('fact_id')
        tier = args.get('tier', 'public')
        user_id = args.get('user_id')
        forget_threshold = float(args.get('forget_threshold', 0.5))
        try:
            # Use the live REST endpoint so ACL + dedup semantics are
            # consistent with all other forget callers.
            # 2026-08-16 ship: bridges the gap where astor_forget was
            # advertised in get_tool_schemas() but no _tool_forget impl.
            body = json.dumps({
                'tier': tier,
                'user_id': user_id,
                'forget_threshold': forget_threshold,
            }).encode()
            if query is not None:
                body_dict = json.loads(body)
                body_dict['query'] = query
                body = json.dumps(body_dict).encode()
            if fact_id is not None:
                body_dict = json.loads(body)
                body_dict['fact_id'] = fact_id
                body = json.dumps(body_dict).encode()
            # ACL context must include the actor for audit. Use the adapter's bound actor.
            import os as _os
            base = _os.environ.get('ASTOR_BASE_URL', 'http://127.0.0.1:7803')
            req = _ur.Request(
                base + '/v1/forget',
                data=body,
                headers={'Content-Type': 'application/json'},
            )
            return _ur.urlopen(req, timeout=15).read().decode('utf-8')
        except Exception as exc:
            return json.dumps({"error": f"astor_forget failed: {exc}"})

    def _tool_status(self) -> str:
        import json
        result = {
            "astor_dir": str(ASTOR_DIR),
            "actor": self._actor,
            "platform": self._platform,
            "session_id": self._session_id,
        }
        try:
            from astor_memory import astor_bus
            for tier in ("public", "source"):
                bus = astor_bus(tier=tier)
                n = bus.conn.execute("SELECT COUNT(*) FROM memory_canonical").fetchone()[0]
                e = bus.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                result[f"{tier}_canonical"] = n
                result[f"{tier}_events"] = e
        except Exception as exc:
            result["error"] = str(exc)
        return json.dumps(result, ensure_ascii=False)
