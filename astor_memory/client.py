"""astor_memory.client — agent-agnostic Python client for astor-memory REST API.

This is the public SDK for the astor-memory service. Works with ANY AI
agent (Codex, Claude Code, custom scripts, etc.) — not bound to hermes-agent.

Install:
    pip install astor-memory

Quickstart:
    >>> from astor_memory.client import AstorClient
    >>> client = AstorClient(base_url="http://127.0.0.1:7803", user_id="myagent")
    >>> facts = client.read(query="user prefers Chinese replies", top_k=5)
    >>> for f in facts:
    ...     print(f.id, f.content[:100])
    >>> fact_id = client.write(
    ...     content="Cross-cutting observation learned this session",
    ...     kind="lesson",
    ...     importance=0.8,
    ... )

API endpoints (v1):
    POST /v1/read              Hybrid vector + BM25 search
    POST /v1/write             Add new fact (auto-routes to canonical tier)
    POST /v1/forget            Soft-delete (tombstone) a fact
    POST /v1/health            Server liveness + tier counts
    GET  /v1/fact/<id>/...     Provenance, lineage, restore
    POST /v1/grant[/list/...]  ACL administration
    POST /v1/merge/find        Find duplicate candidates
    POST /v1/merge/apply       Apply merge

Reference:
    - https://github.com/<repo-owner>/Astor-Memory
    - Inspired by Memmy's cross-agent memory pattern (Sept 2026)

License: MIT
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


# --- Data classes ---

@dataclass
class Fact:
    id: int
    content: str
    kind: str
    namespace: str
    confidence: float
    importance: float
    score: float | None = None  # hybrid score from /read (vector + BM25)
    metadata: dict[str, Any] = field(default_factory=dict)
    promoted_at: str | None = None
    tags: list[str] = field(default_factory=list)
    parent_fact_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Fact":
        return cls(
            id=d.get("id") or d.get("fact_id") or 0,
            content=d.get("content", ""),
            kind=d.get("kind", "fact"),
            namespace=d.get("namespace", ""),
            confidence=float(d.get("confidence") or 0),
            importance=float(d.get("importance") or 0),
            score=d.get("score"),
            metadata=d.get("metadata") or {},
            promoted_at=d.get("promoted_at"),
            tags=d.get("tags") or [],
            parent_fact_ids=d.get("parent_fact_ids") or [],
        )


@dataclass
class ServerHealth:
    version: str
    astor_dir: str
    tier_counts: dict[str, int]
    embedding_count: int
    uptime_seconds: int | None = None


# --- Client ---

class AstorError(Exception):
    """Raised when the astor-memory server returns an error response."""


class AstorClient:
    """Agent-agnostic astor-memory REST client.

    Use this from any Python-based AI agent to persist and recall
    cross-session memories without binding to a specific runtime.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7803",
        user_id: str = "anonymous",
        tier: Literal["public", "source", "private"] = "private",
        timeout: float = 30.0,
        api_key: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.tier = tier
        self.timeout = timeout
        self.api_key = api_key

    # --- HTTP layer ---

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-Astor-Key"] = self.api_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_body = {"detail": str(e)}
            raise AstorError(f"HTTP {e.code}: {err_body.get('detail') or err_body}") from e
        except urllib.error.URLError as e:
            raise AstorError(f"connection failed: {e}") from e

    # --- Endpoints ---

    def health(self) -> ServerHealth:
        """GET /v1/health — server liveness, tier counts, version."""
        d = self._request("GET", "/v1/health")
        return ServerHealth(
            version=d.get("version", "unknown"),
            astor_dir=d.get("astor_dir", ""),
            tier_counts=d.get("tier_counts") or d.get("counts") or {},
            embedding_count=d.get("embedding_count") or d.get("embeddings") or 0,
        )

    def read(
        self,
        query: str,
        top_k: int = 10,
        tier: Literal["public", "source", "private"] | None = None,
        kind: str | None = None,
        min_importance: float | None = None,
    ) -> list[Fact]:
        """POST /v1/read — hybrid vector + BM25 search.

        Returns top_k facts ranked by similarity + importance.
        """
        body = {
            "query": query,
            "top_k": top_k,
            "tier": tier or self.tier,
            "user_id": self.user_id,
        }
        if kind:
            body["kind"] = kind
        if min_importance is not None:
            body["min_importance"] = min_importance

        d = self._request("POST", "/v1/read", json_body=body)
        facts_raw = d.get("facts") or d.get("results") or d.get("items") or []
        return [Fact.from_api(f) for f in facts_raw]

    def write(
        self,
        content: str,
        kind: str = "fact",
        importance: float = 0.5,
        confidence: float = 0.8,
        namespace: str | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        tier: Literal["public", "source", "private"] | None = None,
    ) -> int:
        """POST /v1/write — add new fact.

        Returns the fact_id assigned by the server.
        """
        body = {
            "content": content,
            "kind": kind,
            "importance": importance,
            "confidence": confidence,
            "namespace": namespace or self.user_id,
            "user_id": self.user_id,
            "tier": tier or self.tier,
        }
        if tags:
            body["tags"] = list(tags)
        if metadata:
            body["metadata"] = metadata
        d = self._request("POST", "/v1/write", json_body=body)
        return d.get("fact_id") or d.get("id") or 0

    def forget(self, fact_id: int, reason: str = "tombstoned via client") -> bool:
        """POST /v1/forget — soft-delete (tombstone) a fact."""
        body = {"fact_id": fact_id, "reason": reason, "user_id": self.user_id}
        d = self._request("POST", "/v1/forget", json_body=body)
        return d.get("ret") == 0 or d.get("ok") is True

    def provenance(self, fact_id: int) -> dict[str, Any]:
        """GET /v1/fact/<id>/provenance — full audit trail."""
        return self._request("GET", f"/v1/fact/{fact_id}/provenance")

    def lineage(self, fact_id: int) -> dict[str, Any]:
        """GET /v1/fact/<id>/lineage — parent → child relationships."""
        return self._request("GET", f"/v1/fact/{fact_id}/lineage")

    def restore(self, fact_id: int) -> bool:
        """POST /v1/fact/<id>/restore — un-tombstone a soft-deleted fact."""
        d = self._request("POST", f"/v1/fact/{fact_id}/restore")
        return d.get("ret") == 0 or d.get("ok") is True


# --- Convenience: batch helpers ---

def batch_read(
    client: AstorClient,
    queries: list[str],
    top_k_per_query: int = 5,
) -> dict[str, list[Fact]]:
    """Run multiple queries and return a {query: [facts]} dict.

    Useful for "what does the agent know about X?" sweeps.
    """
    out: dict[str, list[Fact]] = {}
    for q in queries:
        out[q] = client.read(q, top_k=top_k_per_query)
    return out


__all__ = [
    "AstorClient",
    "AstorError",
    "Fact",
    "ServerHealth",
    "batch_read",
]
