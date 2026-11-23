"""Editing the eval runner's plumbing must not invalidate the history.

v1.17.87. The trend reader keeps only the baselines that share the
CURRENT scorer hash -- that rule is right, and it predates the fix
(v1.17.13). But the scorer hash used to cover eval_runner.py
byte-for-byte, so ANY edit to the runner changed it.

The runner is where the harness lives: the metrics directory, the
set-hash plumbing, the off-track guard. None of that is scoring. So a
plumbing edit silently discarded every baseline recorded before it --
the window collapsed to the one run recorded after the edit, and the
ruler read "no_data".

That is what happened on 2026-11-22. The scoring rules did not change
at all (score_results() and eval_oracle.py were untouched), but the
runner was edited, so the whole history became "not comparable".

A hash whose purpose is to detect a changed MEASUREMENT must not fire
on a change that did not touch the measurement.

These tests pin that: a plumbing edit leaves the hash alone, and a
scoring edit changes it.
"""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"


def _load_runner():
    spec = importlib.util.spec_from_file_location("_er_hash_test", TESTS / "eval_runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_er_hash_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _surface_hash(runner_src: str, oracle_src: str) -> str:
    mod = _load_runner()
    return mod._scorer_hash_from_source(runner_src, oracle_src)


def _score_results_source(runner_src: str) -> str:
    tree = ast.parse(runner_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "score_results")
    lines = runner_src.splitlines()
    return "\n".join(lines[fn.lineno - 1:fn.end_lineno])


# --- the hash covers the scoring surface, not the whole file -------------

def test_the_hash_is_not_the_whole_file_hash():
    """A plumbing-only edit must not move the hash."""
    import hashlib

    runner_src = (TESTS / "eval_runner.py").read_text(encoding="utf-8")
    oracle_src = (TESTS / "eval_oracle.py").read_text(encoding="utf-8")

    surface = _surface_hash(runner_src, oracle_src)

    whole = hashlib.sha256()
    whole.update(runner_src.encode("utf-8"))
    whole.update(oracle_src.encode("utf-8"))
    assert surface != whole.hexdigest()[:16], (
        "the scorer hash is the whole-file hash again -- any edit to the "
        "harness would invalidate the entire measurement history"
    )


def test_a_plumbing_edit_leaves_the_hash_alone():
    """Add a comment and a helper far from score_results()."""
    import hashlib

    runner_src = (TESTS / "eval_runner.py").read_text(encoding="utf-8")
    oracle_src = (TESTS / "eval_oracle.py").read_text(encoding="utf-8")
    before = _surface_hash(runner_src, oracle_src)

    # A plumbing change: a new module-level constant and a comment.
    edited = runner_src.replace(
        "def recall(", "# a plumbing comment that changes nothing about scoring\n"
        "_PLUMBING_CONSTANT = 1\n\n\ndef recall(", 1)
    assert edited != runner_src, "the test did not actually edit the file"

    after = _surface_hash(edited, oracle_src)
    assert before == after, (
        "a plumbing edit changed the scorer hash -- the measurement "
        "history would be discarded for a change that did not touch "
        "the measurement"
    )


def test_a_scoring_edit_changes_the_hash():
    """The whole point of the hash: a scoring change MUST move it."""
    runner_src = (TESTS / "eval_runner.py").read_text(encoding="utf-8")
    oracle_src = (TESTS / "eval_oracle.py").read_text(encoding="utf-8")
    before = _surface_hash(runner_src, oracle_src)

    edited = runner_src.replace(
        "def score_results(", "def score_results(", 1).replace(
        "min_hits: int) -> dict:", "min_hits: int) -> dict:  # scoring edit", 1)
    assert edited != runner_src, "the test did not actually edit the file"

    after = _surface_hash(edited, oracle_src)
    assert before != after, (
        "a scoring edit did NOT change the scorer hash -- the hash no "
        "longer detects a loosened scorer, which is the one thing it "
        "is for"
    )


def test_an_oracle_edit_changes_the_hash():
    """eval_oracle.py feeds the gap decomposition, so it counts too."""
    runner_src = (TESTS / "eval_runner.py").read_text(encoding="utf-8")
    oracle_src = (TESTS / "eval_oracle.py").read_text(encoding="utf-8")
    before = _surface_hash(runner_src, oracle_src)

    edited = oracle_src + "\n# a scoring-side change\n"
    after = _surface_hash(runner_src, edited)
    assert before != after, "an eval_oracle.py edit did not change the hash"


def test_the_live_hash_matches_the_source_computed_hash():
    """The two paths must agree, or re-seeding the history is a guess."""
    mod = _load_runner()
    live = mod._scorer_hash()
    runner_src = (TESTS / "eval_runner.py").read_text(encoding="utf-8")
    oracle_src = (TESTS / "eval_oracle.py").read_text(encoding="utf-8")
    assert live == mod._scorer_hash_from_source(runner_src, oracle_src)


def test_the_scoring_rules_have_not_changed_since_the_last_oracle_commit():
    """Why the historical rows are comparable, proved rather than assumed.

    Every baseline on the track was recorded after the last commit that
    touched eval_oracle.py. If the scoring surface at that commit is
    the same as today's, then every one of those rows was measured by
    today's rules -- which is what re-seeding their hash claims.
    """
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "tests/eval_oracle.py"],
        capture_output=True, text=True, cwd=str(REPO)).stdout.strip()
    assert out, "no commit touching eval_oracle.py was found"

    def at(rev: str) -> str:
        runner = subprocess.run(["git", "show", f"{rev}:tests/eval_runner.py"],
                                capture_output=True, text=True, cwd=str(REPO)).stdout
        oracle = subprocess.run(["git", "show", f"{rev}:tests/eval_oracle.py"],
                                 capture_output=True, text=True, cwd=str(REPO)).stdout
        return _surface_hash(runner, oracle)

    mod = _load_runner()
    current = mod._scorer_hash()
    assert at(out) == current, (
        "the scoring rules changed at the last eval_oracle.py commit, so "
        "the historical rows are NOT all measured by today's rules and "
        "must not be re-seeded as if they were"
    )
