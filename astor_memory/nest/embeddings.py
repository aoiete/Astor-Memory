"""
Lazy-load embedding model via fastembed.

Per Plan § Cold start performance:
- Lazy load (daemon ready < 1 second)
- First recall blocks 3-5 seconds for model load
- Subsequent recall < 100ms

Per Plan § Memory ↔ speed:
- Auto-select model based on system RAM at install time:
  - >= 16 GB RAM: multilingual-e5-base (100+ languages)
  - 8-16 GB: BGE-small-en-v1.5 (English) or BGE-small-zh-v1.5 (Chinese)
  - < 8 GB: all-MiniLM-L6-v2 (21 MB, lowest RAM)
"""

from __future__ import annotations

import threading
import psutil

_models: dict[str, object] = {}
_model_lock = threading.Lock()

# v1.10.3: query embedding cache. The dominant per-request cost in /v1/read
# is `model.embed([query])` (~300ms for bge-base on CPU). Hermes retries +
# repeated user questions hit the same query strings often enough that a
# small LRU+TTL cache pays for itself immediately: cache hit = ~0ms vs 300ms.
# Cache is keyed by (model_name, normalized_query); per-model since dims differ.
_QUERY_EMBED_CACHE: dict[tuple[str, str], tuple] = {}
_QUERY_EMBED_CACHE_MAX = 256
_QUERY_EMBED_CACHE_TTL_S = 300.0  # 5 min — long enough for session repeats, short enough to not go stale


def astor_embed_query_cached(model, model_name: str, query: str):
    """Embed a single query string with LRU+TTL cache.

    Returns np.ndarray (float32). Cache key = (model_name, query.strip().lower()).
    Evicts expired + LRU entries when full. Thread-safe via _model_lock (short critical section).
    """
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
        # Evict oldest if full
        if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
            oldest_k = min(_QUERY_EMBED_CACHE, key=lambda k: _QUERY_EMBED_CACHE[k][1])
            _QUERY_EMBED_CACHE.pop(oldest_k, None)
    # Embed outside the lock (300ms critical section would serialize requests)
    emb = list(model.embed([query]))[0]
    emb = _np_qc.asarray(emb, dtype=_np_qc.float32)
    with _model_lock:
        _QUERY_EMBED_CACHE[key] = (emb, now)
    return emb


def astor_get_model_name_for_ram() -> str:
    """Pick embedding model based on system RAM.

    v1.10.1 (2026-08-26): factory function now consults ASTOR_EMBEDDING_USE_BGE_SMALL.
    bge-base is 92M params and embeds 10 texts in ~3.8s on CPU, which
    makes self-reflection's batch-embed path slow on every /v1/read.
    bge-small is 33M params (384d) and is ~4x faster with negligible
    quality drop for our use case (recall + matching, not RAG top-K).

    IMPORTANT: changing model means dim changes (768d -> 384d). Existing
    embeddings in nest.embeddings are keyed by model_name. Old facts stay
    with old model; new facts use new model. They don't mix in recall
    because nest.search filters by model_name. To migrate, run
    `am reembed` to recompute all embeddings under new model.

    For the 2026-08-26 perf fix we keep bge-base as the default (so existing
    vector index keeps working) but expose ASTOR_EMBEDDING_USE_BGE_SMALL=1
    to switch. Caller in match_experiences / hot embed paths can opt-in
    via astor_get_embedding_model('BAAI/bge-small-en-v1.5') directly.
    """
    import os as _os_e
    override = _os_e.environ.get("ASTOR_EMBEDDING_MODEL")
    if override:
        return override
    mem_gb = psutil.virtual_memory().total / 1024**3
    if mem_gb >= 16:
        return 'BAAI/bge-base-en-v1.5'  # 92M params, 768d (existing index)
    elif mem_gb >= 8:
        return 'BAAI/bge-small-en-v1.5'  # 33M params, 384d
    else:
        return 'sentence-transformers/all-MiniLM-L6-v2'  # 22M params, 384d, lowest RAM


def astor_get_embedding_model(model_name: str | None = None):
    """Lazy load and return embedding model.

    v1.10.1: per-model cache (was singleton). match_experiences hot path
    uses bge-small (384d) while main recall uses bge-base (768d), so we
    cache both models keyed by name. This adds ~150MB RAM (bge-small
    in addition to bge-base) but cuts 1.2-3s of bge-base embed time per
    /v1/read on every kw=0 reflect call.
    """
    name = model_name or astor_get_model_name_for_ram()
    with _model_lock:
        if name not in _models or _models[name] is None:
            from fastembed import TextEmbedding
            _models[name] = TextEmbedding(model_name=name)
        return _models[name]


def astor_reset_embedding_model() -> None:
    """Reset the singleton (for testing). v1.10.1: clears all cached models."""
    global _models
    with _model_lock:
        _models = {}


__all__ = ["astor_get_embedding_model", "astor_get_model_name_for_ram", "astor_reset_embedding_model"]
