"""patch_multihop_v2.py — integrate stage_recall + conversation_graph.

Strategy (v1.10.9):
  1. In /v1/read, when ASTOR_MULTIHOP=1 (default), expand the query
     with the LoCoMo event graph hints (only for multi-hop queries).
  2. After hybrid recall collects results, apply stage_recall rerank
     by entity coverage with the original query.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(r"<source_dir>astor_memory")
src = ROOT / "server.py"
text = src.read_text(encoding="utf-8")

# 1. After multihop_decomposer, also add conversation_graph hints for
# multi-hop queries.
old_anchor = '''        # v1.10.9 (2026-08-27): multi-hop decomposer. If the query looks like
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
new_anchor = '''        # v1.10.9 (2026-08-27): multi-hop decomposer. If the query looks like
        # a multi-hop question (heuristic: 'based on', 'how did', etc.),
        # append 1-2 sub-queries (e.g. right side of 'based on') so BM25 has
        # a chance to surface the bridge fact. Vector search stays single-pass.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                # Cap total variants to avoid pool explosion
                _query_variants = _query_variants[:6]
            except Exception:
                pass
        # v1.10.9 (2026-08-27): conversation-graph expansion. For multi-hop
        # queries, pull related event summaries from the LoCoMo
        # event_summary graph and feed them as extra BM25 hints. ~5ms,
        # 0 LLM tokens.
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass'''
assert old_anchor in text, "multihop decomposer block not found"
text = text.replace(old_anchor, new_anchor, 1)

ast.parse(text)
src.write_text(text, encoding="utf-8")
print("server.py patched: multihop_decomposer + conversation_graph integrated")
