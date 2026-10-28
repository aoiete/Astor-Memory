"""The recall log must record WHICH facts came back, not just how many.

Before v1.17.14 the log said "query X returned 3 results, top score
0.85". A fact could be counted (access_count) but never traced: "which
of my questions brought this memory back?" had no answer.

The server writes a `hits` array; `am recall-trail <fact_id>` reads it
back. These tests cover both halves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestRecallLogCarriesHits(unittest.TestCase):
    """The server side: hits must be recorded, and bounded."""

    def test_server_writes_a_hits_array(self):
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        self.assertIn("'hits': _hits", src,
                      "the recall log must record the fact-level hits")
        self.assertIn("_hits.append(", src)

    def test_hits_carry_fact_id_score_kind_and_conf(self):
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        for key in ("'fact_id'", "'score_kind'", "'conf'", "'kind'"):
            self.assertIn(key, src)

    def test_hits_are_capped(self):
        """top_k can be large and this file grows by default."""
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        self.assertIn("enriched[:10]", src,
                      "hits must be capped so the log does not grow unbounded")


class TestRecallTrailCommand(unittest.TestCase):
    """The CLI side: given a log with hits, find the queries."""

    def _run_trail(self, log_lines, fact_id, extra=()):
        with tempfile.TemporaryDirectory() as td:
            metrics = Path(td) / "astor" / "metrics"
            metrics.mkdir(parents=True)
            log = metrics / "recall_log.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in log_lines),
                           encoding="utf-8")
            env = dict(os.environ, ASTOR_DIR=td)
            p = subprocess.run(
                [sys.executable, "-c",
                 "from astor_memory.cli.main import main; main()",
                 "recall-trail", str(fact_id), *extra],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=120)
            return p.stdout + p.stderr

    def test_finds_the_query_that_brought_the_fact_back(self):
        out = self._run_trail([
            {"ts": "2026-10-28T10:00:00Z", "tier": "public",
             "user_id": "admin", "query": "cache key ranking",
             "hits": [{"fact_id": 5832, "score_kind": "hybrid", "conf": 0.85}]},
            {"ts": "2026-10-28T11:00:00Z", "tier": "public",
             "user_id": "admin", "query": "something else",
             "hits": [{"fact_id": 1111, "score_kind": "bm25", "conf": 0.5}]},
        ], 5832)
        self.assertIn("cache key ranking", out)
        self.assertNotIn("something else", out)

    def test_reports_no_trail_clearly(self):
        out = self._run_trail([
            {"ts": "2026-10-28T10:00:00Z", "tier": "public",
             "query": "x", "hits": [{"fact_id": 999}]},
        ], 4242)
        self.assertIn("no recorded trail", out.lower())

    def test_says_that_old_entries_are_untraceable(self):
        """Pre-v1.17.14 rows have no hits -- say so, do not report a lie."""
        out = self._run_trail([
            {"ts": "2026-10-20T10:00:00Z", "tier": "public",
             "query": "old entry without hits", "n_results": 2},
        ], 4242)
        self.assertIn("before v1.17.14", out)

    def test_a_malformed_hits_entry_does_not_crash(self):
        out = self._run_trail([
            {"ts": "2026-10-28T10:00:00Z", "query": "x",
             "hits": [{"fact_id": "not-a-number"}, {"no_fact_id": 1}]},
            {"ts": "2026-10-28T10:00:01Z", "query": "y",
             "hits": [{"fact_id": 7}]},
        ], 7)
        self.assertIn("was brought back by", out)

    def test_non_integer_fact_id_is_rejected(self):
        out = self._run_trail([], "not-a-number")
        self.assertTrue("integer" in out.lower() or "usage" in out.lower())


if __name__ == "__main__":
    unittest.main()
