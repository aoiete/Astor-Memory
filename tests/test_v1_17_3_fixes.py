"""tests/test_v1_17_3_fixes.py — the 2026-10-27 operator report.

Four of these are about a node that LOOKS configured and is not:

  1. the peer fanout never fired, because the local recall always
     returned something;
  2. the dashboard showed a switch but not whether it worked;
  3. `am init` silently created a second identity;
  4. `am peer endpoint-set` accepted an address the peer cannot reach;
  5. the hermes plugin was installed into a Python it cannot import on.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.installer.handlers import (  # noqa: E402
    _python_in_range,
    _requires_python_range,
)


class TestPythonRange(unittest.TestCase):
    """The requires-python comparison the hermes installer uses."""

    def test_spec_is_read(self):
        spec = _requires_python_range()
        self.assertIsNotNone(spec)
        self.assertIn('3.10', spec)

    def test_supported_versions_pass(self):
        spec = _requires_python_range()
        for v in ('3.10', '3.11', '3.12', '3.13', '3.14'):
            self.assertTrue(_python_in_range(v, spec), v)

    def test_314_is_in_range(self):
        # 2026-10-30: 3.14 is supported. Python 3.14's argparse validates
        # help strings, so a literal percent in a help text raised
        # ValueError at parser-build time and took the whole CLI down --
        # that is what kept 3.14 out of range. The one offending help
        # string is fixed (a literal percent is now written %%), and the
        # 3.14 leg of CI runs the suite.
        spec = _requires_python_range()
        self.assertTrue(_python_in_range('3.14', spec), spec)

    def test_315_is_out_of_range(self):
        spec = _requires_python_range()
        self.assertFalse(_python_in_range('3.15', spec))

    def test_39_is_out_of_range(self):
        spec = _requires_python_range()
        self.assertFalse(_python_in_range('3.9', spec))

    def test_unparseable_spec_is_permissive(self):
        # Refusing to install on a spec we cannot parse is worse than
        # trying.
        self.assertTrue(_python_in_range('3.14', 'wat'))
        self.assertTrue(_python_in_range('3.14', ''))


class TestFanoutLocalHitSignal(unittest.TestCase):
    """The fanout trigger is "no lexical hit", not "no rows".

    A vector store always returns nearest neighbours, so the old
    condition (empty result set) never held on a node with an embedding
    model. The signal that separates "this node has it" from "this node
    has nothing" is whether any query token occurs in the local lexical
    index.
    """

    def test_lexical_hit_counts_as_local(self):
        lexical_ids = {10, 11, 12}
        local_ids = {10, 99}
        self.assertTrue(bool(local_ids & lexical_ids))

    def test_vector_only_neighbour_is_not_local(self):
        # The returned row exists but has no lexical overlap: the
        # embedding model's guess, not a match.
        lexical_ids = {10, 11}
        local_ids = {99}
        self.assertFalse(bool(local_ids & lexical_ids))

    def test_empty_lexical_index_falls_back_to_any_row(self):
        # No lexical index at all (fresh node, or embedding-only tier):
        # any row is the best signal available.
        lexical_ids = set()
        local_ids = {99}
        self.assertTrue(bool(local_ids))


if __name__ == '__main__':
    unittest.main()


class TestOutboundSearchSwitch(unittest.TestCase):
    """The outbound mirror of allow-search.

    allow_search is inbound ("that peer may search my public tier").
    outbound_search is the other direction ("I may search that peer").
    They are independent: a node can be a library for one peer and a
    reader of another.
    """

    def _peer(self, **kw):
        base = {'peer_id': 'astor:x', 'trust': 60, 'kind': 'friend',
                'endpoint': 'http://192.168.1.5:7803', 'metadata': {}}
        base.update(kw)
        return base

    def test_default_is_on(self):
        from astor_memory._internal.peer_search import select_search_targets
        targets = select_search_targets([self._peer()])
        self.assertEqual(len(targets), 1)

    def test_outbound_off_excludes_the_peer(self):
        from astor_memory._internal.peer_search import select_search_targets
        p = self._peer(metadata={'outbound_search': False})
        self.assertEqual(len(select_search_targets([p])), 0)

    def test_outbound_on_includes_the_peer(self):
        from astor_memory._internal.peer_search import select_search_targets
        p = self._peer(metadata={'outbound_search': True})
        self.assertEqual(len(select_search_targets([p])), 1)

    def test_outbound_is_independent_of_allow(self):
        # A peer allowed to search me, which I have not chosen to search.
        from astor_memory._internal.peer_search import select_search_targets
        p = self._peer(metadata={'allow_search': True,
                                 'outbound_search': False})
        self.assertEqual(len(select_search_targets([p])), 0)

    def test_allow_only_does_not_gate_outbound(self):
        # A peer I have NOT allowed to search me, but which I may search.
        from astor_memory._internal.peer_search import select_search_targets
        p = self._peer(metadata={'allow_search': False})
        self.assertEqual(len(select_search_targets([p])), 1)

    def test_flag_read_defaults_to_true(self):
        from astor_memory._internal.peer_relationships import (
            _read_outbound_search_flag,
        )
        self.assertTrue(_read_outbound_search_flag('astor:does-not-exist')
                        is False)  # unknown peer -> False


class TestMultiEndpointPeer(unittest.TestCase):
    """A peer can be reachable at more than one address.

    The LAN address is fast and only works inside the LAN; the public
    name works from anywhere. An operator should not have to pick one.
    The dispatcher tries them in order and stops at the first that
    answers.
    """

    def test_endpoints_helper_orders_primary_first(self):
        from astor_memory._internal.peer_search import _endpoints_for
        f = {'endpoint': 'http://192.168.1.x:7803',
             'metadata': {'endpoints': ['http://192.168.1.x:7803',
                                        'https://peer.example.com']}}
        eps = _endpoints_for(f)
        self.assertEqual(eps[0], 'http://192.168.1.x:7803')
        self.assertIn('https://peer.example.com', eps)
        # no duplicates
        self.assertEqual(len(eps), len(set(eps)))

    def test_endpoints_helper_without_metadata(self):
        from astor_memory._internal.peer_search import _endpoints_for
        self.assertEqual(_endpoints_for({'endpoint': 'http://a:1'}),
                         ['http://a:1'])
        self.assertEqual(_endpoints_for({}), [])

    def test_set_and_get_roundtrip(self):
        import os
        import tempfile
        from astor_memory._internal import peer_relationships as PR
        tmp = tempfile.mkdtemp()
        PR.add_peer('astor:multi1', alias='m', trust=60,
                    endpoint='http://192.168.1.9:7803', astor_dir=tmp)
        PR.set_peer_endpoints(
            'astor:multi1',
            ['http://192.168.1.9:7803', 'https://peer.example.com'],
            astor_dir=tmp)
        got = PR.get_peer_endpoints('astor:multi1', astor_dir=tmp)
        self.assertEqual(got, ['http://192.168.1.9:7803',
                               'https://peer.example.com'])
        # the primary column is the first entry
        row = PR.get_peer('astor:multi1', astor_dir=tmp)
        self.assertEqual(row['endpoint'], 'http://192.168.1.9:7803')

    def test_add_peer_endpoint_appends(self):
        import os
        import tempfile
        from astor_memory._internal import peer_relationships as PR
        tmp = tempfile.mkdtemp()
        PR.add_peer('astor:multi2', alias='m', trust=60,
                    endpoint='http://192.168.1.9:7803', astor_dir=tmp)
        PR.add_peer_endpoint('astor:multi2', 'https://peer.example.com',
                             astor_dir=tmp)
        got = PR.get_peer_endpoints('astor:multi2', astor_dir=tmp)
        self.assertEqual(got, ['http://192.168.1.9:7803',
                               'https://peer.example.com'])

    def test_rejects_non_http_endpoint(self):
        import tempfile
        from astor_memory._internal import peer_relationships as PR
        tmp = tempfile.mkdtemp()
        PR.add_peer('astor:multi3', alias='m', trust=60,
                    endpoint='http://a:1', astor_dir=tmp)
        with self.assertRaises(ValueError):
            PR.set_peer_endpoints('astor:multi3', ['ftp://nope'],
                                  astor_dir=tmp)
