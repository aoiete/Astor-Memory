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


# A control set built from a real corpus can only be re-run against that
# corpus. CI starts a server on an empty ASTOR_DIR (see .github/workflows/
# ci.yml), so the server answers /v1/health fine while every query returns
# nothing — that is an empty-corpus signal, not a recall regression.
MIN_CORPUS_FACTS = 500


def _server_with_corpus(server: str = SERVER, timeout: float = 5.0) -> bool:
    """True when `server` is up AND holds enough facts to answer the gate.

    Reachability alone is not enough: a freshly-initialised server passes a
    ping check and then fails every query. Require the corpus to be at least
    MIN_CORPUS_FACTS so the gate only runs where its queries can plausibly
    match, and skips (rather than fails) everywhere else.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{server}/v1/health", timeout=timeout) as r:
            if not (200 <= r.status < 300):
                return False
            health = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    try:
        facts = int(health.get("facts") or 0)
    except (TypeError, ValueError):
        return False
    return facts >= MIN_CORPUS_FACTS


_SKIP_NO_CORPUS = (
    f"no corpus at {SERVER} (need >= {MIN_CORPUS_FACTS} facts) — the "
    f"control-set gate is built from a real corpus, so it only runs "
    f"against one (expected in CI, which runs on an empty ASTOR_DIR)"
)


class TestControlSetGate(unittest.TestCase):
    """Gate runner must:
    1. Run successfully against the live server
    2. Return a structured verdict
    3. Pass cleanly on the live corpus (no recall regression)
    """

    def setUp(self):
        self.server = SERVER
        self._live = _server_with_corpus(self.server)

    def _run(self):
        if not self._live:
            self.skipTest(_SKIP_NO_CORPUS)
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
        if result["verdict"] == "INFRA_ERROR":
            self.skipTest(
                "server unreachable for every control query "
                f"({result.get('n_infra_error')} read errors) -- "
                "infrastructure, not a recall regression")
        self.assertEqual(
            result["n_fail"], 0,
            f"control set gate failed ({result['n_pass']}/{result['n_total']}): "
            f"{result.get('failures', [])}",
        )
        self.assertEqual(result["verdict"], "PASS")

    def test_result_shape(self):
        result = self._run()
        if result["verdict"] == "INFRA_ERROR":
            self.skipTest("server unreachable -- infrastructure failure")
        for field in ("gate_version", "control_set_version", "baseline_version",
                      "n_total", "n_pass", "n_fail", "pass_rate",
                      "failures", "fid_drift_count", "verdict",
                      "n_infra_error", "n_answered", "infra_errors"):
            self.assertIn(field, result, f"missing {field} in result")
        self.assertEqual(result["verdict"], "PASS")

    def test_fid_drift_never_gates(self):
        """A corpus that grew past every baseline id must still PASS, as long
        as the expected meaning is still recalled. This is the regression the
        v1.16.74 rework exists to prevent (the old gate scored 0/40 on a
        healthy system purely because fact_ids moved)."""
        result = self._run()
        if result["verdict"] == "INFRA_ERROR":
            self.skipTest(
                "server unreachable for every control query "
                f"({result.get('n_infra_error')} read errors) -- "
                "infrastructure, not a recall regression")
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
        # This one calls the gate directly (not via _run) so it needs its own
        # reachability guard for the same reason.
        if not self._live:
            self.skipTest(_SKIP_NO_CORPUS)
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
