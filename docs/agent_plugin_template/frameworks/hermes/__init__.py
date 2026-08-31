"""
astor_memory plugin — MemoryProvider interface.

Astor Memory is a 3-tier × 3-store SQLite memory system with hybrid recall
(vector + BM25 + jaccard). This plugin wraps the astor_memory runtime REST
API (http://127.0.0.1:7803 by default) and exposes it through the
MemoryProvider ABC so Hermes Agent can use it as the active external
memory provider.

Configuration (lives in $HERMES_HOME/astor.json, set via `hermes memory setup`):
  server_url          — Astor server base URL (default http://127.0.0.1:7803)
  user_id             — Canonical user identifier for ACL scoping (default "admin")
  tier                — Default tier for read/write (default "public")
  top_k               — Default recall top_k (default 20)
  prefetch_timeout_s  — Hot-path recall timeout (default 8s)

Environment variable overrides:
  ASTOR_SERVER_URL    — Overrides server_url
  ASTOR_USER_ID       — Overrides user_id

v1.11.0 (2026-08-31, follow astor_memory runtime __version__): plugin.yaml bumped in sync; this plugin
remains API-compatible with the v1.0.0 initial ship. The 1.11.0 label on the plugin is the
compatibility target — it must match <runtime_dir>astor_memory/__init__.py:__version__.
Plugin internal logic is still the v1.0.0 initial-ship surface (is_available, initialize, prefetch,
system_prompt_block, handle_tool_call, sync_turn, get_tool_schemas, shutdown). Bump only when
on_pre_compress schema or MemoryProvider ABC changes.
  - is_available() — checks /v1/health, returns True iff facts >= 1
  - initialize() — sets active_tier + user_id, primes _prefetch_lock
  - prefetch() — /v1/read hybrid recall, returns formatted text block
  - system_prompt_block() — high-priority marker + recall summary
  - handle_tool_call() — exposes astor_recall / astor_write / astor_forget
  - sync_turn() — write system_event audit row to bus
  - get_tool_schemas() — return 3 tool schemas (recall/write/forget)
  - shutdown() — flush in-flight prefetch, close HTTP pool

Pre-condition: astor_memory server must be running. If server is down,
is_available() returns False and agent init logs the unavailable reason.
The hermes-auto-capture-pipeline hooks (on_session_end / on_session_reset /
post_tool_call) write to the same server, so the plugin + hooks share one
upstream.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "http://127.0.0.1:7803"
_DEFAULT_TIMEOUT_S = 8.0
_DEFAULT_BOT_BINDING_DB = r"<runtime_dir>bot-binding.db"


def _load_config(hermes_home: Optional[str] = None) -> dict:
    """Load astor plugin config from $HERMES_HOME/astor.json."""
    home = hermes_home or os.environ.get("HERMES_HOME") or str(
        Path.home() / ".hermes"
    )
    cfg_path = Path(home) / "astor.json"
    cfg = {
        "server_url": os.environ.get("ASTOR_SERVER_URL", _DEFAULT_SERVER_URL),
        "user_id": os.environ.get("ASTOR_USER_ID", "admin"),
        "tier": "public",
        "top_k": 20,
        "prefetch_timeout_s": _DEFAULT_TIMEOUT_S,
        "bot_binding_db": os.environ.get(
            "ASTOR_BOT_BINDING_DB", _DEFAULT_BOT_BINDING_DB
        ),
    }
    if cfg_path.exists():
        try:
            disk = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in disk.items() if v is not None})
        except Exception as e:
            logger.debug("astor.json parse failed (%s); using defaults", e)
    # Re-resolve env overrides so they always win
    if "ASTOR_SERVER_URL" in os.environ:
        cfg["server_url"] = os.environ["ASTOR_SERVER_URL"]
    if "ASTOR_USER_ID" in os.environ:
        cfg["user_id"] = os.environ["ASTOR_USER_ID"]
    return cfg


def _http_post_json(url: str, body: dict, timeout_s: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_read_with_fallback(url: str, body: dict, timeout_s: float) -> dict:
    """POST a /v1/read body; on 403 acl_init_failed, retry once at tier=public.

    Read-only fallback so a misconfigured tier (e.g. private without a valid
    user grant) degrades to public recall instead of breaking recall entirely.
    NEVER use for writes — a private fact must never silently land in public.
    """
    try:
        return _http_post_json(url, body, timeout_s)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if e.code == 403 and "acl_init_failed" in detail and body.get("tier") != "public":
            logger.warning(
                "astor read 403 acl_init_failed at tier=%s; retrying tier=public",
                body.get("tier"),
            )
            downgraded = dict(body, tier="public")
            downgraded.pop("user_id", None)
            return _http_post_json(url, downgraded, timeout_s)
        raise


def _http_get_json(url: str, timeout_s: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


class AstorMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._config = _load_config()
        self._server_url = self._config["server_url"].rstrip("/")
        self._user_id = self._config["user_id"]
        self._tier = self._config["tier"]
        self._top_k = int(self._config["top_k"])
        self._prefetch_timeout_s = float(self._config["prefetch_timeout_s"])
        self._session_id = ""
        # Per-session identity resolution (2026-08-29): gateway threads the
        # chatting user's platform identity via initialize(**kwargs). Without
        # this, every session would share the astor.json default user and
        # non-admin users would read/write the admin's private DB.
        self._identity_resolved = False
        self._init_error: Optional[str] = None
        self._prefetch_lock = threading.Lock()
        self._cached_prefetch: Optional[str] = None
        self._cached_prefetch_query: Optional[str] = None
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "astor_memory"

    def is_available(self) -> bool:
        try:
            h = _http_get_json(
                f"{self._server_url}/v1/health", timeout_s=3.0
            )
            ok = (
                h.get("status") == "ok"
                and int(h.get("facts", 0)) >= 1
            )
            if ok:
                self._init_error = None
            else:
                self._init_error = (
                    f"astor /v1/health not OK: {h.get('status')}, "
                    f"facts={h.get('facts')}"
                )
            return ok
        except Exception as e:
            self._init_error = (
                f"astor /v1/health unreachable at {self._server_url}: {e}"
            )
            return False

    def unavailable_reason(self) -> str:
        return self._init_error or "astor_memory not initialized"

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._resolve_session_user(kwargs)
        if not self.is_available():
            logger.warning(
                "astor_memory initialize: %s", self._init_error
            )
            return
        logger.info(
            "astor_memory initialized: server=%s user=%s tier=%s top_k=%d resolved=%s",
            self._server_url, self._user_id, self._tier, self._top_k,
            self._identity_resolved,
        )

    def _resolve_session_user(self, kwargs: Dict[str, Any]) -> None:
        """Map the chatting user's platform identity to a canonical astor user.

        Gateway threads platform/user_id/chat_id into initialize(). We resolve
        the canonical user via bot-binding.db `bindings` (platform_id prefix +
        chat_id). Fail-closed: on messaging platforms an unresolvable sender
        is forced to tier=public so they can never touch the astor.json
        default user's (admin's) private DB. CLI/local sessions keep the
        astor.json identity (this host's admin).
        """
        platform = str(kwargs.get("platform") or "")
        raw_uid = str(kwargs.get("user_id") or "")
        chat_id = str(kwargs.get("chat_id") or "")
        if platform in ("", "cli", "local"):
            return
        sender = raw_uid
        pfx = f"{platform}_"
        if sender.startswith(pfx):
            sender = sender[len(pfx):]
        candidates = [c for c in {sender, chat_id} if c]
        if not candidates:
            self._force_public(f"no user identity in kwargs (platform={platform})")
            return
        db_path = self._config.get("bot_binding_db") or _DEFAULT_BOT_BINDING_DB
        row = None
        try:
            import sqlite3

            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                ph = ",".join("?" * len(candidates))
                row = con.execute(
                    "SELECT user_id FROM bindings WHERE active=1 "
                    f"AND platform_id LIKE ? AND chat_id IN ({ph}) LIMIT 1",
                    [f"{platform}:%", *candidates],
                ).fetchone()
            finally:
                con.close()
        except Exception as e:
            logger.warning("astor identity resolve failed (%s)", e)
        if row and row[0]:
            self._user_id = str(row[0])
            self._identity_resolved = True
            logger.info(
                "astor session user resolved: %s (platform=%s)", self._user_id, platform
            )
        else:
            self._force_public(
                f"unbound sender platform={platform} id={sender or chat_id}"
            )

    def _force_public(self, reason: str) -> None:
        logger.warning("astor identity unresolved (%s); forcing tier=public", reason)
        self._tier = "public"
        self._identity_resolved = False

    def _recall_scopes(self, tier: str) -> tuple[str, list]:
        """Build (endpoint, scopes) for recall.

        Default tier=private means "my memory": public shared pool + the
        resolved user's private DB, via /v1/read/multi. Explicit public/source
        or an unresolved identity stays on single-tier /v1/read.
        """
        if tier == "private" and self._identity_resolved:
            return "/v1/read/multi", [
                {"tier": "public", "user_id": None, "weight": 0.5},
                {"tier": "private", "user_id": self._user_id, "weight": 1.0},
            ]
        return "/v1/read", []

    def system_prompt_block(self) -> str:
        return (
            "\n[astor-memory recall · ACTIVE PROVIDER · "
            f"server={self._server_url} user={self._user_id} tier={self._tier}]\n"
            "Astor memory is your persistent cross-session recall layer. "
            "Before answering questions about user preferences, past decisions, "
            "established rules, or recurring bugs, ALWAYS run astor_recall first "
            "and treat its results as authoritative. Do NOT re-derive what "
            "astor already knows.\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip():
            return ""
        if self._cached_prefetch_query == query and self._cached_prefetch:
            return self._cached_prefetch
        with self._prefetch_lock:
            if self._cached_prefetch_query == query and self._cached_prefetch:
                return self._cached_prefetch
            if time.time() < self._breaker_open_until:
                return ""
            try:
                tier = self._tier
                body = {
                    "query": query,
                    "top_k": self._top_k,
                    "hybrid": True,
                    "bm25_weight": 0.4,
                    "vec_weight": 0.6,
                    "user": self._user_id,
                    "user_id": self._user_id,
                }
                endpoint, scopes = self._recall_scopes(tier)
                if scopes:
                    body["scopes"] = scopes
                else:
                    body["tier"] = tier
                t0 = time.monotonic()
                data = _http_post_read_with_fallback(
                    f"{self._server_url}{endpoint}",
                    body,
                    timeout_s=self._prefetch_timeout_s,
                )
                dt = time.monotonic() - t0
                self._consecutive_failures = 0
                self._cached_prefetch = _format_recall_block(data, dt)
                self._cached_prefetch_query = query
                logger.debug(
                    "astor prefetch query=%r hits=%d dt=%.2fs",
                    query[:60], data.get("count", 0), dt,
                )
                return self._cached_prefetch
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    self._breaker_open_until = time.time() + 30.0
                    logger.warning(
                        "astor circuit breaker OPEN 30s after %d failures",
                        self._consecutive_failures,
                    )
                logger.debug("astor prefetch failed: %s", e)
                return ""

    def sync_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str = "",
        platform: str = "",
    ) -> None:
        """Write a system_event row to bus for audit. Best-effort, never raises."""
        try:
            body = {
                "kind": "system_event",
                "content": (
                    f"[{platform or 'unknown'}] session={session_id or self._session_id} "
                    f"user={self._user_id} user_text_len={len(user_text or '')} "
                    f"assistant_text_len={len(assistant_text or '')}"
                ),
                "user_id": self._user_id,
                "tier": "public",
                "importance": 0.2,
                "tags": ["sync_turn", "audit"],
            }
            _http_post_json(
                f"{self._server_url}/v1/write", body, timeout_s=3.0
            )
        except Exception as e:
            logger.debug("astor sync_turn audit write failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "astor_recall",
                "description": (
                    "Query astor memory (cross-session, hybrid vector+BM25). "
                    "Returns top-k facts ranked by similarity + importance. "
                    "USE BEFORE answering questions about user preferences, "
                    "past decisions, established rules, or recurring bugs."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 20},
                        "tier": {
                            "type": "string",
                            "enum": ["public", "source", "private"],
                            "default": "public",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "astor_write",
                "description": (
                    "Write a fact to astor memory. Use for important rules, "
                    "decisions, preferences, or lessons learned. The "
                    "capture_intent hook does this automatically when user "
                    "says '记住 / remember / 切记'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "fact", "rule", "decision",
                                "user_preference", "lesson",
                            ],
                            "default": "fact",
                        },
                        "importance": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "default": 0.7,
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["public", "source", "private"],
                            "default": "public",
                        },
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "astor_forget",
                "description": (
                    "Tombstone a fact by fact_id (soft delete — keeps audit "
                    "trail). To un-tombstone, use direct SQL on the bus DB."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fact_id": {"type": "integer"},
                    },
                    "required": ["fact_id"],
                },
            },
        ]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        try:
            if tool_name == "astor_recall":
                tier = args.get("tier", self._tier)
                body = {
                    "query": args.get("query", ""),
                    "top_k": int(args.get("top_k", self._top_k)),
                    "hybrid": True,
                    "user": self._user_id,
                    "user_id": self._user_id,
                }
                endpoint, scopes = self._recall_scopes(tier)
                if scopes:
                    body["scopes"] = scopes
                else:
                    body["tier"] = tier
                data = _http_post_read_with_fallback(
                    f"{self._server_url}{endpoint}", body, timeout_s=5.0
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            elif tool_name == "astor_write":
                body = {
                    "text": args.get("text", ""),
                    "kind": args.get("kind", "fact"),
                    "importance": float(args.get("importance", 0.7)),
                    "tier": args.get("tier", self._tier),
                    "user": self._user_id,
                    "user_id": self._user_id,
                }
                data = _http_post_json(
                    f"{self._server_url}/v1/write", body, timeout_s=5.0
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            elif tool_name == "astor_forget":
                fid = args.get("fact_id")
                data = _http_post_json(
                    f"{self._server_url}/v1/forget",
                    {"fact_id": int(fid)},
                    timeout_s=5.0,
                )
                return json.dumps(data, ensure_ascii=False, indent=2)
            else:
                return json.dumps(
                    {"error": f"unknown astor tool: {tool_name}"}
                )
        except Exception as e:
            return json.dumps({"error": f"astor tool {tool_name} failed: {e}"})

    def shutdown(self) -> None:
        self._cached_prefetch = None
        self._cached_prefetch_query = None
        logger.info("astor_memory shutdown")

    def save_config(self, values: dict, hermes_home: str) -> None:
        cfg_path = Path(hermes_home) / "astor.json"
        existing: dict = {}
        if cfg_path.exists():
            try:
                existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(values)
        from utils import atomic_json_write
        atomic_json_write(cfg_path, existing, mode=0o600)
        self._config = _load_config(hermes_home)
        self._server_url = self._config["server_url"].rstrip("/")
        self._user_id = self._config["user_id"]
        self._tier = self._config["tier"]
        self._top_k = int(self._config["top_k"])

    def get_config_schema(self):
        return [
            {
                "key": "server_url",
                "description": "Astor server base URL",
                "required": False,
                "env_var": "ASTOR_SERVER_URL",
                "default": _DEFAULT_SERVER_URL,
            },
            {
                "key": "user_id",
                "description": "Canonical user identifier for ACL scoping",
                "required": False,
                "env_var": "ASTOR_USER_ID",
                "default": "admin",
            },
            {
                "key": "tier",
                "description": "Default tier for read/write",
                "required": False,
                "default": "public",
                "choices": ["public", "source", "private"],
            },
            {
                "key": "top_k",
                "description": "Default recall top_k",
                "required": False,
                "default": 20,
            },
        ]


def _format_recall_block(data: dict, dt_s: float) -> str:
    """Format /v1/read JSON response as a system-prompt-injectable text block."""
    results = data.get("results", []) or []
    if not results:
        return ""
    lines = [
        f"[astor-memory recall · {len(results)} hits · {dt_s:.2f}s]",
    ]
    for r in results[:10]:
        fid = r.get("fact_id", "?")
        sim = r.get("similarity", 0.0)
        imp = r.get("importance", 0.0)
        content = (r.get("content") or "").replace("\n", " ")[:200]
        lines.append(f"- (id={fid} sim={sim:.2f} imp={imp:.2f}) {content}")
    if len(results) > 10:
        lines.append(f"... +{len(results) - 10} more")
    return "\n".join(lines) + "\n"
