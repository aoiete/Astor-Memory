"""Tests for v1.15.23 (2026-09-28) — PPS trust auto-update (Ship F).

Covers:
  - bump_trust: adds delta, clamps 0..100, returns new trust
  - bump_trust: returns None when peer unknown
  - record_peer_error: increments consecutive_error_count metadata field
  - clear_peer_errors: resets to 0
  - should_decay_trust: respects age + trust threshold
  - decay_peer_trust: applies -5 clamped at default
  - End-to-end: /v1/peer/adopt auto-bumps source peer trust on success
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
            astor_reset_nest()
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
            close_all_connections()
        except Exception:
            pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass






def _close_all_singletons():
    try:
        from astor_memory.nest import lex_index as _lex_mod
        from astor_memory.nest.vector_store import astor_reset_nest
        from astor_memory._internal import audit_logger as _al_mod
        with _lex_mod._LEX_SINGLETONS_LOCK:
            for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                try: lex.close()
                except Exception: pass
            _lex_mod._LEX_SINGLETONS.clear()
        astor_reset_nest()
        _al_mod._reset_audit_conn()
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

class TestTrustBumpHelpers(unittest.TestCase):
    """Pure logic tests against the in-memory sqlite store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)
        # Pre-create a friend with trust=70
        from astor_memory._internal.peer_relationships import (
            add_peer, get_peer, close_all_connections,
        )
        add_peer("astor:" + "a" * 32, kind="friend", trust=70,
                 endpoint="http://a.example.com:7803",
                 public_key="AAAA")
        add_peer("astor:" + "b" * 32, kind="blacklist", trust=0)
        # Order: singletons close BEFORE tmpdir cleanup (LIFO: tmp first).
        # We rely on the fact that addCleanup runs in LIFO order, so we
        # add tmp FIRST then singletons.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(close_all_connections)
        self.addCleanup(_close_all_singletons)

    def test_bump_trust_positive(self):
        from astor_memory._internal.peer_relationships import bump_trust
        new = bump_trust("astor:" + "a" * 32, 5)
        self.assertEqual(new, 75)

    def test_bump_trust_negative(self):
        from astor_memory._internal.peer_relationships import bump_trust
        new = bump_trust("astor:" + "a" * 32, -3)
        self.assertEqual(new, 67)

    def test_bump_trust_clamps_high(self):
        from astor_memory._internal.peer_relationships import bump_trust
        new = bump_trust("astor:" + "a" * 32, 100)  # 70 + 100 = 170, clamp
        self.assertEqual(new, 100)

    def test_bump_trust_clamps_low(self):
        from astor_memory._internal.peer_relationships import bump_trust
        new = bump_trust("astor:" + "a" * 32, -200)  # 70 - 200 = -130, clamp
        self.assertEqual(new, 0)

    def test_bump_trust_unknown_returns_none(self):
        from astor_memory._internal.peer_relationships import bump_trust
        new = bump_trust("astor:" + "z" * 32, 5)
        self.assertIsNone(new)

    def test_bump_trust_no_change_doesnt_write(self):
        from astor_memory._internal.peer_relationships import (
            bump_trust, get_peer,
        )
        # 70 + 0 = 70, no-op
        before = get_peer("astor:" + "a" * 32)["updated_at"]
        new = bump_trust("astor:" + "a" * 32, 0)
        self.assertEqual(new, 70)
        after = get_peer("astor:" + "a" * 32)["updated_at"]
        self.assertEqual(before, after)

    def test_record_peer_error_increments(self):
        from astor_memory._internal.peer_relationships import (
            record_peer_error, get_peer, _error_count_path,
        )
        n1 = record_peer_error("astor:" + "a" * 32)
        n2 = record_peer_error("astor:" + "a" * 32)
        n3 = record_peer_error("astor:" + "a" * 32)
        self.assertEqual([n1, n2, n3], [1, 2, 3])
        meta = get_peer("astor:" + "a" * 32)["metadata"]
        self.assertEqual(_error_count_path(meta), 3)

    def test_record_peer_error_caps_at_100(self):
        from astor_memory._internal.peer_relationships import record_peer_error
        # First one
        record_peer_error("astor:" + "a" * 32)
        # Now hammer it
        last = None
        for _ in range(150):
            last = record_peer_error("astor:" + "a" * 32)
        self.assertEqual(last, 100)

    def test_record_peer_error_unknown_returns_none(self):
        from astor_memory._internal.peer_relationships import record_peer_error
        self.assertIsNone(record_peer_error("astor:" + "z" * 32))

    def test_clear_peer_errors(self):
        from astor_memory._internal.peer_relationships import (
            record_peer_error, clear_peer_errors,
        )
        record_peer_error("astor:" + "a" * 32)
        record_peer_error("astor:" + "a" * 32)
        v = clear_peer_errors("astor:" + "a" * 32)
        self.assertEqual(v, 0)
        # Clear again should be idempotent
        v2 = clear_peer_errors("astor:" + "a" * 32)
        self.assertEqual(v2, 0)

    def test_should_decay_trust_false_when_recent(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, should_decay_trust,
        )
        p = get_peer("astor:" + "a" * 32)
        self.assertFalse(should_decay_trust(p))

    def test_should_decay_trust_true_when_stale_and_above_default(self):
        from astor_memory._internal.peer_relationships import (
            update_trust, should_decay_trust,
        )
        # Force updated_at to be 60 days ago
        from astor_memory._internal.peer_relationships import _get_conn
        old_ts = ((_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=60))
                   .strftime("%Y-%m-%dT%H:%M:%SZ"))
        con = _get_conn()
        con.execute(
            "UPDATE peer_relationships SET updated_at = ? WHERE peer_id = ?",
            (old_ts, "astor:" + "a" * 32),
        )
        con.commit()
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "a" * 32)
        self.assertTrue(should_decay_trust(p, days=30))

    def test_decay_peer_trust_applies_minus_5(self):
        from astor_memory._internal.peer_relationships import (
            update_trust, _get_conn, decay_peer_trust, get_peer,
        )
        # Force stale + trust 70
        old_ts = ((_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=60))
                   .strftime("%Y-%m-%dT%H:%M:%SZ"))
        con = _get_conn()
        con.execute(
            "UPDATE peer_relationships SET updated_at = ? WHERE peer_id = ?",
            (old_ts, "astor:" + "a" * 32),
        )
        con.commit()
        new = decay_peer_trust("astor:" + "a" * 32, days=30, default_trust=30)
        self.assertEqual(new, 65)  # 70 - 5

    def test_decay_peer_trust_clamps_at_default(self):
        from astor_memory._internal.peer_relationships import (
            update_trust, _get_conn, decay_peer_trust,
        )
        update_trust("astor:" + "a" * 32, 33)
        old_ts = ((_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=60))
                   .strftime("%Y-%m-%dT%H:%M:%SZ"))
        con = _get_conn()
        con.execute(
            "UPDATE peer_relationships SET updated_at = ? WHERE peer_id = ?",
            (old_ts, "astor:" + "a" * 32),
        )
        con.commit()
        # 33 - 5 = 28, but default is 30, so clamp at 30
        new = decay_peer_trust("astor:" + "a" * 32, days=30, default_trust=30)
        self.assertEqual(new, 30)


