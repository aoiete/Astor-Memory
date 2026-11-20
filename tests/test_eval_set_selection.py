"""The eval scripts must read the file they are told to read.

A --set argument that is accepted and printed but never used makes a
measurement look like it was run on a set it never touched. That bug
was in eval_runner.py and then in eval_sweep.py.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


class TestEvalSetSelection(unittest.TestCase):

    def _write(self, rows):
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.close()
        return Path(fh.name)

    def test_eval_runner_loads_the_given_file(self):
        import eval_runner
        p = self._write([{"q": "a"}, {"q": "b"}])
        try:
            rows = eval_runner.load_eval_set(p)
        finally:
            p.unlink(missing_ok=True)
        self.assertEqual([r["q"] for r in rows], ["a", "b"])

    def test_eval_sweep_loads_the_given_file(self):
        import eval_sweep
        p = self._write([{"q": "a"}])
        try:
            rows = eval_sweep.load_eval_set(p)
        finally:
            p.unlink(missing_ok=True)
        self.assertEqual([r["q"] for r in rows], ["a"])

    def test_both_default_to_their_own_set(self):
        import eval_runner
        import eval_sweep
        self.assertTrue(eval_runner.load_eval_set())
        self.assertTrue(eval_sweep.load_eval_set())


if __name__ == "__main__":
    unittest.main()
