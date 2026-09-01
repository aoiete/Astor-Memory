"""patch_v1109_final.py — apply all v1.10.9 enhancements in one shot.

Includes:
  1. multihop_decomposer (heuristic query decomposition)
  2. conversation_graph (LoCoMo event_summary expansion for multi-hop)
  3. stage_recall (entity-coverage rerank)
"""
import ast
import sys
from pathlib import Path

ROOT = Path(r"<source_dir>astor_memory")
src = ROOT / "server.py"
text = src.read_text(encoding="utf-8")

# 1. Add multihop + graph after synonym expansion
ANCHOR_1_OLD = '''        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass'''
ANCHOR_1_NEW = '''        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
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
assert ANCHOR_1_OLD in text, "synonym expansion block not found"
text = text.replace(ANCHOR_1_OLD, ANCHOR_1_NEW, 1)
print("Patch 1 OK: multihop_decomposer + conversation_graph integrated")

# 2. Add stage_recall rerank right before optional rerank
ANCHOR_2_OLD = '''            # v1.10.9 (2026-08-26): optional rerank (env ASTOR_RERANK=1).
            # Lifts multi-hop chain coherence via lexical+bridge rerank.
            if os.environ.get('ASTOR_RERANK', '0') == '1' and results:'''
ANCHOR_2_NEW = '''            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query. Free, <5ms.
            if os.environ.get('ASTOR_STAGERECALL', '1') != '0' and results:
                try:
                    from .nest.stage_recall import stage_recall_rerank as _sr
                    if candidate_fids:
                        _ph2 = ','.join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, content FROM memory_canonical "
                            f"WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        _cand_text = {int(r[0]): (r[1] or '') for r in _rtext}
                    else:
                        _cand_text = {}
                    results = _sr(results, _cand_text, query, top_k=top_k)
                except Exception:
                    pass
            # v1.10.9 (2026-08-26): optional rerank (env ASTOR_RERANK=1).
            # Lifts multi-hop chain coherence via lexical+bridge rerank.
            if os.environ.get('ASTOR_RERANK', '0') == '1' and results:'''
assert ANCHOR_2_OLD in text, "rerank block not found"
text = text.replace(ANCHOR_2_OLD, ANCHOR_2_NEW, 1)
print("Patch 2 OK: stage_recall rerank integrated")

ast.parse(text)
src.write_text(text, encoding="utf-8")
print("All v1.10.9 multi-hop enhancements applied. Syntax OK.")
