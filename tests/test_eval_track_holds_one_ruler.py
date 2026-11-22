"""The trend track holds one ruler's runs, and nothing else.

The track (eval_history.jsonl) is what eval_trend.py, the dashboard and
the regression check read to answer "did recall get better, or did the
ruler change". Every row in it is compared against, and averaged with,
every other row in it. So a row that measured a DIFFERENT question set
does not belong there.

Three ways a row gets in that should not:

1. A unit test calls the runner's main() with a scratch set. The run
   lands in the operator's track, and the ruler -- the median of the
   last 3 baselines -- comes out 0.0 instead of 0.9811. That is what
   happened: 22 rows of a 3-row scratch set, hit_rate 0.0, and the
   ruler reading 0.0 while the real measurement was 0.9811.

2. A run against another set (the paraphrase set) lands in the track
   and is averaged with runs against the operator's set. The two
   answer different questions, and the average of them is not a
   measurement of anything.

3. The recorded eval_set_hash says the DEFAULT set's hash whatever
   set was loaded, so the field that exists to tell runs apart cannot.

This checks the three properties that stop them. The runner side is
covered by test_eval_history_not_polluted.py; this is the reader side
and the classification the repair relies on.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).parent
REPO = TESTS.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestTheTrackHoldsOneRuler(unittest.TestCase):

    def setUp(self):
        self.runner = _load("_eval_runner_track_test", TESTS / "eval_runner.py")
        self.default_set = TESTS / "eval_set.jsonl"
        self.para_set = TESTS / "eval_paraphrase_set.jsonl"

    def test_the_two_sets_are_distinguishable(self):
        """Two rulers must not hash the same, or nothing can tell them
        apart -- and the set hash is the only thing that does."""
        d = self.runner._eval_set_hash(self.default_set)
        p = self.runner._eval_set_hash(self.para_set)
        self.assertIsNotNone(d)
        self.assertIsNotNone(p)
        self.assertNotEqual(d, p)

    def test_a_run_is_on_the_track_only_if_it_measured_the_operators_set(self):
        """The classification the repair used, exercised on real sets.

        The repair decided which set each historical row measured by
        its category counts and its scored count -- both had to match a
        known set exactly. That is the rule, so it is the rule that gets
        tested: a row that matches one set must not also match the
        other, and a row that matches neither must be left alone rather
        than guessed at.
        """
        d_rows = [json.loads(l) for l in
                  self.default_set.read_text(encoding="utf-8").splitlines()
                  if l.strip()] if self.default_set.exists() else []
        p_rows = [json.loads(l) for l in
                  self.para_set.read_text(encoding="utf-8").splitlines()
                  if l.strip()]

        def counts(rows):
            out = {}
            for r in rows:
                out[r.get("category")] = out.get(r.get("category"), 0) + 1
            return out

        def scored(rows):
            # expected_miss rows are not counted toward the run's totals,
            # so the number a run reports is the scored count.
            return sum(1 for r in rows if not r.get("expected_miss"))

        d_counts, p_counts = counts(d_rows), counts(p_rows)
        self.assertNotEqual(d_counts, p_counts,
                            "the two sets have the same category profile -- "
                            "the rows cannot be told apart")

        # A row that measured the default set is on the track; a row
        # that measured the paraphrase set is not.
        d_summary = {"n_queries": scored(d_rows), "by_category": {
            k: {"n": v} for k, v in d_counts.items()},
            "eval_set_hash": self.runner._eval_set_hash(self.default_set)}
        p_summary = {"n_queries": scored(p_rows), "by_category": {
            k: {"n": v} for k, v in p_counts.items()},
            "eval_set_hash": self.runner._eval_set_hash(self.para_set)}

        if d_rows:
            self.assertTrue(self.runner._is_operator_set_run(d_summary))
        self.assertFalse(self.runner._is_operator_set_run(p_summary))

    def test_a_row_that_lies_about_its_hash_is_rejected(self):
        """The hash alone can be faked; the count cannot.

        The 22 litter rows carried the DEFAULT set's hash while
        measuring three questions, because the runner hashed the
        default set whatever it loaded. A guard that checks only the
        hash waves exactly those rows onto the track.
        """
        scratch = {"n_queries": 3, "by_category": {"t": {"n": 3}},
                   "eval_set_hash": self.runner._eval_set_hash(self.default_set)}
        self.assertFalse(self.runner._is_operator_set_run(scratch),
                         "a scratch row with the default set's hash was "
                         "accepted onto the track -- the hash it recorded "
                         "was never the set it measured")

    def test_a_scratch_row_is_not_on_the_track(self):
        """The litter from a unit test is not a measurement."""
        scratch = {"n_queries": 3, "by_category": {"t": {"n": 3}},
                   "eval_set_hash": "not-the-operators-set"}
        self.assertFalse(self.runner._is_operator_set_run(scratch),
                         "a scratch row was accepted onto the track")

    def test_an_unhashed_row_is_not_on_the_track(self):
        """No hash means no proof it measured the operator's set."""
        row = {"n_queries": 106, "by_category": {}, "eval_set_hash": None}
        self.assertFalse(self.runner._is_operator_set_run(row))


if __name__ == "__main__":
    unittest.main()
