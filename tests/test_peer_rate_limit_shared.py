"""Cross-process test for the per-peer PPS rate limit.

The budget belongs to the PEER, not to the process that happens to serve it.
While the counter lived in a module-level dict, every worker process had its
own copy of it, so N workers meant N x 1000 requests per peer per 24h and a
replayed request could be replayed once per worker.

These tests spawn a real second interpreter against the same ASTOR_DIR and
assert that both processes draw from one shared allowance.

Never touches the live runtime: ASTOR_DIR is pointed at a temp dir.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CAP = 5

# Child process body: consume n units of a peer's budget, print how many
# were allowed.
_CHILD_SRC = """
import sys
from astor_memory._internal import peer_rate_limit as prl

peer_id = sys.argv[1]
n = int(sys.argv[2])
allowed = 0
for _ in range(n):
    ok, _count, _retry = prl.check_and_consume(peer_id)
    if ok:
        allowed += 1
print(allowed)
"""


class TestPeerBudgetIsPerPeerNotPerProcess(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self._old_astor_dir = os.environ.get("ASTOR_DIR")
        self._old_cap = os.environ.get("ASTOR_PEER_RATE_LIMIT_PER_24H")
        os.environ["ASTOR_DIR"] = str(self.tmpdir)
        os.environ["ASTOR_PEER_RATE_LIMIT_PER_24H"] = str(CAP)
        from astor_memory._internal import peer_rate_limit as prl
        prl._BUCKET.clear()

    def tearDown(self):
        from astor_memory._internal import peer_rate_limit as prl
        try:
            prl._BUCKET.clear()
        except Exception:
            pass
        if self._old_astor_dir is None:
            os.environ.pop("ASTOR_DIR", None)
        else:
            os.environ["ASTOR_DIR"] = self._old_astor_dir
        if self._old_cap is None:
            os.environ.pop("ASTOR_PEER_RATE_LIMIT_PER_24H", None)
        else:
            os.environ["ASTOR_PEER_RATE_LIMIT_PER_24H"] = self._old_cap
        self._tmp.cleanup()

    def _consume(self, peer_id, n=1):
        from astor_memory._internal import peer_rate_limit as prl
        return [prl.check_and_consume(peer_id) for _ in range(n)]

    def _child(self, peer_id, n):
        """Run a second interpreter against the same ASTOR_DIR."""
        env = dict(os.environ)
        env["ASTOR_DIR"] = str(self.tmpdir)
        env["ASTOR_PEER_RATE_LIMIT_PER_24H"] = str(CAP)
        env["PYTHONPATH"] = (
            str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""))
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SRC, peer_id, str(n)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(f"child process failed:\n{proc.stderr}")
        return int(proc.stdout.strip().splitlines()[-1])

    def test_child_shares_the_same_allowance(self):
        """A second process must not get a fresh budget for the same peer."""
        pid = "astor:" + "a" * 32
        self._consume(pid, 2)
        allowed_in_child = self._child(pid, 5)
        self.assertEqual(
            allowed_in_child, CAP - 2,
            "child process drew a fresh budget instead of sharing the peer's")

    def test_child_is_denied_after_parent_exhausts_budget(self):
        pid = "astor:" + "b" * 32
        for _ in range(CAP):
            allowed, _, _ = self._consume(pid)[0]
            self.assertTrue(allowed)
        self.assertEqual(self._child(pid, 1), 0,
                         "child process was allowed past the shared cap")
        from astor_memory._internal import peer_rate_limit as prl
        self.assertEqual(prl.snapshot(pid)["count"], CAP)

    def test_parent_sees_child_usage(self):
        """The shared counter is visible to every process, not just the writer."""
        pid = "astor:" + "c" * 32
        self.assertEqual(self._child(pid, 3), 3)
        from astor_memory._internal import peer_rate_limit as prl
        self.assertEqual(prl.snapshot(pid)["count"], 3)

    def test_different_peer_has_its_own_allowance(self):
        pid_a = "astor:" + "d" * 32
        pid_b = "astor:" + "e" * 32
        self.assertEqual(self._child(pid_a, CAP), CAP)
        allowed, count, _ = self._consume(pid_b)[0]
        self.assertTrue(allowed)
        self.assertEqual(count, 1)

    def test_counter_lives_in_astor_dir(self):
        """The shared counter is on disk under ASTOR_DIR, not in one process."""
        from astor_memory._internal import peer_rate_limit as prl
        self._consume("astor:" + "f" * 32)
        identity = self.tmpdir / "identity"
        self.assertTrue(identity.is_dir(),
                        f"no shared counter under {self.tmpdir}")
        self.assertTrue(list(identity.glob("*.db")),
                        "shared counter db missing under ASTOR_DIR")


if __name__ == "__main__":
    unittest.main()
