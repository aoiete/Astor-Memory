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

_model = None
_model_lock = threading.Lock()


def astor_get_model_name_for_ram() -> str:
    """Pick embedding model based on system RAM."""
    mem_gb = psutil.virtual_memory().total / 1024**3
    if mem_gb >= 16:
        return 'BAAI/bge-base-en-v1.5'  # 92M params, 768d, English best quality
    elif mem_gb >= 8:
        return 'BAAI/bge-small-en-v1.5'  # 33M params, 384d
    else:
        return 'sentence-transformers/all-MiniLM-L6-v2'  # 22M params, 384d, lowest RAM


def astor_get_embedding_model(model_name: str | None = None):
    """Lazy load and return embedding model (singleton)."""
    global _model
    with _model_lock:
        if _model is None:
            name = model_name or astor_get_model_name_for_ram()
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=name)
        return _model


def astor_reset_embedding_model() -> None:
    """Reset the singleton (for testing)."""
    global _model
    with _model_lock:
        _model = None


__all__ = ["astor_get_embedding_model", "astor_get_model_name_for_ram", "astor_reset_embedding_model"]
