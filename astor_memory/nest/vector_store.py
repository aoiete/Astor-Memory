"""
Nest: vector store backed by its own SQLite DB (~/.astor/astor_nest.db).

v1.0 design (per user lock 2026-08-15): 3 separate DBs, one per store
- astor_bus.db — events + memory_candidates + memory_canonical + audit_log
- astor_forge.db — LLM extraction cache
- astor_nest.db — embeddings table (1 row per fact, model_name indexed)

Embeddings are 768-dim float32 = 3 KB per fact. With 100K facts = 300 MB.

Features:
- Brute-force cosine similarity (1ms @ 5K docs; v1.1+ HNSW)
- L1 in-memory cache (LRU 100 MB)
- Version-based cache invalidation via model_name column
- Independent DB lock from bus (read-heavy, write-light)
"""

from __future__ import annotations

import sqlite3
import struct
import threading
import numpy as np
from pathlib import Path
from collections import OrderedDict
from typing import Any

from .embeddings import astor_get_embedding_model
from .schema import astor_init_nest_schema


def _pack_embedding(embedding: np.ndarray) -> bytes:
    """Pack float32 array as raw bytes for SQLite BLOB."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def _unpack_embedding(blob: bytes) -> np.ndarray:
    """Unpack raw bytes to float32 array."""
    n = len(blob) // 4
    return np.array(struct.unpack(f'{n}f', blob), dtype=np.float32)


class AstorNest:
    """Vector store for fact embeddings, backed by its own SQLite DB."""

    def __init__(self, db_path: Path | None = None, cache_size_mb: int = 100):
        from ..config import get_default_nest_path
        if db_path is None:
            db_path = get_default_nest_path()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Per Plan § Memory <-> concurrency: WAL + foreign_keys + busy_timeout
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode = WAL')
        self._conn.execute('PRAGMA synchronous = NORMAL')
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.execute('PRAGMA busy_timeout = 5000')
        astor_init_nest_schema(self._conn)

        self._cache_size = cache_size_mb * 1024 * 1024  # bytes
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the SQLite connection for vector store (embeddings table)."""
        return self._conn

    def close(self) -> None:
        """Close the vector store connection (CLI teardown)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _embed(self, text: str) -> np.ndarray:
        """Compute embedding (calls lazy-loaded model)."""
        model = astor_get_embedding_model()
        embeddings = list(model.embed([text]))
        return np.array(embeddings[0], dtype=np.float32)

    def _cache_key(self, fact_id: int, model_name: str) -> str:
        return f'{fact_id}:{model_name}'

    def get(self, fact_id: int, model_name: str | None = None) -> np.ndarray | None:
        """Get embedding from cache or DB."""
        if model_name is None:
            from .embeddings import astor_get_model_name_for_ram
            model_name = astor_get_model_name_for_ram()
        key = self._cache_key(fact_id, model_name)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)  # LRU touch
                return self._cache[key]

        # Cache miss: load from nest DB
        row = self._conn.execute(
            "SELECT embedding FROM embeddings WHERE fact_id = ? AND model_name = ?",
            (fact_id, model_name),
        ).fetchone()
        if row is None or row[0] is None:
            return None

        emb = _unpack_embedding(row[0])
        self._put(fact_id, model_name, emb)
        return emb

    def _put(self, fact_id: int, model_name: str, embedding: np.ndarray):
        """Put in cache (LRU eviction)."""
        key = self._cache_key(fact_id, model_name)
        size = embedding.nbytes
        with self._cache_lock:
            while self._cache_size_used() + size > self._cache_size and self._cache:
                self._cache.popitem(last=False)  # Evict LRU
            self._cache[key] = embedding

    def store(self, fact_id: int, text: str, model_name: str | None = None) -> np.ndarray:
        """Compute embedding for text and persist to nest DB (embeddings table).

        Called by bus.promote_candidate() at write-time per Plan § Write-time dedup.
        Returns the embedding for callers that need it.
        """
        if model_name is None:
            from .embeddings import astor_get_model_name_for_ram
            model_name = astor_get_model_name_for_ram()
        emb = self._embed(text)
        self._conn.execute(
            """INSERT OR REPLACE INTO embeddings (fact_id, embedding, model_name, dim, updated_at)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (fact_id, _pack_embedding(emb), model_name, emb.shape[0]),
        )
        self._put(fact_id, model_name, emb)
        return emb

    def _cache_size_used(self) -> int:
        return sum(e.nbytes for e in self._cache.values())

    def invalidate_fact(self, fact_id: int):
        """Invalidate all cached embeddings for a fact."""
        with self._cache_lock:
            keys_to_remove = [k for k in self._cache if k.startswith(f'{fact_id}:')]
            for k in keys_to_remove:
                del self._cache[k]
        self._conn.execute("DELETE FROM embeddings WHERE fact_id = ?", (fact_id,))

    def search(
        self,
        query_embedding: np.ndarray,
        limit: int = 5,
        model_name: str | None = None,
    ) -> list[tuple[int, float]]:
        """Brute-force cosine similarity search.

        Returns list of (fact_id, similarity) sorted by similarity desc.
        User/namespace/tier/since filters apply via JOIN to bus.memory_canonical
        (Plan § 3-store: nest holds embeddings, bus holds metadata).
        """
        if model_name is None:
            from .embeddings import astor_get_model_name_for_ram
            model_name = astor_get_model_name_for_ram()

        rows = self._conn.execute(
            "SELECT fact_id, embedding FROM embeddings WHERE model_name = ?",
            (model_name,),
        ).fetchall()

        results = []
        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0:
            return []
        for fact_id, emb_blob in rows:
            emb = _unpack_embedding(emb_blob)
            emb_norm = np.linalg.norm(emb)
            if emb_norm == 0:
                continue
            sim = float(np.dot(query_embedding, emb) / (query_norm * emb_norm))
            results.append((fact_id, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


_nest_singleton: AstorNest | None = None
_nest_lock = threading.Lock()


def astor_nest(
    db_path: Path | None = None,
    tier: str | None = None,
    user_id: str | None = None,
) -> AstorNest:
    """
    Get or create a nest (vector store) handle.

    2026-08-15 ship: backward-compat REMOVED. Same rationale as astor_bus():
    the legacy single-file fallback silently regenerated ASTOR_DIR/astor_nest.db
    bypassing 3-tier × 3-store ACL. tier is now REQUIRED.

    Args:
        db_path:  override sqlite file path (testing only)
        tier:     'public' / 'source' / 'private' — REQUIRED
        user_id:  required when tier='private'
    """
    from .._internal.acl_layout import get_db_path as _gdp
    from .._internal.acl import astor_check_read, astor_check_write, PermissionError_

    if tier is None:
        raise ValueError(
            "astor_nest() requires tier='public'|'source'|'private'. "
            "The legacy single-file fallback was removed 2026-08-15."
        )
    # 2026-08-16 strict-privacy ship: opening nest requires READ access.
    # A read-grant covers read; a write-grant covers both. write-grant is
    # checked at write time, not at connection time.
    astor_check_read(tier, user_id)
    if tier == "private":
        try:
            astor_check_write(tier, user_id)
        except PermissionError_:
            pass  # read-grant holders may open, error at write time
    target = db_path if db_path is not None else _gdp(tier, "nest", user_id)
    return AstorNest(target)


def astor_nest_for(tier: str, user_id: str | None = None) -> AstorNest:
    """9-db layout nest accessor. Same semantics as astor_nest(tier, user_id)."""
    return astor_nest(tier=tier, user_id=user_id)


def astor_reset_nest() -> None:
    """Reset the singleton (for testing)."""
    global _nest_singleton
    with _nest_lock:
        _nest_singleton = None


__all__ = ["AstorNest", "astor_nest", "astor_nest_for", "astor_reset_nest"]
