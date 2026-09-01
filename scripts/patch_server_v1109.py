"""patch_server.py — apply 4 surgical patches to server.py for v1.10.9 Temporal + Multi-Hop.

Run: D:/AI/Astor-Memory-venv/Scripts/python.exe patch_server.py
"""
import ast
import sys
from pathlib import Path

src = Path(r"<source_dir>astor_memory\server.py")
text = src.read_text(encoding="utf-8")

# Patch 1: read query_timestamp at the top of /v1/read handler
ANCHOR_1_OLD = "        tier = body.get('tier', 'public')\n        # v1.1: tier=repo accepts repo_id (explicit) or user_id (fallback)."
ANCHOR_1_NEW = """        tier = body.get('tier', 'public')
        # v1.10.9 (2026-08-27): accept query_timestamp (LoCoMo, LongMemEval)
        # for temporal proximity boosting.
        query_timestamp = body.get('query_timestamp')
        since_ts = body.get('since_ts')
        until_ts = body.get('until_ts')
        if since_ts and not isinstance(since_ts, str):
            since_ts = None
        if until_ts and not isinstance(until_ts, str):
            until_ts = None
        if query_timestamp and not isinstance(query_timestamp, str):
            query_timestamp = None
        query_anchor = (query_timestamp or '')[:10] or None
        # v1.1: tier=repo accepts repo_id (explicit) or user_id (fallback)."""
assert ANCHOR_1_OLD in text, "ANCHOR_1 missing"
text = text.replace(ANCHOR_1_OLD, ANCHOR_1_NEW, 1)
print("Patch 1 OK")

# Patch 2: build temporal_boost from kw_rows (need event_date)
ANCHOR_2_OLD = "            # Query keywords = tokens of the query (cheap; no LLM call).\n            from .nest.lex_index import _tokenize\n            query_keywords = _tokenize(query)\n            merged = _hybrid_merge("
ANCHOR_2_NEW = """            # Query keywords = tokens of the query (cheap; no LLM call).
            from .nest.lex_index import _tokenize
            query_keywords = _tokenize(query)
            # v1.10.9: build temporal_boost map for hybrid_merge.
            _temporal_boost = {}
            if candidate_fids:
                try:
                    _tb_rows = bus.conn.execute(
                        f"SELECT id, event_date, event_date_precision FROM memory_canonical "
                        f"WHERE id IN ({','.join('?' * len(candidate_fids))})",
                        list(candidate_fids),
                    ).fetchall()
                    for _tbid, _tbd, _tbp in _tb_rows:
                        if _tbd:
                            _temporal_boost[int(_tbid)] = (str(_tbd), str(_tbp or 'day'))
                except Exception:
                    pass
            merged = _hybrid_merge("""
assert ANCHOR_2_OLD in text, "ANCHOR_2 missing"
text = text.replace(ANCHOR_2_OLD, ANCHOR_2_NEW, 1)
print("Patch 2 OK")

# Patch 3: pass temporal_boost + query_anchor into hybrid_merge call
ANCHOR_3_OLD = """            merged = _hybrid_merge(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                bm25_weight=bm25_weight,
                vec_weight=vec_weight,
                limit=oversample,
                keyword_hits=keyword_hits if keyword_hits else None,
                query_keywords=query_keywords,
            )"""
ANCHOR_3_NEW = """            merged = _hybrid_merge(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                bm25_weight=bm25_weight,
                vec_weight=vec_weight,
                limit=oversample,
                keyword_hits=keyword_hits if keyword_hits else None,
                query_keywords=query_keywords,
                temporal_boost=_temporal_boost if _temporal_boost else None,
                temporal_boost_strength=0.7,
                query_anchor=query_anchor,
            )"""
assert ANCHOR_3_OLD in text, "ANCHOR_3 missing"
text = text.replace(ANCHOR_3_OLD, ANCHOR_3_NEW, 1)
print("Patch 3 OK")

# Patch 4: insert multi-hop bridge after the optional rerank block
ANCHOR_4_OLD = """                    reranked = _rerank(query, cand_dicts, top_n=top_k, rerank_weight=0.65)
                    results = [(c['id'], c['score']) for c in reranked]
                except Exception:
                    pass
            # If hybrid returned nothing"""
ANCHOR_4_NEW = """                    reranked = _rerank(query, cand_dicts, top_n=top_k, rerank_weight=0.65)
                    results = [(c['id'], c['score']) for c in reranked]
                except Exception:
                    pass
            # v1.10.9: multi-hop bridge (default ON, no env var).
            # Lifts co-ranking of facts that share named entities, helping
            # multi-hop chains surface together. Pure Python, <5ms per call.
            if results and len(results) >= 2:
                try:
                    from .nest.multi_hop_bridge import apply_multi_hop_boost as _bridge
                    _bfids = [fid for fid, _ in results]
                    if _bfids:
                        _phb = ','.join('?' * len(_bfids))
                        _brows = bus.conn.execute(
                            f"SELECT id, content, keywords, tags FROM memory_canonical WHERE id IN ({_phb})",
                            _bfids,
                        ).fetchall()
                        _bcands = []
                        for _bid, _bct, _bkw, _btg in _brows:
                            try:
                                _bkws = _json.loads(_bkw) if _bkw else []
                            except Exception:
                                _bkws = []
                            try:
                                _btgs = _json.loads(_btg) if _btg else []
                            except Exception:
                                _btgs = []
                            _bcands.append({
                                'id': int(_bid), 'content': _bct or '',
                                'keywords': _bkws, 'tags': _btgs,
                                'score': next((s for f, s in results if f == int(_bid)), 0.0),
                            })
                        _boosted = _bridge(_bcands, top_seed_n=min(5, len(_bcands)))
                        _score_map = {c['id']: c['score'] for c in _boosted}
                        results = [(fid, _score_map.get(int(fid), 0.0)) for fid, _ in results]
                        results.sort(key=lambda x: x[1], reverse=True)
                except Exception:
                    pass
            # If hybrid returned nothing"""
assert ANCHOR_4_OLD in text, "ANCHOR_4 missing"
text = text.replace(ANCHOR_4_OLD, ANCHOR_4_NEW, 1)
print("Patch 4 OK")

# Validate syntax
ast.parse(text)
src.write_text(text, encoding="utf-8")
print("\nAll 4 patches applied. Syntax OK.")
