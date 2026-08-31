"""astor_memory_client.py — framework-agnostic MemoryProvider implementation.

Drop-in client for any agent framework. Requires only numpy + stdlib.

Usage:
    from astor_memory_client import AstorMemoryProvider
    p = AstorMemoryProvider()
    if p.is_available():
        p.initialize(session_id="sess-123")
        facts = p.prefetch("user's last message")
        # inject facts into your framework's system prompt
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any


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

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._last_recall = ""

    def _http_post(self, path: str, body: dict, timeout: float = 0.0) -> dict:
        timeout = timeout or self._timeout_s
        req = urllib.request.Request(
            f"{self._server_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self._session_id
        with self._prefetch_lock:
            try:
                d = self._http_post("/v1/read", {"query": query, "user": self._user_id, "top_k": 20})
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
        try:
            if tool_name == "astor_recall":
                return {"results": self._http_post("/v1/read", {"query": args.get("query", ""), "user": self._user_id, "top_k": int(args.get("top_k", 10))})}
            if tool_name == "astor_write":
                return self._http_post("/v1/write", {"text": args.get("text", ""), "tier": args.get("tier", "public"), "user": args.get("user", self._user_id)})
            if tool_name == "astor_forget":
                return self._http_post("/v1/forget", {"fact_id": int(args.get("fact_id", 0))})
            return {"error": f"unknown tool: {tool_name}"}
        except Exception as e:
            return {"error": str(e)}

    def sync_turn(self, role: str, content: str) -> None:
        # Optional: write a turn-event to astor
        pass

    def get_tool_schemas(self) -> list[dict]:
        return [
            {"name": "astor_recall", "description": "Recall facts from astor memory", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "astor_write", "description": "Write a fact to astor memory", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "tier": {"type": "string", "enum": ["public", "private", "source"]}, "user": {"type": "string"}}, "required": ["text"]}},
            {"name": "astor_forget", "description": "Forget a fact by id", "parameters": {"type": "object", "properties": {"fact_id": {"type": "integer"}}, "required": ["fact_id"]}},
        ]

    def shutdown(self) -> None:
        self._last_recall = ""
        self._session_id = ""
