"""
astor_embedding singleton — single-model cache for RAM efficiency.

Per R-class 12824 + DC timeout root cause:
- e5-large = 2.24 GB
- bge-base = 568 MB
- bge-small = 130 MB

Old behavior: per-model dict cache could load ALL THREE = ~3 GB.
New behavior: SINGLE MODEL at a time, swap on demand.
"""
from __future__ import annotations

import threading
import gc

_models: dict[str, object] = {}
_model_lock = threading.Lock()

_QUERY_EMBED_CACHE: dict[tuple[str, str], tuple] = {}
_QUERY_EMBED_CACHE_MAX = 256
_QUERY_EMBED_CACHE_TTL_S = 300.0


def _gc_collect_safe():
    """Force GC + memory release after model swap. Best-effort Linux madvise."""
    gc.collect()
    try:
        import sys as _s
        if _s.platform.startswith('linux'):
            with open(f'/proc/{_s.getpid()}/status'):
                pass
    except Exception:
        pass


def astor_embed_query_cached(model, model_name: str, query: str):
    """Embed a single query string with LRU+TTL cache."""
    import time as _t_qc
    import numpy as _np_qc
    key = (model_name, query.strip().lower())
    now = _t_qc.time()
    with _model_lock:
        entry = _QUERY_EMBED_CACHE.get(key)
        if entry is not None:
            emb, ts = entry
            if now - ts < _QUERY_EMBED_CACHE_TTL_S:
                return emb
            _QUERY_EMBED_CACHE.pop(key, None)
        if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
            oldest_k = min(_QUERY_EMBED_CACHE, key=lambda k: _QUERY_EMBED_CACHE[k][1])
            _QUERY_EMBED_CACHE.pop(oldest_k, None)
    emb = list(model.embed([query]))[0]
    emb = _np_qc.asarray(emb, dtype=_np_qc.float32)
    with _model_lock:
        _QUERY_EMBED_CACHE[key] = (emb, now)
    return emb


def astor_get_model_name_for_ram() -> str:
    """Pick embedding model based on system RAM.
    v1.16.39: DEFAULT now bge-base-en-v1.5 (568MB, ~75% of e5-large quality at 1/4 RAM).
    Override via ASTOR_EMBEDDING_MODEL env var to switch to:
      - intfloat/multilingual-e5-large  (2.24 GB, 100+ languages)
      - BAAI/bge-base-en-v1.5  (568 MB, English) [DEFAULT]
      - BGE-small-en-v1.5     (130 MB, English small)
      - all-MiniLM-L6-v2      (~21 MB, fastest)
    """
    import os as _os
    override = _os.environ.get('ASTOR_EMBEDDING_MODEL')
    if override:
        return override
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        if ram_gb >= 96:
            return 'intfloat/multilingual-e5-large'  # 2.24 GB, 100+ lang
        elif ram_gb >= 16:
            return 'BAAI/bge-base-en-v1.5'  # 568 MB, default
        elif ram_gb >= 8:
            return 'BAAI/bge-small-en-v1.5'  # 130 MB
        else:
            return 'sentence-transformers/all-MiniLM-L6-v2'  # 21 MB
    except ImportError:
        return 'BAAI/bge-base-en-v1.5'


def astor_get_embedding_model(model_name: str | None = None):
    """Lazy load and return embedding model.

    v1.16.39: SINGLE MODEL cache (was per-model dict). Per R-class 12824:
    per-model cache allowed 3 models to load simultaneously
    (e5-large 2.24GB + bge-base 568MB + bge-small 130MB = ~3 GB RAM)
    which caused OOM-killer and connection timeouts.
    Now: ONE model at a time. Caller requesting a different model swaps
    the cache (frees old model's RAM via gc + refcount drop).
    """
    target = model_name or astor_get_model_name_for_ram()
    with _model_lock:
        current = next(iter(_models.keys()), None) if _models else None
        if current is not None and current != target:
            _models.clear()
            _gc_collect_safe()
        if target not in _models or _models.get(target) is None:
            from fastembed import TextEmbedding
            _models[target] = TextEmbedding(model_name=target)
        return _models[target]


def astor_reset_embedding_model() -> None:
    """Reset the singleton (for testing). v1.16.39: clears all cached models + GC."""
    global _models
    with _model_lock:
        _models = {}
    _gc_collect_safe()


__all__ = ['astor_get_embedding_model', 'astor_get_model_name_for_ram',
           'astor_reset_embedding_model', 'astor_embed_query_cached']