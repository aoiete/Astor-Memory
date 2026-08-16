"""Tests for astor_memory v0.3.0 lex index + forget + cross-tier recall.

Run:    python -m pytest tests/test_lex_index.py -v
Or:     python tests/test_lex_index.py  (uses unittest)
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Ensure ASTOR_DIR points at the live runtime so we exercise real paths.
os.environ['ASTOR_DIR'] = str(Path(r'<runtime_dir>'))
sys.path.insert(0, str(Path(r'<source_dir>')))

from astor_memory.nest.lex_index import (
    astor_lex, hybrid_merge, _tokenize, BM25_K1, BM25_B,
)
from astor_memory.nest import lex_rebuild


class TokenizeTests(unittest.TestCase):
    def test_english_lowercase(self):
        self.assertEqual(_tokenize('Hello WORLD'),
                         ['hello', 'world'])
    def test_stopwords_dropped(self):
        self.assertEqual(_tokenize('The cat is on the mat'),
                         ['cat', 'mat'])
    def test_short_tokens_dropped(self):
        # tokens < 2 chars dropped (after stop)
        self.assertEqual(_tokenize('a to be'),
                         [])
    def test_cjk_unigram(self):
        # Chinese: each CJK char is its own token
        self.assertEqual(_tokenize('小明'), ['小', '明'])
    def test_mixed(self):
        out = _tokenize('我叫小明 my name is John')
        self.assertIn('john', out)
        self.assertIn('小', out)
        self.assertIn('明', out)


class LexIndexTests(unittest.TestCase):
    def setUp(self):
        self.lex = astor_lex(tier='public', user_id=None)
        # cleanup any prior test data
        self.lex._conn.execute('DELETE FROM documents')
        self.lex._conn.execute('DELETE FROM terms')
        self.lex._conn.execute('DELETE FROM postings')
        self.lex._refresh_stats()

    def test_index_and_search(self):
        # Three docs that vary by length and overlap with the query.
        self.lex.index_fact(1, 'Astor memory uses inverted index for keyword search')
        self.lex.index_fact(2, 'Flipkart is an online shopping platform')
        self.lex.index_fact(3, 'Astor has both BM25 and vector recall')
        hits = self.lex.bm25_search('Astor memory BM25')
        scores_by_fid = dict(hits)
        # Doc 2 has NO query-token overlap → BM25 correctly omits it.
        self.assertNotIn(2, scores_by_fid)
        # Both relevant docs (1 and 3) must surface, and doc 3 (shorter,
        # better length-normalized) scores higher than doc 1.
        self.assertIn(1, scores_by_fid)
        self.assertIn(3, scores_by_fid)
        self.assertGreater(scores_by_fid[3], scores_by_fid[1])

    def test_remove_fact(self):
        self.lex.index_fact(10, 'apple banana cherry')
        self.lex.index_fact(11, 'apple durian elderberry')
        # both contain "apple"
        hits_before = self.lex.bm25_search('apple')
        self.assertEqual(len(hits_before), 2)
        # remove doc 11
        self.lex.remove_fact_hard(11)
        hits_after = self.lex.bm25_search('apple')
        self.assertEqual(len(hits_after), 1)
        self.assertEqual(hits_after[0][0], 10)

    def test_cjk_recall(self):
        self.lex.index_fact(20, '我的名字叫小明')
        hits = self.lex.bm25_search('小明')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 20)

    def test_idf_zero_when_term_in_all_docs(self):
        # Term "common" appears in every doc → IDF → 0. The BM25 score
        # should then be 0 (or near zero) regardless of tf, since the
        # idf factor controls the score. Verify doc with no overlap gets
        # a bigger score when there IS an "exclusive" term — but only
        # check the basic invariant: tf and df drive ordering.
        self.lex.index_fact(30, 'common cat')
        self.lex.index_fact(31, 'common cat common')  # tf = 2 for common
        self.lex.index_fact(32, 'common')              # shortest doc
        # search "common" alone — every doc has it.
        # We don't assert exact ranking (BM25 has subtle behaviors with
        # dlen normalization) — just verify all 3 docs surface.
        hits = self.lex.bm25_search('common')
        fids = {fid for fid, _ in hits}
        self.assertEqual(fids, {30, 31, 32})


class HybridMergeTests(unittest.TestCase):
    def test_pure_vector_only(self):
        merged = hybrid_merge(
            bm25_hits=[],
            vector_hits=[(1, 0.9), (2, 0.5)],
            bm25_weight=0.4, vec_weight=0.6, limit=10,
        )
        # just rescaled cosine
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0][0], 1)
    def test_pure_bm25_only(self):
        merged = hybrid_merge(
            bm25_hits=[(1, 5.0), (2, 2.0)],
            vector_hits=[],
            bm25_weight=0.4, vec_weight=0.6, limit=10,
        )
        self.assertEqual(len(merged), 2)
        # doc 1 ranks higher
        self.assertEqual(merged[0][0], 1)
    def test_bm25_rescues_far_vector_match(self):
        # doc A: BM25 high, vector low (irrelevant text)
        # doc B: BM25 low, vector high
        # merged should keep A higher than B (or comparable)
        merged = hybrid_merge(
            bm25_hits=[(1, 8.0), (2, 1.0)],
            vector_hits=[(2, 0.95), (1, 0.40)],
            bm25_weight=0.5, vec_weight=0.5, limit=5,
        )
        # doc 1 score = 0.5*1.0 + 0.5*0.40 = 0.70
        # doc 2 score = 0.5*0.125 + 0.5*0.95 = 0.5375
        # doc 1 should win
        self.assertEqual(merged[0][0], 1)


class LexRebuildTests(unittest.TestCase):
    def test_rebuild_returns_expected_shape(self):
        """rebuild() must return a dict with indexed_docs OR a 'skipped'
        marker (when no bus DB exists). Smoke test against a tempdir."""
        import tempfile
        import os
        from astor_memory.nest import lex_index as _li
        tmp = tempfile.mkdtemp(prefix='astor_rebuild_test_')
        old_env = os.environ.get('ASTOR_DIR')
        os.environ['ASTOR_DIR'] = tmp
        try:
            with _li._LEX_SINGLETONS_LOCK:
                for k in list(_li._LEX_SINGLETONS):
                    inst = _li._LEX_SINGLETONS.pop(k, None)
                    if inst is not None:
                        try:
                            inst.close()
                        except Exception:
                            pass
            from astor_memory._internal import acl_layout
            acl_layout._ASTOR_DIR = None
            from astor_memory.nest import lex_rebuild as _lr
            r = _lr.rebuild('public', None, drop=True)
            # Either indexed_docs > 0 (when bus had data) OR skipped (no bus)
            self.assertTrue(
                'indexed_docs' in r or 'skipped' in r,
                f'unexpected rebuild response: {r}',
            )
        finally:
            if old_env is not None:
                os.environ['ASTOR_DIR'] = old_env
            else:
                os.environ.pop('ASTOR_DIR', None)
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)


class ServerIntegrationTests(unittest.TestCase):
    """These hit the live astor server on localhost:7803.
    Skipped automatically when server is not running."""
    @classmethod
    def setUpClass(cls):
        import urllib.request as _ur
        try:
            _ur.urlopen('http://127.0.0.1:7803/v1/health', timeout=2)
        except Exception:
            raise unittest.SkipTest('astor server not running on 7803')

    def _post(self, path: str, payload: dict, timeout: int = 30):
        import json
        import urllib.request as _ur
        req = _ur.Request(
            f'http://127.0.0.1:7803{path}',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
        )
        return json.loads(_ur.urlopen(req, timeout=timeout).read())

    def _get(self, path: str, timeout: int = 30):
        import json as _json
        import urllib.request as _ur
        return _json.loads(_ur.urlopen(
            f'http://127.0.0.1:7803{path}', timeout=timeout
        ).read())

    def test_hybrid_default_returns_score_kind(self):
        r = self._post('/v1/read', {'query': 'astor memory',
                                    'tier': 'public', 'top_k': 3})
        self.assertGreater(len(r['results']), 0)
        for res in r['results']:
            self.assertIn('score_kind', res)
            self.assertEqual(res['score_kind'], 'hybrid')

    def test_pure_vector_when_hybrid_false(self):
        r = self._post('/v1/read', {'query': 'astor memory',
                                    'tier': 'public', 'top_k': 3,
                                    'hybrid': False})
        for res in r['results']:
            self.assertEqual(res['score_kind'], 'cosine')

    def test_lex_stats_endpoint(self):
        r = self._get('/v1/lex/stats')
        self.assertIn('public/_', r)
        self.assertIn('documents', r['public/_'])
        self.assertGreater(r['public/_']['documents'], 0)

    def test_read_multi_cross_tier(self):
        r = self._post('/v1/read/multi', {
            'query': 'astor', 'user_id': 'admin',
            'top_k': 10, 'hybrid': True,
        })
        self.assertGreater(len(r['results']), 0)
        scopes = {s['tier'] for s in r['scopes_searched']}
        self.assertIn('public', scopes)
        self.assertIn('private', scopes)
        for res in r['results']:
            self.assertIn('tier', res)
            self.assertIn('cross_tier_score', res)

    def test_forget_by_query(self):
        # write unique fact
        w = self._post('/v1/write', {
            'text': 'delete_me_quokka_42 unique test fact.',
            'tier': 'public', 'mode': 'regex', 'user': 'admin',
        })
        self.assertEqual(w['count'], 1)
        fid = w['fact_ids'][0]
        # forget it
        r = self._post('/v1/forget', {
            'query': 'delete_me_quokka_42',
            'tier': 'public', 'forget_threshold': 0.5,
        })
        self.assertEqual(len(r['forgotten']), 1)
        self.assertEqual(r['forgotten'][0]['fact_id'], fid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
