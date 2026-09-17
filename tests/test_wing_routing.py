"""Tests for Ship F: kind-based routing (wing auto-tag + wing alias).

The /v1/read endpoint now accepts `wing=human|agent|rule` and filters
results to the matching provenance_kind set. The /v1/write path auto-
derives provenance_kind from origin_session_id prefix.

WING_TO_PROVENANCE mapping:
  wing=human  → {manual}
  wing=agent  → {extracted, inferred, merged}
  wing=rule   → {rule}

Tests cover the helper functions (no server spin-up needed).
"""

from __future__ import annotations

import unittest
import sys

# Import the server module's helpers (without spinning up Flask)
sys.path.insert(0, 'D:/AI/astor-memory')
from astor_memory.server import (
    _infer_provenance_kind,
    _expand_wing_to_provenance,
    WING_TO_PROVENANCE,
)


class TestInferProvenanceKind(unittest.TestCase):
    """origin_session_id prefix → provenance_kind."""

    def test_no_session_id_defaults_to_manual(self):
        self.assertEqual(_infer_provenance_kind(None), "manual")
        self.assertEqual(_infer_provenance_kind(""), "manual")

    def test_human_platforms_are_manual(self):
        for sid in ("discord:1234", "telegram:5678", "wechat:abc", "cli:write"):
            self.assertEqual(_infer_provenance_kind(sid), "manual", sid)

    def test_hook_and_cron_are_extracted(self):
        for sid in (
            "cron:daily-brief",
            "hook:post_tool_call",
            "hook:session_end",
            "hook:pre_compact",
        ):
            self.assertEqual(_infer_provenance_kind(sid), "extracted", sid)

    def test_auto_processes_are_inferred(self):
        for sid in ("auto_link:abc", "auto_observe:def", "merge:xyz"):
            self.assertEqual(_infer_provenance_kind(sid), "inferred", sid)

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(_infer_provenance_kind("weird:thing"))
        self.assertIsNone(_infer_provenance_kind("just-a-name"))


class TestExpandWingToProvenance(unittest.TestCase):
    """wing alias → provenance_kind set."""

    def test_no_wing_returns_none(self):
        self.assertIsNone(_expand_wing_to_provenance(None))
        self.assertIsNone(_expand_wing_to_provenance(""))
        self.assertIsNone(_expand_wing_to_provenance("   "))

    def test_human_wing_maps_to_manual(self):
        self.assertEqual(_expand_wing_to_provenance("human"), {"manual"})
        # Case-insensitive
        self.assertEqual(_expand_wing_to_provenance("Human"), {"manual"})
        self.assertEqual(_expand_wing_to_provenance("HUMAN"), {"manual"})

    def test_agent_wing_maps_to_extracted_inferred_merged(self):
        self.assertEqual(
            _expand_wing_to_provenance("agent"),
            {"extracted", "inferred", "merged"},
        )

    def test_rule_wing_maps_to_rule(self):
        self.assertEqual(_expand_wing_to_provenance("rule"), {"rule"})

    def test_unknown_wing_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            _expand_wing_to_provenance("dragon")
        self.assertIn("dragon", str(ctx.exception))
        # Error message lists valid wings
        self.assertIn("human", str(ctx.exception))
        self.assertIn("agent", str(ctx.exception))
        self.assertIn("rule", str(ctx.exception))


class TestWingToProvenanceMapping(unittest.TestCase):
    """Validate the WING_TO_PROVENANCE dict shape."""

    def test_mapping_has_all_three_wings(self):
        self.assertIn("human", WING_TO_PROVENANCE)
        self.assertIn("agent", WING_TO_PROVENANCE)
        self.assertIn("rule", WING_TO_PROVENANCE)

    def test_mapping_values_are_sets(self):
        for wing, prov_set in WING_TO_PROVENANCE.items():
            self.assertIsInstance(prov_set, set, wing)

    def test_mapping_disjoint_except_agent_includes_extracted(self):
        # human and rule are singletons; agent covers the rest
        self.assertEqual(WING_TO_PROVENANCE["human"], {"manual"})
        self.assertEqual(WING_TO_PROVENANCE["rule"], {"rule"})
        # agent should NOT include manual or rule (those are human/system)
        self.assertNotIn("manual", WING_TO_PROVENANCE["agent"])
        self.assertNotIn("rule", WING_TO_PROVENANCE["agent"])


if __name__ == '__main__':
    unittest.main()
