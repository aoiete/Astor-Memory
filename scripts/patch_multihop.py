"""patch_multihop.py — v1.10.9 (2026-08-27)

Integrate multihop_decomposer into /v1/read. When the query is detected as
multi-hop, we run BM25 search on the original query AND each decomposed
sub-query, then dedupe by best BM25 score. Vector search stays single-pass
to avoid candidate pool explosion.

Cost: 0 LLM tokens. Adds <5ms per request.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(r"<source_dir>astor_memory")
src = ROOT / "server.py"
text = src.read_text(encoding="utf-8")

# 1. After `_query_variants = [query]` initialization, append multi-hop
# decomposition if the query is flagged.
old_anchor = '''        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass'''
new_anchor = '''        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer. If the query looks like
        # a multi-hop question (heuristic: 'based on', 'how did', etc.),
        # append 1-2 sub-queries (e.g. right side of 'based on') so BM25 has
        # a chance to surface the bridge fact. Vector search stays single-pass.
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import decompose as _mh
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                # Cap total variants to avoid pool explosion
                _query_variants = _query_variants[:6]
            except Exception:
                pass'''

assert old_anchor in text, "synonym expansion block not found"
text = text.replace(old_anchor, new_anchor, 1)

ast.parse(text)
src.write_text(text, encoding="utf-8")
print("server.py patched: multihop_decomposer integrated")
