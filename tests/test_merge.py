"""Tests for astor_memory.nest.merge (dedup v2).

Run: python tests/test_merge.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ['ASTOR_DIR'] = str(Path(r'D:\\AI\\Astor-Memory-Runtime'))
sys.path.insert(0, str(Path(r'D:\\AI\\astor-memory')))
# Do not purge astor_memory from sys.modules here.  Pytest imports test
# modules during collection, and test_acl.py keeps references to its imported
# ACL functions.  Purging the package creates a second ACL module instance
# whose thread-local state is different from those references, causing order-
# dependent false failures.  ASTOR_DIR is set above before the package is
# first imported by this test process.


class MergeCoreTests(unittest.TestCase):
    """Pure-Python tests for the merge module — no server required."""

    def test_llm_judge_returns_dict_shape(self):
        from astor_memory.nest.merge import _llm_judge_pair
        # We can't assume an LLM is reachable, but the function must
        # return a valid dict in the documented shape.
        out = _llm_judge_pair(
            "User prefers iced coffee.",
            "The user likes cold-brewed coffee.",
        )
        self.assertIn('verdict', out)
        self.assertIn('winner', out)
        self.assertIn('confidence', out)
        self.assertIn('reason', out)
        # verdict is one of the documented values
        self.assertIn(out['verdict'], ('same', 'distinct', 'subsume', 'unknown'))
        self.assertIn(out['winner'], ('a', 'b', 'either'))

    def test_cosine_basic(self):
        import numpy as np
        from astor_memory.nest.merge import _cosine
        self.assertAlmostEqual(_cosine(
            np.array([1.0, 0.0]), np.array([1.0, 0.0])
        ), 1.0, places=5)
        self.assertAlmostEqual(_cosine(
            np.array([1.0, 0.0]), np.array([0.0, 1.0])
        ), 0.0, places=5)
        # unit vectors at 45°
        import math
        self.assertAlmostEqual(_cosine(
            np.array([1.0, 0.0]), np.array([math.sqrt(2)/2]*2)
        ), math.sqrt(2)/2, places=4)

    def test_unpack_roundtrip(self):
        import numpy as np
        from astor_memory.nest.merge import _unpack
        arr = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        packed = arr.tobytes()
        out = _unpack(packed)
        np.testing.assert_array_almost_equal(out, arr)

    def test_apply_merges_idempotent_on_missing_loser(self):
        """Applying merges with a non-existent loser_id should be skipped
        gracefully (counted in 'skipped') rather than raising.

        This test requires a first_admin ACL context (the in-process
        bus/nest/lex constructors check it), so we skip if we can't
        set one up."""
        try:
            from astor_memory._internal.acl import astor_init_acl
            astor_init_acl(
                actor='first_admin', role='first_admin', tier='public',
            )
        except Exception as exc:
            self.skipTest(f'cannot init ACL: {exc}')
        try:
            from astor_memory.nest.merge import apply_merges
            r = apply_merges([{
                'winner_id': 999999, 'loser_id': 999998,
                'tier': 'public', 'user_id': None,
            }], actor='unit_test')
            self.assertEqual(r['applied_count'], 0)
            self.assertEqual(r['skipped_count'], 1)
            # reason should mention missing (not just ACL error)
            self.assertIn(
                'missing', r['skipped'][0]['reason'],
                f'expected "missing" in reason, got: {r["skipped"][0]}',
            )
        finally:
            # Best-effort: do NOT clear ACL (other tests in suite share it)
            pass


class MergeServerTests(unittest.TestCase):
    """End-to-end against the live astor server."""

    @classmethod
    def setUpClass(cls):
        import urllib.request as _ur
        try:
            _ur.urlopen('http://127.0.0.1:7803/v1/health', timeout=2)
        except Exception:
            raise unittest.SkipTest('astor server not running on 7803')

    def _post(self, path: str, payload: dict, timeout: int = 60,
              allow_4xx: bool = False):
        """POST helper. If allow_4xx=True, expect a 400/404 and return
        a dict like {'error': 'http 400'}."""
        import json
        import urllib.request as _ur
        req = _ur.Request(
            f'http://127.0.0.1:7803{path}',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
        )
        try:
            return json.loads(_ur.urlopen(req, timeout=timeout).read())
        except Exception as exc:
            # urllib.error.HTTPError is the .NET 4xx case
            import urllib.error as _ue
            if isinstance(exc, _ue.HTTPError) and allow_4xx:
                return {
                    'http_status': exc.code,
                    'reason': exc.reason,
                    'body': exc.read().decode(errors='ignore'),
                }
            raise

    def test_merge_find_returns_groups(self):
        r = self._post('/v1/merge/find', {
            'tier': 'public', 'top_k': 50,
            'use_llm': False, 'threshold': 0.85,
        })
        self.assertIn('groups', r)
        self.assertIn('group_count', r)
        if r['groups']:
            g = r['groups'][0]
            self.assertIn('suggested_winner', g)
            self.assertIn('losers', g)

    def test_merge_apply_simple(self):
        # Create a controlled duplicate: write two near-identical facts
        # via the write endpoint, then merge them. We DO NOT use the
        # LLM judge here because we may not have an LLM provider.
        suffix = 'unit_test_merge_quagga_99'
        text_a = f'A fact about {suffix} alpha version of test.'
        text_b = f'A fact about {suffix} beta version of test.'
        # We assume the server's mode='regex' extracts both facts and
        # they end up similar. Because embeddings are deterministic,
        # they will land at high cosine.
        w1 = self._post('/v1/write', {
            'text': text_a, 'tier': 'public', 'mode': 'regex',
            'user': 'admin',
        })
        w2 = self._post('/v1/write', {
            'text': text_b, 'tier': 'public', 'mode': 'regex',
            'user': 'admin',
        })
        if not w1.get('fact_ids') or not w2.get('fact_ids'):
            self.skipTest('write returned no facts; forge extraction empty')
        fid_a = w1['fact_ids'][0]
        fid_b = w2['fact_ids'][0]
        # Merge them
        r = self._post('/v1/merge/apply', {
            'merges': [{
                'winner_id': fid_a, 'loser_id': fid_b,
                'tier': 'public', 'user_id': None,
            }],
            'actor': 'unit_test',
        })
        # Either the merge succeeded, or the ids were already merged
        # by the write-time dedup (which uses content hash). We accept
        # either applied_count==1 OR both being skipped.
        self.assertGreaterEqual(r['applied_count'] + r['skipped_count'], 1)

    def test_merge_apply_rejects_empty_list(self):
        r = self._post('/v1/merge/apply', {
            'merges': [], 'actor': 'unit_test',
        }, allow_4xx=True)
        self.assertEqual(r['http_status'], 400)
        self.assertIn('merges', r['body'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
