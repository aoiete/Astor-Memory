"""tests/test_peer_fanout_switch.py — the node-level auto peer-search switch.

The switch decides whether /v1/read falls back to the peer network when
local recall found nothing. Three things have to hold:

  1. Default is ON (an operator with peers configured expects them used).
  2. The env var overrides the stored decision.
  3. A per-request body flag overrides both.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory._internal import peer_fanout  # noqa: E402


class TestPeerFanoutSwitch(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old_env = os.environ.get('ASTOR_DIR')
        self._old_flag = os.environ.get('ASTOR_PEER_FANOUT_DEFAULT')
        os.environ['ASTOR_DIR'] = str(self.dir)
        os.environ.pop('ASTOR_PEER_FANOUT_DEFAULT', None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_env is None:
            os.environ.pop('ASTOR_DIR', None)
        else:
            os.environ['ASTOR_DIR'] = self._old_env
        if self._old_flag is None:
            os.environ.pop('ASTOR_PEER_FANOUT_DEFAULT', None)
        else:
            os.environ['ASTOR_PEER_FANOUT_DEFAULT'] = self._old_flag
        self._tmp.cleanup()

    def test_default_is_on(self):
        self.assertTrue(peer_fanout.is_enabled())
        self.assertEqual(peer_fanout.status()['source'], 'default')

    def test_set_enabled_persists_across_reload(self):
        peer_fanout.set_enabled(False)
        self.assertFalse(peer_fanout.is_enabled())
        self.assertEqual(peer_fanout.status()['source'], 'dashboard')
        # a fresh read of the file (simulating a restart)
        self.assertFalse(peer_fanout._load())
        peer_fanout.set_enabled(True)
        self.assertTrue(peer_fanout._load())

    def test_env_overrides_stored(self):
        peer_fanout.set_enabled(False)
        os.environ['ASTOR_PEER_FANOUT_DEFAULT'] = '1'
        self.assertTrue(peer_fanout.is_enabled())
        self.assertEqual(peer_fanout.status()['source'], 'env')
        os.environ['ASTOR_PEER_FANOUT_DEFAULT'] = '0'
        self.assertFalse(peer_fanout.is_enabled())

    def test_state_file_lives_under_identity(self):
        peer_fanout.set_enabled(True)
        p = self.dir / 'identity' / 'peer_fanout.json'
        self.assertTrue(p.exists())
        self.assertEqual(json.loads(p.read_text())['enabled'], True)

    def test_broken_state_file_falls_back_to_default(self):
        (self.dir / 'identity').mkdir(parents=True, exist_ok=True)
        (self.dir / 'identity' / 'peer_fanout.json').write_text('{not json')
        self.assertIsNone(peer_fanout._load())
        self.assertTrue(peer_fanout.is_enabled())


if __name__ == '__main__':
    unittest.main()
