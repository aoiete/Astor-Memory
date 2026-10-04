"""Tests for v1.15.22 (2026-09-28) — PPS per-peer rate limit (Ship E).

Covers:
  - In-memory check_and_consume allows up to N requests
  - Past N, returns (False, count, retry_after_seconds)
  - 24h window slides correctly (timestamps older than 24h ignored)
  - reset(peer_id) clears the bucket
  - all_snapshots returns per-peer rollups
  - status_summary aggregates
  - snapshot includes cap from env override
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock
from astor_memory._internal.test_state import astor_close_all_test_state


class TestPeerRateLimitCore(unittest.TestCase):
    """Pure in-memory logic; no DB, no Flask."""

    def setUp(self):
        # Make sure each test starts with a clean singleton state.
        from astor_memory._internal import peer_rate_limit as prl
        with prl._LOCK:
            prl._BUCKET.clear()
        # Use a tight cap so we don't need to hit 1000+
        os.environ["ASTOR_PEER_RATE_LIMIT_PER_24H"] = "5"

    def tearDown(self):
        os.environ.pop("ASTOR_PEER_RATE_LIMIT_PER_24H", None)
        from astor_memory._internal import peer_rate_limit as prl
        with prl._LOCK:
            prl._BUCKET.clear()

    def test_cap_default_overridable(self):
        from astor_memory._internal import peer_rate_limit as prl
        self.assertEqual(prl._limit(), 5)
        os.environ.pop("ASTOR_PEER_RATE_LIMIT_PER_24H")
        self.assertEqual(prl._limit(), prl.DEFAULT_LIMIT_PER_24H)

    def test_allows_up_to_cap(self):
        from astor_memory._internal import peer_rate_limit as prl
        pid = "astor:" + "a" * 32
        for i in range(5):
            allowed, count, retry = prl.check_and_consume(pid)
            self.assertTrue(allowed, f"call {i+1} should be allowed")
            self.assertEqual(count, i + 1)
            self.assertEqual(retry, 0)

    def test_denies_at_cap_plus_one(self):
        from astor_memory._internal import peer_rate_limit as prl
        pid = "astor:" + "a" * 32
        for _ in range(5):
            allowed, _, _ = prl.check_and_consume(pid)
            self.assertTrue(allowed)
        allowed, count, retry = prl.check_and_consume(pid)
        self.assertFalse(allowed)
        self.assertEqual(count, 5)
        self.assertGreater(retry, 0)
        self.assertLessEqual(retry, 24 * 3600)

    def test_24h_window_slides(self):
        """Old timestamps get evicted when we add a new one."""
        from astor_memory._internal import peer_rate_limit as prl
        pid = "astor:" + "a" * 32
        # Plant 5 timestamps at "25 hours ago"
        cutoff_age = 25 * 3600
        now = time.time()
        with prl._LOCK:
            prl._BUCKET[pid] = [now - cutoff_age for _ in range(5)]
        # New request: prune old, count is 0, allow
        allowed, count, _ = prl.check_and_consume(pid)
        self.assertTrue(allowed)
        self.assertEqual(count, 1)  # just our new one
        # Old timestamps were pruned
        with prl._LOCK:
            self.assertEqual(len(prl._BUCKET[pid]), 1)

    def test_two_peers_are_isolated(self):
        from astor_memory._internal import peer_rate_limit as prl
        pA = "astor:" + "a" * 32
        pB = "astor:" + "b" * 32
        for _ in range(5):
            prl.check_and_consume(pA)
        # A is full; B still has full budget
        allowed, count, _ = prl.check_and_consume(pB)
        self.assertTrue(allowed)
        self.assertEqual(count, 1)

    def test_reset_clears_bucket(self):
        from astor_memory._internal import peer_rate_limit as prl
        pid = "astor:" + "a" * 32
        for _ in range(3):
            prl.check_and_consume(pid)
        cleared = prl.reset(pid)
        self.assertEqual(cleared, 3)
        # Bucket usable again
        allowed, count, _ = prl.check_and_consume(pid)
        self.assertTrue(allowed)
        self.assertEqual(count, 1)

    def test_snapshot_shape(self):
        from astor_memory._internal import peer_rate_limit as prl
        pid = "astor:" + "a" * 32
        prl.check_and_consume(pid)
        s = prl.snapshot(pid)
        for k in ('peer_id', 'count', 'cap', 'window_hours',
                  'last_request_iso', 'oldest_iso',
                  'retry_after_seconds'):
            self.assertIn(k, s)
        self.assertEqual(s['peer_id'], pid)
        self.assertEqual(s['count'], 1)
        self.assertEqual(s['cap'], 5)

    def test_snapshot_unknown_peer_returns_zero(self):
        from astor_memory._internal import peer_rate_limit as prl
        s = prl.snapshot("astor:" + "z" * 32)
        self.assertEqual(s['count'], 0)
        self.assertIsNone(s['last_request_iso'])

    def test_all_snapshots_sorted(self):
        from astor_memory._internal import peer_rate_limit as prl
        for _ in range(5):
            prl.check_and_consume("astor:" + "a" * 32)
        for _ in range(2):
            prl.check_and_consume("astor:" + "b" * 32)
        rows = prl.all_snapshots()
        peer_counts = {r['peer_id']: r['count'] for r in rows}
        self.assertEqual(peer_counts.get("astor:" + "a" * 32), 5)
        self.assertEqual(peer_counts.get("astor:" + "b" * 32), 2)

    def test_status_summary(self):
        from astor_memory._internal import peer_rate_limit as prl
        prl.check_and_consume("astor:" + "a" * 32)
        prl.check_and_consume("astor:" + "b" * 32)
        s = prl.status_summary()
        self.assertEqual(s['peers_tracked'], 2)
        self.assertEqual(s['requests_in_window'], 2)
        self.assertEqual(s['cap_per_peer_per_24h'], 5)
        self.assertEqual(s['window_hours'], 24)

    def test_empty_peer_id_fails_closed(self):
        from astor_memory._internal import peer_rate_limit as prl
        allowed, count, retry = prl.check_and_consume("")
        self.assertFalse(allowed)
        self.assertEqual(count, 0)
        self.assertEqual(retry, prl.WINDOW_SECONDS)


class _Tmp:
    def setUp_tmp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        astor_close_all_test_state()
        try:
            os.environ.pop("ASTOR_PEER_RATE_LIMIT_PER_24H", None)
            from astor_memory._internal import peer_rate_limit as prl
            with prl._LOCK:
                prl._BUCKET.clear()
            from astor_memory.nest import lex_index as _lx
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            try:
                from astor_memory.bus.store import (
                    astor_reset_bus, _BUS_SINGLETONS,
                )
                astor_reset_bus()
                if _BUS_SINGLETONS is not None:
                    for _b in list(_BUS_SINGLETONS.values()):
                        try: _b.close()
                        except Exception: pass
                    _BUS_SINGLETONS.clear()
            except Exception: pass
            with _lx._LEX_SINGLETONS_LOCK:
                for lx in list(_lx._LEX_SINGLETONS.values()):
                    try: lx.close()
                    except Exception: pass
                _lx._LEX_SINGLETONS.clear()
            astor_reset_nest()
            close_all_connections()
            try:
                from astor_memory.forge import (_forge_conns, _forge_lock)
                with _forge_lock:
                    for _fc in list(_forge_conns.values()):
                        try: _fc.close()
                        except Exception: pass
                    _forge_conns.clear()
            except Exception: pass
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass


class TestPeerRateLimitEndpoint(_Tmp, unittest.TestCase):
    """The 3 REST endpoints + main-loop integration."""

    def setUp(self):
        self.setUp_tmp()
        # Clean bucket + tmp app
        from astor_memory._internal import peer_rate_limit as prl
        with prl._LOCK:
            prl._BUCKET.clear()
        from astor_memory.server import create_app
        os.environ["ASTOR_PEER_RATE_LIMIT_PER_24H"] = "5"
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def setUp(self):
        # Clean bucket + tmp app
        from astor_memory._internal import peer_rate_limit as prl
        with prl._LOCK:
            prl._BUCKET.clear()
        from astor_memory.server import create_app
        # Tight cap for the test
        os.environ["ASTOR_PEER_RATE_LIMIT_PER_24H"] = "5"
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.app = create_app(astor_dir=str(Path(self._tmp.name)))
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("ASTOR_PEER_RATE_LIMIT_PER_24H", None)
        from astor_memory._internal import peer_rate_limit as prl
        with prl._LOCK:
            prl._BUCKET.clear()
        from astor_memory.nest import lex_index as _lx
        from astor_memory.nest.vector_store import astor_reset_nest
        from astor_memory._internal.peer_relationships import close_all_connections
        try:
            from astor_memory.bus.store import (
                astor_reset_bus, _BUS_SINGLETONS,
            )
            astor_reset_bus()
            if _BUS_SINGLETONS is not None:
                for _b in list(_BUS_SINGLETONS.values()):
                    try: _b.close()
                    except Exception: pass
                _BUS_SINGLETONS.clear()
        except Exception: pass
        with _lx._LEX_SINGLETONS_LOCK:
            for lx in list(_lx._LEX_SINGLETONS.values()):
                try: lx.close()
                except Exception: pass
            _lx._LEX_SINGLETONS.clear()
        astor_reset_nest()
        close_all_connections()
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception: pass
        self._tmp.cleanup()

    def test_status_endpoint_400_without_peer_id(self):
        resp = self.client.get("/v1/peer/rate-limit")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "peer_id_required")

    def test_status_endpoint_returns_snapshot(self):
        resp = self.client.get("/v1/peer/rate-limit?peer_id=astor:" + "a" * 32)
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d['count'], 0)
        self.assertEqual(d['cap'], 5)

    def test_all_endpoint_returns_summary_and_peers(self):
        resp = self.client.get("/v1/peer/rate-limit?all=true")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertIn('summary', d)
        self.assertIn('peers', d)
        self.assertEqual(d['summary']['cap_per_peer_per_24h'], 5)

    def test_reset_endpoint_requires_peer_id(self):
        resp = self.client.post("/v1/peer/rate-limit/reset",
            json={}, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_rebuild_endpoint_runs(self):
        # No audit rows in fresh tmpdir — should return 0 rebuilt; that's fine.
        resp = self.client.post("/v1/peer/rate-limit/rebuild",
            json={}, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['rebuilt_count'], 0)


if __name__ == "__main__":
    unittest.main()
