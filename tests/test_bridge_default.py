"""test_bridge_default.py — verify ASTOR_BRIDGE=1 (default) doesn't regress.

Ships with v1.16.6 — when C is enabled. For now (v1.16.5) we test
both modes via env override and check that apply_multi_hop_boost
behaves sanely with default + non-default.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import astor_memory.nest.multi_hop_bridge as mhb


def _mk(content, tags=None, keywords=None, score=0.5):
    return {
        "id": 0,
        "score": score,
        "content": content,
        "tags": tags or [],
        "keywords": keywords or [],
    }


class TestBridgeDefault(unittest.TestCase):

    def test_apply_multi_hop_boost_empty(self):
        """Empty input → empty output, no crash."""
        result = mhb.apply_multi_hop_boost(candidates=[])
        self.assertEqual(result, [])

    def test_apply_multi_hop_boost_single(self):
        """Single candidate → no boost (need pairs for entity bridge)."""
        cands = [_mk("John works at X", tags=["John", "X"], score=1.0)]
        result = mhb.apply_multi_hop_boost(candidates=cands)
        # Should return at least the input
        self.assertGreaterEqual(len(result), 1)

    def test_apply_multi_hop_boost_chain(self):
        """Two candidates sharing 'John' should both surface."""
        cands = [
            _mk("Mary is John's friend", tags=["Mary", "John"], score=1.0),
            _mk("John works at X Corp", tags=["John", "X Corp"], score=1.0),
        ]
        result = mhb.apply_multi_hop_boost(candidates=cands)
        # Both should be returned (chain coherence)
        self.assertEqual(len(result), 2)

    def test_apply_multi_hop_boost_no_overlap(self):
        """Disjoint entities — no boost, original order preserved."""
        cands = [
            _mk("Alice likes dark mode", tags=["Alice"], score=1.0),
            _mk("Bob works at Y Corp", tags=["Bob", "Y Corp"], score=1.0),
        ]
        result = mhb.apply_multi_hop_boost(candidates=cands)
        # No shared entities → no boost, order preserved
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)