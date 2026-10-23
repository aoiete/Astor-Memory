"""Tests for v1.16.72 write-path timing instrumentation.

Run: pytest tests/test_write_timing.py -v
"""
import json
import unittest
import urllib.error
import urllib.request

URL = "http://127.0.0.1:7803/v1/write"


_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        # v1.16.95: this test used to post to the LIVE server. Its probe text
        # ("astor write path instrumentation probe ...") is on the residue
        # list, so /v1/write now demotes it out of the shared tier -- which
        # is the point: the probe must not reach the production public tier.
        # Posting through the Flask test client against the isolated runtime
        # (conftest points ASTOR_DIR at a temp dir) keeps the test honest
        # AND keeps production clean.
        from astor_memory.server import create_app
        _CLIENT = create_app().test_client()
    return _CLIENT


def _write(text, debug_timing=True):
    r = _client().post("/v1/write", json={
        "text": text,
        "kind": "fact",
        "debug_timing": debug_timing,
    }, headers={"X-Actor": "admin:admin"})
    try:
        return r.get_json()
    except Exception:
        return {"_http_error": r.status_code,
                "_body": r.get_data(as_text=True)[:500]}


class TestWriteTiming(unittest.TestCase):
    """Verify /v1/write returns per-stage timing when debug_timing=True."""

    def setUp(self):
        # Use hex(id(self))[2:] for uniqueness; avoid words Memory Defense flags
        # (credit_card / discord_snowflake regex) by using neutral words.
        self.text = "astor write path instrumentation probe {}".format(hex(id(self))[2:])

    def test_debug_timing_true_returns_w_stages_ms(self):
        d = _write(self.text, debug_timing=True)
        self.assertNotIn("_http_error", d, d.get("_body", ""))
        self.assertIn("w_stages_ms", d)
        stages = d["w_stages_ms"]
        for s in ("classify_intent", "start_extract", "end_extract",
                  "promote_start", "promote_done"):
            self.assertIn(s, stages, f"missing stage {s}")
        for s, ms in stages.items():
            self.assertGreaterEqual(ms, 0, f"stage {s} negative: {ms}ms")

    def test_debug_timing_false_returns_null_w_stages(self):
        d = _write(self.text, debug_timing=False)
        self.assertNotIn("_http_error", d, d.get("_body", ""))
        self.assertIn("w_stages_ms", d)
        self.assertIsNone(d["w_stages_ms"],
                           "w_stages_ms must be null when debug_timing not requested")

    def test_classify_intent_under_500ms(self):
        d = _write(self.text, debug_timing=True)
        stages = d["w_stages_ms"]
        self.assertLess(stages["classify_intent"], 500,
                         f"classify_intent too slow: {stages['classify_intent']}ms")

    def test_extract_window_bounds(self):
        d = _write(self.text, debug_timing=True)
        stages = d["w_stages_ms"]
        extract_elapsed = stages["end_extract"] - stages["start_extract"]
        self.assertLess(extract_elapsed, 5000,
                         f"extract too slow: {extract_elapsed}ms")

    def test_promote_loop_bounds(self):
        d = _write(self.text, debug_timing=True)
        stages = d["w_stages_ms"]
        promote_elapsed = stages["promote_done"] - stages["promote_start"]
        self.assertLess(promote_elapsed, 2000,
                         f"promote_loop too slow: {promote_elapsed}ms")

    def test_stage_order(self):
        d = _write(self.text, debug_timing=True)
        stages = d["w_stages_ms"]
        order = ["classify_intent", "start_extract", "end_extract",
                 "promote_start", "promote_done"]
        for i in range(len(order) - 1):
            a, b = order[i], order[i + 1]
            self.assertLessEqual(stages[a], stages[b] + 5,
                                  f"{a} ({stages[a]}) > {b} ({stages[b]})")


if __name__ == "__main__":
    unittest.main()