"""Tests for v1.15.27 (2026-09-28) - Ship K: PPS topic-aware dispatch."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from astor_memory._internal.test_state import astor_close_all_test_state


if __name__ == "__main__":
    unittest.main()


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown_tmp(self):
        astor_close_all_test_state()
        try:
            from astor_memory._internal.audit_logger import _reset_audit_conn
            _reset_audit_conn()
        except Exception: pass
        try:
            from astor_memory.nest import lex_index as _lex_mod
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
        except Exception: pass
        try:
            from astor_memory.nest.vector_store import astor_reset_nest
            astor_reset_nest()
        except Exception: pass
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
            from astor_memory._internal.peer_relationships import close_all_connections
            close_all_connections()
        except Exception: pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception: pass
        try: self._tmp.cleanup()
        except Exception: pass


class TestSelectSearchTargetsTopic(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, set_topic,
        )
        init_identity(str(self.tmpdir))
        # 3 friends
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        add_peer(
            "astor:" + "b" * 32, kind="friend", trust=80,
            alias="bob", endpoint="http://bob.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        add_peer(
            "astor:" + "c" * 32, kind="friend", trust=80,
            alias="charlie", endpoint="http://charlie.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        # alice knows "poker" (weight 0.8), bob knows "fortune" (0.7),
        # charlie knows both (0.6 + 0.9)
        set_topic("poker", "astor:" + "a" * 32, weight=0.8)
        set_topic("fortune", "astor:" + "b" * 32, weight=0.7)
        set_topic("poker", "astor:" + "c" * 32, weight=0.6)
        set_topic("fortune", "astor:" + "c" * 32, weight=0.9)

    def tearDown(self):
        self.tearDown_tmp()

    def test_no_topic_returns_all_eligible(self):
        from astor_memory._internal.peer_search import select_search_targets
        targets = select_search_targets(self._all_peers())
        self.assertEqual(len(targets), 3)

    def test_topic_poker_filters_correctly(self):
        from astor_memory._internal.peer_search import select_search_targets
        targets = select_search_targets(
            self._all_peers(), topic="poker", topic_min_weight=0.5,
        )
        # alice (0.8) + charlie (0.6) = 2; bob (no poker) excluded
        pids = sorted([t["peer_id"] for t in targets])
        self.assertEqual(pids, sorted([
            "astor:" + "a" * 32, "astor:" + "c" * 32,
        ]))

    def test_topic_min_weight_filters_correctly(self):
        from astor_memory._internal.peer_search import select_search_targets
        targets = select_search_targets(
            self._all_peers(), topic="poker", topic_min_weight=0.7,
        )
        # Only alice (0.8); charlie (0.6) excluded
        pids = [t["peer_id"] for t in targets]
        self.assertEqual(pids, ["astor:" + "a" * 32])

    def test_topic_no_match_returns_empty(self):
        from astor_memory._internal.peer_search import select_search_targets
        targets = select_search_targets(
            self._all_peers(), topic="nonexistent_topic",
            topic_min_weight=0.5,
        )
        self.assertEqual(targets, [])

    def test_topic_with_low_min_weight_includes_all(self):
        from astor_memory._internal.peer_search import select_search_targets
        # Min weight 0.01 includes anyone with the topic at any weight
        targets = select_search_targets(
            self._all_peers(), topic="poker", topic_min_weight=0.01,
        )
        pids = sorted([t["peer_id"] for t in targets])
        self.assertEqual(pids, sorted([
            "astor:" + "a" * 32, "astor:" + "c" * 32,
        ]))

    def _all_peers(self):
        from astor_memory._internal.peer_relationships import list_peers
        return list_peers()


class TestDispatchPeerFanoutTopic(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, set_topic,
        )
        init_identity(str(self.tmpdir))
        # Two friends, only one knows "poker"
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        add_peer(
            "astor:" + "b" * 32, kind="friend", trust=80,
            alias="bob", endpoint="http://bob.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        set_topic("poker", "astor:" + "a" * 32, weight=0.9)

    def tearDown(self):
        self.tearDown_tmp()

    def test_dispatch_returns_no_targets_when_no_friends_know_topic(self):
        # Both friends don't know "fortune", so fanout returns 0 targets
        from astor_memory._internal.peer_recall import dispatch_peer_fanout
        # No mock — just verify behavior (dispatch_search_to_peers would 
        # try real network for matching targets). With topic=fortune and 
        # only alice/bob (neither has fortune), the result has 0 peer hits.
        result = dispatch_peer_fanout(
            query="q", topic="fortune", topic_min_weight=0.5,
            actor_peer_id="astor:local",
        )
        # Either 0 results (because no targets) or empty per_peer list
        self.assertEqual(result["peer_count"], 0)
        self.assertEqual(result["per_peer"], [])
        # hint should mention topic filter
        self.assertIn("fortune", result.get("hint", ""))

    def test_dispatch_with_no_topic_includes_all_peers(self):
        # Without topic, all eligible friends go through (but the
        # dispatch_search_to_peers call would try real network, so we
        # only verify that the targets were selected — via select_search_targets)
        from astor_memory._internal.peer_recall import dispatch_peer_fanout
        from astor_memory._internal.peer_search import select_search_targets
        from astor_memory._internal.peer_relationships import list_peers
        targets = select_search_targets(list_peers(), topic=None)
        self.assertEqual(len(targets), 2)


class TestPeerRecallEndpointTopic(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        from astor_memory.server import create_app
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_topic_min_weight_parsed_from_query_string(self):
        # Force the empty target path by setting topic_min_weight=99
        resp = self.client.get(
            "/v1/peer/recall?q=test&topic=poker&topic_min_weight=99.0"
        )
        d = resp.get_json()
        self.assertEqual(d.get("mode"), "local_only")
        self.assertIn("topic=", d.get("hint", ""))







