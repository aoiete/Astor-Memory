# Astor Agent Plugin Template

Use astor as a **memory provider** in **any** agent framework.

## What astor is

Astor is a standalone REST memory server (default `http://127.0.0.1:7803`).
This template gives your agent framework a **client-side plugin** that talks
to astor over HTTP. You do NOT need to install astor's full server-side deps
in your agent's venv — the plugin is HTTP-only.

## Install

```bash
# 1. Start astor server somewhere (separate venv or system Python)
pip install astor-memory[server]
python -m astor_memory.server  # listens on :7803

# 2. In your agent framework's venv, install minimal deps
pip install numpy  # the only 3rd-party dep the plugin needs

# 3. Copy this template into your framework's plugin path
#    (wherever your framework loads 3rd-party code)

# 4. Configure your framework to enable "astor_memory" as the memory provider
```

## MemoryProvider ABC (framework-agnostic)

Any agent framework that supports astor should expose this ABC. Your framework
binds the abstract methods to its own lifecycle (init, prompt assembly, etc.).

```python
from abc import ABC, abstractmethod
from typing import Optional


class MemoryProvider(ABC):
    """Memory provider interface that astor implements.

    Frameworks should call these methods at well-defined points in the agent
    lifecycle to integrate astor as the active external memory store.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider name used in config + logs (e.g. 'astor_memory')."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the provider is reachable + has at least one fact.

        Return False (and populate `unavailable_reason`) when:
        - server unreachable
        - server returned an error
        - server empty (zero facts)
        """

    @abstractmethod
    def unavailable_reason(self) -> str:
        """Human-readable reason when is_available() returns False."""

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Called once per session start. Reset any per-session caches."""

    @abstractmethod
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall top-k facts for a query.

        Returns a formatted text block ready to be inserted into the
        system prompt. Should be fast (< 8s) and return empty string on error.
        """

    @abstractmethod
    def system_prompt_block(self) -> str:
        """Return the high-priority marker + recall summary for system prompt."""

    @abstractmethod
    def handle_tool_call(self, tool_name: str, args: dict) -> dict:
        """Expose astor operations as agent tools (recall / write / forget)."""

    @abstractmethod
    def sync_turn(self, role: str, content: str) -> None:
        """Optional: log each turn to astor as an audit event."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict]:
        """Return tool schemas (OpenAI function-calling format) for astor_recall / astor_write / astor_forget."""

    @abstractmethod
    def shutdown(self) -> None:
        """Called at session end. Flush in-flight prefetch, close HTTP pool."""
```

## Minimal plugin implementation (HTTP-only)

Drop this in your framework's plugin path. It only requires `numpy` + stdlib
(`urllib`, `json`, `threading`).

```python
"""astor_memory plugin — MemoryProvider implementation over HTTP.

Plug into any agent framework that supports the MemoryProvider ABC.
Config (env vars or framework config):
  ASTOR_SERVER_URL  — default http://127.0.0.1:7803
  ASTOR_USER_ID     — default 'admin'
  ASTOR_TIMEOUT_S   — default 8
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional


class AstorMemoryProvider:
    name = "astor_memory"

    def __init__(self) -> None:
        self._server_url = os.environ.get("ASTOR_SERVER_URL", "http://127.0.0.1:7803").rstrip("/")
        self._user_id = os.environ.get("ASTOR_USER_ID", "admin")
        self._timeout_s = float(os.environ.get("ASTOR_TIMEOUT_S", "8"))
        self._session_id: str = ""
        self._prefetch_lock = threading.Lock()
        self._last_recall: str = ""

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self._server_url}/v1/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read())
                return d.get("status") == "ok" and d.get("facts", 0) > 0
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def unavailable_reason(self) -> str:
        return f"astor_memory unreachable at {self._server_url}"

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._last_recall = ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self._session_id
        with self._prefetch_lock:
            try:
                body = json.dumps({"query": query, "user": self._user_id, "top_k": 20}).encode()
                req = urllib.request.Request(
                    f"{self._server_url}/v1/read",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self._timeout_s) as r:
                    d = json.loads(r.read())
                results = d.get("results", [])
                lines = [f"[astor-memory recall · {len(results)} hits · 0.0s]"]
                for x in results[:10]:
                    sim = x.get("similarity", 0)
                    txt = x.get("content", "")[:80]
                    lines.append(f"- (id={x.get('fact_id')} sim={sim:.2f}) {txt}")
                self._last_recall = "\n".join(lines)
                return self._last_recall
            except Exception as e:
                return f"[astor-memory recall failed: {e}]"

    def system_prompt_block(self) -> str:
        return self._last_recall or "[astor-memory: no active recall]"

    def handle_tool_call(self, tool_name: str, args: dict) -> dict:
        # astor_recall / astor_write / astor_forget
        ...

    def sync_turn(self, role: str, content: str) -> None:
        # Optional: write audit event
        pass

    def get_tool_schemas(self) -> list[dict]:
        return [
            {"name": "astor_recall", "description": "Recall facts from astor memory", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "astor_write", "description": "Write a fact to astor memory", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "tier": {"type": "string", "enum": ["public", "private", "source"]}, "user": {"type": "string"}}, "required": ["text"]}},
            {"name": "astor_forget", "description": "Forget a fact by id", "parameters": {"type": "object", "properties": {"fact_id": {"type": "integer"}}, "required": ["fact_id"]}},
        ]

    def shutdown(self) -> None:
        self._last_recall = ""
        self._session_id = ""
```

## Framework integration

For each framework, you write a thin adapter that maps the framework's memory
hooks onto the `MemoryProvider` ABC. Example: hermes has it at
`hermes-agent/agent/memory_provider.py` — your framework will have its own.

Typical integration points:

| Framework event | Call |
|---|---|
| Session start | `provider.initialize(session_id=...)` |
| Before LLM call | `prefetch(query)` → inject into system prompt as `system_prompt_block()` |
| LLM tool call (astor_*) | `handle_tool_call(tool_name, args)` |
| Session end | `provider.shutdown()` |
| Periodic health | `is_available()` — if False, fall back to .env USER.md |

## Required server endpoints

The plugin expects these endpoints on `ASTOR_SERVER_URL`:
- `GET /v1/health` — `{"status": "ok", "facts": N, "version": "x.y.z"}`
- `POST /v1/read` — body `{"query": str, "user": str, "top_k": int}` → `{"results": [{fact_id, content, similarity, ...}]}`
- `POST /v1/write` — body `{"text": str, "tier": str, "user": str}` → `{"fact_ids": [...]}`
- `POST /v1/forget` — body `{"fact_id": int}` → `{"forgotten": [...]}`
- `GET /v1/audit/health` — 3-dim memory-native audit (Bannings 2026-08)

## Versioning

Plugin version must equal server version. Bump together. See
`RULES_astor_version_sync.md` for the locked rule.

## License

MIT (matches astor-memory)
