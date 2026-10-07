"""Tests for control_set_gate (v1.16.74 semantic rework).

Run: pytest tests/test_control_set_gate.py -v

v1.16.74 changed the gate's contract:
  - assertion is KEYWORD-based (meaning), not id-based (row identity)
  - per-query `tier` override (a single global tier is wrong when the
    control set spans tiers; private-tier facts are unreadable from source)
  - `user` is threaded through because private-tier reads are ACL-gated
    and 403 without an actor identity
  - `baseline_top3_fids` survives as a REPORTED soft signal, never gating
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

# The package resolves from the repo root; tests/ lives one level down.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load control_set_gate as module (it's a script, not in package layout)
_GATE_PATH = Path(__file__).parent / "control_set_gate.py"
_spec = importlib.util.spec_from_file_location("control_set_gate", _GATE_PATH)
control_set_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control_set_gate)

_check_control_set = control_set_gate._check_control_set
CONTROL_SET = control_set_gate.CONTROL_SET

SERVER = "http://127.0.0.1:7803"
TIER = "source"
USER = "admin"
TOP_K = 10


class TestControlSetGate(unittest.TestCase):
    """Gate runner must:
    1. Run successfully against the live server
    2. Return a structured verdict
    3. Pass cleanly on the live corpus (no recall regression)
    """

    def setUp(self):
        self.server = SERVER

    def _run(self):
        return _check_control_set(self.server, TOP_K, TIER, USER)

    def test_control_set_loads(self):
        cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
        self.assertIn("queries", cs)
        self.assertGreater(len(cs["queries"]), 20, "control set must have >=20 queries")

    def test_control_set_each_query_has_required_fields(self):
        """baseline_top3_fids is optional now (soft signal), but expected_keywords
        is mandatory: the semantic assertion has nothing to check without it."""
        cs = json.loads(CONTROL_SET.read_text(encoding="utf-8"))
        for q in cs["queries"]:
            for field in ("qid", "query", "category", "expected_keywords"):
                self.assertIn(field, q, f"query {q.get('qid', '?')} missing {field}")
            kws = q["expected_keywords"]
            self.assertGreaterEqual(
                len(kws), 1,
                f"{q['qid']}: expected_keywords must be non-empty "
                f"(semantic assertion needs something to match)",
            )

    def test_keyword_ratio_is_sane(self):
        self.assertGreater(control_set_gate.KEYWORD_MIN_RATIO, 0.0)
        self.assertLessEqual(control_set_gate.KEYWORD_MIN_RATIO, 1.0)

    def test_live_run_passes_baseline(self):
        """The control set was built from the live corpus, so the live run
        should pass. This is the assertion the ship gate actually enforces."""
        result = self._run()
        self.assertEqual(
            result["n_fail"], 0,
            f"control set gate failed ({result['n_pass']}/{result['n_total']}): "
            f"{result.get('failures', [])}",
        )
        self.assertEqual(result["verdict"], "PASS")

    def test_result_shape(self):
        result = self._run()
        for field in ("gate_version", "control_set_version", "baseline_version",
                      "n_total", "n_pass", "n_fail", "pass_rate",
                      "failures", "fid_drift_count", "verdict"):
            self.assertIn(field, result, f"missing {field} in result")
        self.assertEqual(result["verdict"], "PASS")

    def test_fid_drift_never_gates(self):
        """A corpus that grew past every baseline id must still PASS, as long
        as the expected meaning is still recalled. This is the regression the
        v1.16.74 rework exists to prevent (the old gate scored 0/40 on a
        healthy system purely because fact_ids moved)."""
        result = self._run()
        self.assertEqual(
            result["verdict"], "PASS",
            "gate must not fail on fact_id drift alone",
        )
        # drift is reported, not fatal
        self.assertIsInstance(result["fid_drift_count"], int)
        if result["fid_drift_count"]:
            self.assertTrue(result["fid_drift_sample"])

    def test_semantic_gate_ignores_baseline_fids(self):
        """Direct unit check of the assertion itself: a query whose expected
        keywords ARE present passes even when its baseline fids are absent."""
        qs = [{"qid": "T1", "query": "DCA barbell", "category": "test",
               "expected_keywords": ["DCA", "barbell"],
               "baseline_top3_fids": [999999, 888888, 777777]}]
        import tempfile
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump({"version": 1, "queries": qs}, tmp)
        tmp.close()
        orig = control_set_gate.CONTROL_SET
        try:
            control_set_gate.CONTROL_SET = Path(tmp.name)
            result = control_set_gate._check_control_set(SERVER, TOP_K, TIER, USER)
        finally:
            control_set_gate.CONTROL_SET = orig
            Path(tmp.name).unlink(missing_ok=True)
        self.assertEqual(result["n_pass"], 1,
                         "stale baseline fids must not fail a semantically-correct recall")
        self.assertGreaterEqual(result["fid_drift_count"], 0)


if __name__ == "__main__":
    unittest.main()
