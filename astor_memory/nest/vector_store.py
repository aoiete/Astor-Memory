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

    def __init__(self, db_path: Path | None = None, cache_size_mb: int = 100,
                 *, tier: str = 'private', user_id: str | None = None):
        from ..config import get_default_nest_path
        if db_path is None:
            db_path = get_default_nest_path()
        self.db_path = Path(db_path)
        if tier not in ('public', 'source', 'private', 'repo'):
            raise ValueError(f"unsupported nest tier: {tier!r}")
        self.tier = tier
        self.user_id = user_id or (
            '_system' if tier in ('public', 'source') else '_current'
        )
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
        """Close the vector store connection (CLI teardown).

        v1.10.8 (2026-08-26): also remove this instance from the module-level
        singleton dict. Previously the dict kept a reference to a closed
        AstorNest whose `_conn is None`, so the next astor_nest() call with
        the same (tier, user_id, db_path) key returned the stale instance
        and any self._conn.execute(...) crashed with AttributeError.
        """
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        # Pop our key from the singleton so the next astor_nest() rebuilds.
        from .. import _cleanup_nest_singleton
        _cleanup_nest_singleton(self)

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
            """INSERT OR REPLACE INTO embeddings
               (fact_id, embedding, model_name, dim, updated_at, user_id, tier)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?)""",
            (fact_id, _pack_embedding(emb), model_name, emb.shape[0],
             self.user_id, self.tier),
        )
        self._put(fact_id, model_name, emb)
        # v1.10.2: invalidate the search-cache for this model_name so the
        # next search() sees the new row.
        sc = getattr(self, '_search_cache', None)
        if sc:
            key = (model_name, id(self._conn))
            sc.pop(key, None)
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
        # v1.10.2: search-cache matrix is now stale; drop it.
        sc = getattr(self, '_search_cache', None)
        if sc:
            sc.clear()

    def search(
        self,
        query_embedding: np.ndarray,
        limit: int = 5,
        model_name: str | None = None,
    ) -> list[tuple[int, float]]:
        """Brute-force cosine similarity search.

        v1.10.2 (2026-08-26): vectorized with numpy + cached norm matrix.
        Previous loop unpacked + dot-producted one row at a time (~300ms
        for 6334 admin rows). v1.10.2 path stacks all blobs into a single
        (N, dim) matrix and does one matmul (~5ms cold, <1ms warm).

        A per-(model_name) cache holds the stacked matrix + per-row norms +
        fact_ids. The cache is invalidated on store()/invalidate_fact() so
        writes stay correct. First search per model_name loads from DB
        (~10ms for 6334 rows); subsequent searches in same model only
        rebuild the matrix if rows changed.

        Returns list of (fact_id, similarity) sorted by similarity desc.
        User/namespace/tier/since filters apply via JOIN to bus.memory_canonical
        (Plan § 3-store: nest holds embeddings, bus holds metadata).
        """
        if model_name is None:
            from .embeddings import astor_get_model_name_for_ram
            model_name = astor_get_model_name_for_ram()

        # v1.10.2: cached stack + norms. Key by (model_name, conn).
        # If rows in the table changed (insert/delete), we invalidate
        # by counting rows and rebuilding if mismatch.
        cache = getattr(self, '_search_cache', None)
        if cache is None:
            cache = self._search_cache = {}
        key = (model_name, id(self._conn))
        entry = cache.get(key)
        if entry is not None:
            emb_matrix, fact_ids, e_norms, cached_fingerprint = entry
            # v1.10.8 (2026-08-26): use a combined fingerprint (row_count,
            # total_byte_size, max_updated_at) instead of row count alone.
            # Previous logic only invalidated when COUNT(*) changed, which
            # missed INSERT OR REPLACE updates (same fact_id, new embedding
            # bytes) and delete+insert pairs (same row count, new content).
            # Both cases silently returned stale vectors until process restart.
            cur_fp = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(embedding)), 0), "
                "COALESCE(MAX(updated_at), '') "
                "FROM embeddings WHERE model_name = ?",
                (model_name,),
            ).fetchone()
            cur_fingerprint = (cur_fp[0], cur_fp[1], cur_fp[2])
            if cur_fingerprint == cached_fingerprint:
                # v1.10.7 fix: if cached_fingerprint implies empty table, emb_matrix
                # is None — return [] directly. Previous code passed None to
                # _topk, which crashed on `valid = e_norms > 0`.
                if emb_matrix is None or e_norms is None:
                    return []
                return self._topk(query_embedding, emb_matrix, fact_ids, e_norms, limit)
            # fingerprint drifted — invalidate
            cache.pop(key, None)

        # Cold path: load from DB and stack
        rows = self._conn.execute(
            "SELECT fact_id, embedding FROM embeddings WHERE model_name = ?",
            (model_name,),
        ).fetchall()
        if not rows:
            # Cache the empty state so future searches don't re-query.
            # Fingerprint (count=0, total_bytes=0, max_updated_at='') matches
            # what the cache-check SQL would produce when the table is empty.
            cache[key] = (None, None, None, (0, 0, ''))
            return []
        fact_ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        blob = b"".join(r[1] for r in rows)
        first_len = len(rows[0][1])
        # v1.10.3 fix: previous sanity check `blob.count(b"\x00") % 4 != 0`
        # was nonsense — float32 embeddings contain tons of null bytes
        # (e.g. 0.5 = 0x3F000000), so the count is essentially random mod 4
        # and ~75% of searches silently fell back to the slow row-by-row
        # path and NEVER populated the cache. Correct check: blob length
        # must be divisible by 4 (bytes per float32).
        if first_len == 0 or first_len % 4 != 0:
            return self._search_slow(query_embedding, limit, model_name)
        dim = first_len // 4
        if dim == 0:
            return []
        # Verify uniform dim (cheap; protects against corrupt mixed-dim data)
        for r in rows[1:]:
            if len(r[1]) != first_len:
                return self._search_slow(query_embedding, limit, model_name)
        emb_matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), dim)
        e_norms = np.linalg.norm(emb_matrix, axis=1)
        # v1.10.8: cache fingerprint (count, total_bytes, max_updated_at) so the
        # warm-path invalidation check catches INSERT OR REPLACE updates too.
        cache[key] = (
            emb_matrix, fact_ids, e_norms,
            (len(rows), len(blob), str(self._conn.execute(
                "SELECT MAX(updated_at) FROM embeddings WHERE model_name = ?",
                (model_name,),
            ).fetchone()[0] or '')),
        )
        return self._topk(query_embedding, emb_matrix, fact_ids, e_norms, limit)

    def _topk(self, query_embedding, emb_matrix, fact_ids, e_norms, limit,
              recency_weight: float = 0.2, half_life_days: float = 30.0):
        """Inner top-k cosine + recency boost. Shared by search() and _search_slow().

        v1.12.0 (2026-08-29): multi-signal ranking per R250 / Mem0 2026 lesson.
        Final score = sim * (1 - recency_weight) + recency_weight * recency_score
        where recency_score = 0.5 ** (age_days / half_life_days) (exponential decay).
        recency_weight=0 means pure cosine; recency_weight=1 means pure recency.
        Lookup updated_at via single batch query against embeddings.updated_at.
        """
        import time as _time
        query = np.asarray(query_embedding, dtype=np.float32).reshape(-1)
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0:
            return []
        valid = e_norms > 0
        if not valid.any():
            return []
        sims = np.empty(len(fact_ids), dtype=np.float32)
        sims.fill(-np.inf)
        sims[valid] = emb_matrix[valid] @ query / (e_norms[valid] * q_norm)

        # v1.12.0: recency boost — fetch updated_at for the candidate fact_ids
        # only (limit + buffer) so we never scan all rows for a single query.
        candidate_n = min(len(fact_ids), max(limit * 4, 32))
        # Get top sims first (without recency) to know which fact_ids to look up
        if candidate_n >= len(sims):
            pre_idx = np.argsort(-sims)
        else:
            pre_idx = np.argpartition(-sims, candidate_n)[:candidate_n]
        pre_idx = pre_idx[np.argsort(-sims[pre_idx])]
        cand_fids = [int(fact_ids[i]) for i in pre_idx if sims[i] != -np.inf]
        if not cand_fids:
            return []
        try:
            placeholders = ",".join("?" * len(cand_fids))
            age_rows = self._conn.execute(
                f"SELECT fact_id, "
                f"  (julianday('now') - julianday(updated_at)) AS age_days "
                f"FROM embeddings "
                f"WHERE fact_id IN ({placeholders}) AND model_name = ("
                f"  SELECT model_name FROM embeddings WHERE fact_id = ? LIMIT 1)",
                (*cand_fids, cand_fids[0]),
            ).fetchall()
        except Exception:
            age_rows = []
        age_map = {int(r[0]): float(r[1] if r[1] is not None else 9999.0) for r in age_rows}

        scored = []
        for i in pre_idx:
            s = float(sims[i])
            if s == -np.inf:
                continue
            fid = int(fact_ids[i])
            age = age_map.get(fid, 9999.0)
            # recency_score in [0,1] via half-life decay; cap at 9999 days → ~0
            rec = 0.5 ** (min(age, 9999.0) / half_life_days)
            final = s * (1.0 - recency_weight) + rec * recency_weight
            scored.append((fid, final, s, rec))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(fid, final) for fid, final, _, _ in scored[:limit]]

    def _search_slow(self, query_embedding, limit, model_name):
        """Legacy row-by-row search. Kept as a fallback for mixed-dim or
        corrupt DBs where the vectorized fast path is unsafe."""
        rows = self._conn.execute(
            "SELECT fact_id, embedding FROM embeddings WHERE model_name = ?",
            (model_name,),
        ).fetchall()
        results = []
        query_norm = float(np.linalg.norm(query_embedding))
        if query_norm == 0:
            return []
        q = np.asarray(query_embedding)
        for fact_id, emb_blob in rows:
            emb = _unpack_embedding(emb_blob)
            emb_norm = float(np.linalg.norm(emb))
            if emb_norm == 0:
                continue
            sim = float(np.dot(q, emb) / (query_norm * emb_norm))
            results.append((int(fact_id), sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


# v1.10.8 (2026-08-26): correct type annotation. The runtime value is a
# dict[(tier, user_id, str(db_path)) -> AstorNest]; previously annotated
# as AstorNest | None which was both wrong type and missed the per-key
# granularity. Keep the alias module-level for backward compat with
# tests that import _nest_singleton directly.
_nest_singleton: dict | None = None
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
    # v1.10.3: real singleton per (tier, user_id, db_path). Previously this
    # function built a NEW AstorNest on every call despite the
    # _nest_singleton global, which meant the v1.10.2 _search_cache
    # (per-instance) never survived a single request — every /v1/read
    # re-loaded + re-stacked the full embedding matrix (~233ms on admin
    # tier with 3.6K rows). Fix: cache instances keyed by
    # (tier, user_id, str(target)) so the search matrix cache persists
    # across requests. db_path overrides (tests) bypass the singleton so
    # test isolation is preserved.
    if db_path is not None:
        return AstorNest(target, tier=tier, user_id=user_id)
    key = (tier, user_id, str(target))
    global _nest_singleton
    if _nest_singleton is None:
        _nest_singleton = {}
    with _nest_lock:
        inst = _nest_singleton.get(key)
        if inst is None:
            inst = AstorNest(target, tier=tier, user_id=user_id)
            _nest_singleton[key] = inst
        return inst


def astor_nest_for(tier: str, user_id: str | None = None) -> AstorNest:
    """9-db layout nest accessor. Same semantics as astor_nest(tier, user_id)."""
    return astor_nest(tier=tier, user_id=user_id)


def astor_reset_nest() -> None:
    """Reset the singleton (for testing)."""
    global _nest_singleton
    with _nest_lock:
        _nest_singleton = None


__all__ = ["AstorNest", "astor_nest", "astor_nest_for", "astor_reset_nest"]
