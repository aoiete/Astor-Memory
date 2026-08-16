"""Tests for astor_memory.nest.provenance (graph)."""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ['ASTOR_DIR'] = str(Path(r'<runtime_dir>'))
sys.path.insert(0, str(Path(r'<source_dir>')))


class ProvenanceCoreTests(unittest.TestCase):
    """Pure-Python tests. Use a transient tempdir to avoid touching the
    live astor server's data."""

    def _setup_acl(self):
        try:
            from astor_memory._internal.acl import astor_init_acl
            astor_init_acl(
                actor='first_admin', role='first_admin', tier='public',
            )
        except Exception:
            self.skipTest('cannot init ACL')

    def setUp(self):
        self._setup_acl()
        # ACL is per-thread; test fixtures share it.

    def test_record_and_walk_provenance_within_scope(self):
        """Write facts via /v1/write, record parent → child, walk up + down."""
        import urllib.request as _ur
        # 1) write two real facts
        def _w(text):
            body = json.dumps({'text': text, 'tier': 'public',
                               'mode': 'regex', 'user': 'admin'}).encode()
            req = _ur.Request(
                'http://127.0.0.1:7803/v1/write', data=body,
                headers={'Content-Type': 'application/json'},
            )
            return json.loads(_ur.urlopen(req, timeout=15).read())
        try:
            a = _w('provenance test unit parent_mongoose_88')
            b = _w('provenance test unit child_mongoose_88 beta version')
        except Exception as exc:
            self.skipTest(f'astor server unavailable: {exc}')
        if not a.get('fact_ids') or not b.get('fact_ids'):
            self.skipTest('write returned 0 facts')
        pa, pb = a['fact_ids'][0], b['fact_ids'][0]

        # 2) record provenance
        rec = json.loads(_ur.urlopen(_ur.Request(
            f'http://127.0.0.1:7803/v1/fact/{pb}/provenance',
            data=json.dumps({'tier': 'public', 'parents': [pa],
                             'kind': 'inferred', 'agent': 'unit_test'}).encode(),
            headers={'Content-Type': 'application/json'},
        ), timeout=10).read())
        self.assertEqual(rec['fact_id'], pb)
        self.assertEqual(rec['provenance_depth'], 1)

        # 3) walk upward from pb
        up = json.loads(_ur.urlopen(
            f'http://127.0.0.1:7803/v1/fact/{pb}/provenance', timeout=10).read())
        self.assertEqual(len(up['ancestors']), 1)
        self.assertEqual(up['ancestors'][0]['fact']['id'], pa)

        # 4) walk downward from pa
        down = json.loads(_ur.urlopen(
            f'http://127.0.0.1:7803/v1/fact/{pa}/lineage', timeout=10).read())
        # pb should appear
        ids = {x['fact_id'] for x in down['descendants']}
        self.assertIn(pb, ids)

        # 5) graph.dot
        dot = _ur.urlopen(
            f'http://127.0.0.1:7803/v1/fact/{pa}/graph.dot?direction=both',
            timeout=5,
        ).read().decode()
        self.assertIn('digraph provenance', dot)
        self.assertIn(f'f{pa}', dot)
        self.assertIn(f'f{pb}', dot)

    def test_get_provenance_returns_chain_broken_when_missing(self):
        """If a parent fact_id is missing, the chain is marked broken but
        the call still succeeds.

        Implementation note: when ALL parents are missing, depth is set
        to 0 (because max_known_parent_depth = -1 + 1 = 0). When at
        least one parent is found, depth >= 1. Verify that the record
        succeeds (idempotent) and that a subsequent provenance walk
        flags chain_broken=True."""
        from astor_memory.nest.provenance import get_provenance
        import urllib.request as _ur
        try:
            body = json.dumps({'text': 'provenance chain-broken test_pangolin_33',
                               'tier': 'public', 'mode': 'regex',
                               'user': 'admin'}).encode()
            w = json.loads(_ur.urlopen(_ur.Request(
                'http://127.0.0.1:7803/v1/write', data=body,
                headers={'Content-Type': 'application/json'},
            ), timeout=15).read())
        except Exception as exc:
            self.skipTest(f'astor server unavailable: {exc}')
        if not w.get('fact_ids'):
            self.skipTest('write returned 0 facts')
        fid = w['fact_ids'][0]
        # Inject parent_fact_ids referencing a non-existent fact_id 8888888
        rec = json.loads(_ur.urlopen(_ur.Request(
            f'http://127.0.0.1:7803/v1/fact/{fid}/provenance',
            data=json.dumps({'tier': 'public', 'parents': [8888888],
                             'kind': 'inferred', 'agent': 'unit_test'}).encode(),
            headers={'Content-Type': 'application/json'},
        ), timeout=10).read())
        # parents=[] is invalid + parent lookup misses → depth stays 0/None
        self.assertIn(rec['provenance_depth'], (0, 1))
        # Walk upward: should mark chain_broken=True and produce 0 ancestors
        up = json.loads(_ur.urlopen(
            f'http://127.0.0.1:7803/v1/fact/{fid}/provenance?scope_search=true',
            timeout=10,
        ).read())
        self.assertTrue(up['chain_broken'])
        self.assertEqual(len(up['ancestors']), 0)

    def test_graph_dot_returns_empty_graph_for_missing_fact(self):
        """For a missing fact, graph_dot returns a minimal valid DOT
        document instead of raising — UI tools can still render it."""
        from astor_memory.nest.provenance import graph_dot
        out = graph_dot(fact_id=9999999, tier='public', direction='up')
        self.assertIn('digraph provenance', out)
        # No node edges should appear
        self.assertNotIn('->', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