class TestAdoptEndpointTrustBump(_Tmp, unittest.TestCase):
    """End-to-end: POST /v1/peer/adopt auto-bumps source peer trust."""

    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        from astor_memory._internal.peer_relationships import add_peer
        os.environ["ASTOR_DIR"] = str(self.tmpdir)
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()
        # Pre-create a friend with trust=60
        add_peer("astor:" + "b" * 32, kind="friend", trust=60,
                 endpoint="http://b.example.com:7803",
                 public_key="BBBB")
        # Allow-search so the friend can be queried
        from astor_memory._internal.peer_relationships import set_allow_search
        set_allow_search("astor:" + "b" * 32, True)

    def tearDown(self):
        self.tearDown_tmp()

    def test_adopt_bumps_source_peer_trust(self):
        body = {
            "source_peer_id": "astor:" + "b" * 32,
            "tier": "source",
            "facts": [
                {"content": "R132 forbids fabrication in any agent output",
                 "kind": "rule", "tags": ["r132"], "importance": 0.9,
                 "fact_id": 99},
            ],
        }
        resp = self.client.post("/v1/peer/adopt", json=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["peer_trust_after_adopt"], 61)
        # Verify the row was actually updated
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "b" * 32)
        self.assertEqual(p["trust"], 61)

    def test_adopt_with_empty_facts_does_not_bump(self):
        body = {
            "source_peer_id": "astor:" + "b" * 32,
            "tier": "source",
            "facts": [
                {"content": "abc"},  # too short, will be skipped
            ],
        }
        resp = self.client.post("/v1/peer/adopt", json=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["skipped"], 1)
        # Trust unchanged (no adopt happened)
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "b" * 32)
        self.assertEqual(p["trust"], 60)

    def test_adopt_unknown_peer_does_not_bump(self):
        body = {
            "source_peer_id": "astor:" + "z" * 32,  # unknown
            "tier": "source",
            "facts": [
                {"content": "valid length fact for adopt", "kind": "fact"},
            ],
        }
        resp = self.client.post("/v1/peer/adopt", json=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        # written is still 1 (the fact was saved to local bus), but
        # peer_trust_after_adopt is None (unknown peer).
        self.assertEqual(d["count"], 1)
        self.assertIsNone(d["peer_trust_after_adopt"])


if __name__ == "__main__":
    unittest.main()
