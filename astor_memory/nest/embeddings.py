"""
astor_embedding singleton — thread-safe for waitress 8-thread pool.

Per R-class 12518 + R-class 100-user scaling:
- Old code: _models[model_name] shared across threads, single Lock for everything
- Issue: under 8 concurrent threads, all serialize on _model_lock during embed
  (300ms × 8 = 2.4s effective latency even with 8 threads)
- Fix: per-thread model cache. Each thread loads its own model (1.5GB * 8 = 12GB
  if all hold models, too much RAM) →  use a thread pool of N model instances
  (e.g. 4 instances shared across 8 threads)
"""
from __future__ import annotations

import threading
import gc
import os
import time as _t
from collections import deque

_models: dict[str, object] = {}
_model_lock = threading.Lock()

# Thread-safe query cache (256 LRU entries per model)
_QUERY_EMBED_CACHE: dict[tuple[str, str], tuple] = {}
_QUERY_EMBED_CACHE_MAX = 256
_QUERY_EMBED_CACHE_TTL_S = 300.0
_query_cache_lock = threading.Lock()

# v1.16.42: thread-pool of model instances for true parallelism
# ASTOR_EMBEDDING_POOL_SIZE = number of model instances to keep in memory
# Default = min(8, ASTOR_SERVER_THREADS)
_POOL_SIZE = int(os.environ.get('ASTOR_EMBEDDING_POOL_SIZE', '4'))
_model_pool: deque = deque()  # pool of model objects (Round Robin)
_model_pool_size_per_name: dict[str, int] = {}


def _gc_collect_safe():
    gc.collect()
    try:
        import sys as _s
        if _s.platform.startswith('linux'):
            with open(f'/proc/{_s.getpid()}/status'):
                pass
    except Exception:
        pass


def _make_model(model_name: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=model_name)


def _get_pooled_model(model_name: str):
    """Return a model instance from the pool, or create one.

    Uses round-robin assignment. Models are reused across threads.
    Pool size = _POOL_SIZE (default 4) so 8 threads share 4 model instances.
    """
    global _model_pool, _model_pool_size_per_name
    with _model_lock:
        # Filter pool to only this model_name
        same_models = [m for m in _model_pool
                       if _model_pool_size_per_name.get(id(m)) == model_name]

        if same_models:
            return same_models[len(same_models) % max(1, len(same_models))]

        # Need to grow the pool
        if len(same_models) < _POOL_SIZE:
            new_model = _make_model(model_name)
            _model_pool.append(new_model)
            _model_pool_size_per_name[id(new_model)] = model_name
            return new_model

        # Pool full but no match — return existing one (fallback)
        return same_models[0] if same_models else _make_model(model_name)


def astor_embed_query_cached(model_name: str, query: str):
    """Embed a single query string. LRU+TTL cache + thread-safe.

    v1.16.42: takes model_name (not model instance) so it can route to pool.
    """
    import numpy as _np
    key = (model_name, query.strip().lower())
    now = _t.time()

    # Cache hit (no model needed)
    with _query_cache_lock:
        entry = _QUERY_EMBED_CACHE.get(key)
        if entry is not None:
            emb, ts = entry
            if now - ts < _QUERY_EMBED_CACHE_TTL_S:
                return emb
            _QUERY_EMBED_CACHE.pop(key, None)
        if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
            oldest_k = min(_QUERY_EMBED_CACHE, key=lambda k: _QUERY_EMBED_CACHE[k][1])
            _QUERY_EMBED_CACHE.pop(oldest_k, None)

    # Embed via pool
    model = _get_pooled_model(model_name)
    emb = list(model.embed([query]))[0]
    emb = _np.asarray(emb, dtype=_np.float32)

    with _query_cache_lock:
        _QUERY_EMBED_CACHE[key] = (emb, now)
    return emb


def astor_get_model_name_for_ram() -> str:
    """Pick embedding model based on system RAM."""
    override = os.environ.get('ASTOR_EMBEDDING_MODEL')
    if override:
        return override
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        if ram_gb >= 96:
            return 'intfloat/multilingual-e5-large'
        elif ram_gb >= 16:
            return 'BAAI/bge-base-en-v1.5'
        elif ram_gb >= 8:
            return 'BAAI/bge-small-en-v1.5'
        else:
            return 'sentence-transformers/all-MiniLM-L6-v2'
    except ImportError:
        return 'BAAI/bge-base-en-v1.5'


def astor_get_embedding_model(model_name: str | None = None):
    """Backward-compat: return one model instance (for legacy callers)."""
    target = model_name or astor_get_model_name_for_ram()
    return _get_pooled_model(target)


def astor_reset_embedding_model() -> None:
    """Reset all models + cache (for testing)."""
    global _models, _model_pool, _model_pool_size_per_name
    with _model_lock:
        _models = {}
        _model_pool.clear()
        _model_pool_size_per_name.clear()
    with _query_cache_lock:
        _QUERY_EMBED_CACHE.clear()
    _gc_collect_safe()


__all__ = [
    'astor_embed_query_cached',
    'astor_get_model_name_for_ram',
    'astor_get_embedding_model',
    'astor_reset_embedding_model',
    'ASTOR_EMBEDDING_POOL_SIZE',
]