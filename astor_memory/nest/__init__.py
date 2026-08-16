"""
astor_nest: SQLite + numpy vector store for fact embeddings.

v1.0 simple install: same SQLite file as bus (memory_canonical.embedding BLOB).

Features:
- Brute-force cosine similarity (1ms per query at 5K docs; Plan § Architecture)
- L1/L2 cache with version-based invalidation (Plan § Embedding cache invalidation)
- Lazy model load (Plan § Cold start)
- Write-time dedup (Plan § Write-time dedup)

v1.1+ (deferred): HNSW index for > 100K docs
"""
from .vector_store import AstorNest, astor_nest, astor_reset_nest
from .embeddings import astor_get_embedding_model, astor_reset_embedding_model, astor_get_model_name_for_ram

__all__ = [
    'AstorNest', 'astor_nest', 'astor_reset_nest',
    'astor_get_embedding_model', 'astor_reset_embedding_model', 'astor_get_model_name_for_ram',
]
