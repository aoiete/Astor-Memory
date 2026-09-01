"""patch_stage_recall.py — apply stage_recall rerank right before the
optional rerank block in /v1/read."""
import ast
import sys
from pathlib import Path

ROOT = Path(r"<source_dir>astor_memory")
src = ROOT / "server.py"
text = src.read_text(encoding="utf-8")

# 1. Stage_recall rerank: top-K (default 30) -> entity-coverage rerank -> top_k
old_anchor = '''            # v1.10.9 (2026-08-26): optional rerank (env ASTOR_RERANK=1).
            # Lifts multi-hop chain coherence via lexical+bridge rerank.
            if os.environ.get('ASTOR_RERANK', '0') == '1' and results:'''
new_anchor = '''            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query (key signal for multi-hop). Free, <5ms.
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
assert old_anchor in text, "rerank block not found"
text = text.replace(old_anchor, new_anchor, 1)

ast.parse(text)
src.write_text(text, encoding="utf-8")
print("server.py patched: stage_recall rerank integrated")
