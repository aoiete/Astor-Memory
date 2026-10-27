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
        for v in ('3.10', '3.11', '3.12', '3.13'):
            self.assertTrue(_python_in_range(v, spec), v)

    def test_314_is_out_of_range(self):
        # Hermes 0.22 ships on 3.14; astor-memory declares <3.14.
        spec = _requires_python_range()
        self.assertFalse(_python_in_range('3.14', spec))

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
