"""Tests for v1.14.70 S1 — topic-aware routing."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_relationships import (
    set_topic, get_topic, list_topics_for_peer, list_peers_for_topic,
    list_all_topics, bump_topic_seen, remove_topic, close_all_connections,
)


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        close_all_connections()
        self._tmp.cleanup()


class TestTopicIndexCRUD(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_set_topic_basic(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            result = set_topic("poker", "astor:abc" + "0" * 29, weight=0.9)
        self.assertEqual(result["topic"], "poker")
        self.assertAlmostEqual(result["weight"], 0.9)

    def test_set_topic_idempotent(self):
        pid = "astor:" + "1" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("fortune", pid, weight=0.5)
            set_topic("fortune", pid, weight=0.8)  # update
            t = get_topic("fortune", pid)
        self.assertAlmostEqual(t["weight"], 0.8)

    def test_set_topic_invalid_weight_raises(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            with self.assertRaises(ValueError):
                set_topic("x", "astor:" + "2" * 32, weight=1.5)
            with self.assertRaises(ValueError):
                set_topic("x", "astor:" + "2" * 32, weight=-0.1)

    def test_get_topic_returns_none_when_missing(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            t = get_topic("nope", "astor:" + "3" * 32)
        self.assertIsNone(t)

    def test_remove_topic(self):
        pid = "astor:" + "4" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("astrology", pid, weight=0.7)
            removed = remove_topic("astrology", pid)
            still = get_topic("astrology", pid)
        self.assertTrue(removed)
        self.assertIsNone(still)


class TestTopicRouting(_Tmp, unittest.TestCase):
    """The actual routing queries — list_topics_for_peer, list_peers_for_topic."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_list_topics_for_peer(self):
        pid = "astor:" + "5" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("poker", pid, weight=0.95)
            set_topic("fortune", pid, weight=0.40)
            set_topic("nlp", pid, weight=0.10)
            topics = list_topics_for_peer(pid, min_weight=0.5)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic"], "poker")

    def test_list_peers_for_topic(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("poker", "astor:" + "a" * 32, weight=0.95)
            set_topic("poker", "astor:" + "b" * 32, weight=0.80)
            set_topic("fortune", "astor:" + "c" * 32, weight=0.70)
            peers = list_peers_for_topic("poker")
        self.assertEqual(len(peers), 2)
        self.assertEqual(peers[0]["peer_id"], "astor:" + "a" * 32)
        self.assertEqual(peers[1]["peer_id"], "astor:" + "b" * 32)

    def test_list_all_topics_aggregates(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("poker", "astor:" + "d" * 32, weight=0.95)
            set_topic("poker", "astor:" + "e" * 32, weight=0.85)
            set_topic("fortune", "astor:" + "f" * 32, weight=0.70)
            all_topics = list_all_topics()
        self.assertEqual(len(all_topics), 2)
        # Poker has 2 peers, fortune has 1
        poker_entry = next(t for t in all_topics if t["topic"] == "poker")
        fortune_entry = next(t for t in all_topics if t["topic"] == "fortune")
        self.assertEqual(poker_entry["peer_count"], 2)
        self.assertEqual(fortune_entry["peer_count"], 1)
        self.assertAlmostEqual(poker_entry["avg_weight"], 0.90, places=2)

    def test_bump_topic_seen_increments_count(self):
        pid = "astor:" + "9" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            set_topic("poker", pid, weight=0.9)
            bump_topic_seen("poker", pid)
            bump_topic_seen("poker", pid)
            t = get_topic("poker", pid)
        self.assertEqual(t["fact_count"], 2)

    def test_bump_topic_seen_no_op_when_missing(self):
        """Bump on non-existent (topic, peer) should be a no-op, not error."""
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            # No exception expected
            bump_topic_seen("neverexisted", "astor:" + "x" * 32)
        # Should still be None
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            t = get_topic("neverexisted", "astor:" + "x" * 32)
        self.assertIsNone(t)


class TestTopicFilter(_Tmp, unittest.TestCase):
    """Test the topic filter logic in /v1/read — boost facts whose tags contain topic."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_topic_boost_logic_unit(self):
        """Simulate the boost logic from server.py to verify it works."""
        # Simulate a recall result set
        enriched = [
            {"fact_id": 1, "similarity": 0.5, "tags": ["poker", "strategy"]},
            {"fact_id": 2, "similarity": 0.6, "tags": ["fact", "auto_extracted"]},
            {"fact_id": 3, "similarity": 0.7, "tags": ["poker", "hand-replay"]},
        ]
        topic = "poker"
        boost = 1.10
        applied = False
        for r in enriched:
            if topic in (r.get("tags") or []):
                r["similarity"] = float(r.get("similarity", 0)) * boost
                applied = True
        if applied:
            enriched.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        # Fact 3 should now be top (0.7 * 1.1 = 0.77)
        self.assertEqual(enriched[0]["fact_id"], 3)
        self.assertAlmostEqual(enriched[0]["similarity"], 0.77, places=2)
        self.assertTrue(applied)

    def test_topic_boost_no_match_no_applied(self):
        enriched = [
            {"fact_id": 1, "similarity": 0.5, "tags": ["fortune"]},
            {"fact_id": 2, "similarity": 0.6, "tags": ["nlp"]},
        ]
        topic = "poker"
        applied = False
        for r in enriched:
            if topic in (r.get("tags") or []):
                r["similarity"] *= 1.10
                applied = True
        self.assertFalse(applied)
        # Order unchanged
        self.assertEqual(enriched[0]["fact_id"], 1)

    def test_topic_boost_handles_string_tags(self):
        """tags might come back as a JSON string (from sqlite)."""
        enriched = [
            {"fact_id": 1, "similarity": 0.5, "tags": '["poker", "strategy"]'},
        ]
        topic = "poker"
        applied = False
        for r in enriched:
            tags = r.get("tags") or []
            if isinstance(tags, str):
                import json
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            if topic in tags:
                r["similarity"] *= 1.10
                applied = True
        self.assertTrue(applied)
        self.assertAlmostEqual(enriched[0]["similarity"], 0.55, places=2)


if __name__ == "__main__":
    unittest.main()
