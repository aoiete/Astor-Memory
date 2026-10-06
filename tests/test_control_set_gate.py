"""Tests for control_set_gate (v1.16.71 Ship D).

Run: pytest tests/test_control_set_gate.py -v
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, r"D:/AI/Astor-Memory-Runtime")

# Load control_set_gate as module (it's a script, not in package layout)
_GATE_PATH = Path(__file__).parent / "control_set_gate.py"
_spec = importlib.util.spec_from_file_location("control_set_gate", _GATE_PATH)
control_set_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control_set_gate)

_check_control_set = control_set_gate._check_control_set
CONTROL_SET = control_set_gate.CONTROL_SET


class TestControlSetGate(unittest.TestCase):
    """Gate runner must:
    1. Run successfully against the live server
    2. Return a structured verdict
    3. Pass cleanly on the live baseline (no degradation)
    """

    def setUp(self):
        self.server = "http://127.0.0.1:7803"

    def test_control_set_loads(self):
        cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
        self.assertIn("queries", cs)
        self.assertGreater(len(cs["queries"]), 20, "control set must have ≥20 queries")
        self.assertIn("pass_rule", cs)

    def test_control_set_each_query_has_required_fields(self):
        cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
        for q in cs["queries"]:
            for field in ("qid", "query", "category", "baseline_top3_fids",
                          "baseline_first_rank", "baseline_mrr"):
                self.assertIn(field, q, f"query {q.get('qid', '?')} missing {field}")
            self.assertEqual(q["baseline_first_rank"], 1,
                              f"only first_rank=1 queries allowed (got {q['baseline_first_rank']})")
            self.assertEqual(q["baseline_mrr"], 1.0,
                              f"only mrr=1.0 queries allowed (got {q['baseline_mrr']})")

    def test_pass_rule_present(self):
        cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
        rule = cs.get("pass_rule", {})
        self.assertIn("overlap_min", rule)
        self.assertIn("mrr_tolerance", rule)
        # Sanity bounds
        self.assertGreater(rule["overlap_min"], 0.0)
        self.assertLess(rule["overlap_min"], 1.0)
        self.assertLess(rule["mrr_tolerance"], 0.5)

    def test_live_run_passes_baseline(self):
        """The control set was built from current live state, so the live
        run should pass with no flips. Cold-cache variance may cause
        occasional 1-flip — that's normal and the gate handles it."""
        result = _check_control_set(self.server)
        self.assertEqual(result["n_fail"], 0,
                         f"control set gate failed (n_fail={result['n_fail']}): {result.get('flips', [])}")
        self.assertEqual(result["verdict"], "PASS")

    def test_result_shape(self):
        result = _check_control_set(self.server)
        for field in ("baseline_run", "baseline_version", "control_set_version",
                      "n_total", "n_pass", "n_fail", "flips", "verdict"):
            self.assertIn(field, result, f"missing {field} in result")
        self.assertEqual(result["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()