"""Regression proof for the v1.17.37 foreign-key severity split.

Three independent reverts must each break the suite. A revert that leaves the
tests green means the test does not actually pin the behaviour.

  revert 1: ship_c_warn keys on the raw total -> the whole point of the change
  revert 2: no sentinel bucket (TEXT in an INTEGER FK column) -> sentinels
             start counting as live defects
  revert 3: no legacy bucket (tombstoned rows) -> dead history starts alarming

Run:  python tests/test_fk_severity_split_revert_proof.py
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SERVER = REPO / "astor_memory" / "server.py"
TEST = "tests/test_fk_severity_split.py"

REVERTS = {
    "ship_c_warn keys on the raw total": (
        'db_corruption_check.get("foreign_key_violations_defect", 0) > 0',
        'db_corruption_check["foreign_key_violations"] > 0',
    ),
    "no sentinel bucket": (
        'if not str(cand).lstrip("-").isdigit() or \\\n'
        '                                not str(evt).lstrip("-").isdigit():',
        'if False:',
    ),
    "no legacy bucket": (
        "if tomb:\n"
        "                            continue  # legacy: a tombstoned row can never be recalled",
        "if False:\n"
        "                            continue",
    ),
}


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q"],
        cwd=str(REPO), capture_output=True, text=True, timeout=600,
    )
    tail = "\n".join(r.stdout.strip().split("\n")[-4:])
    return r.returncode, tail


def main() -> int:
    original = SERVER.read_text(encoding="utf-8")
    backup = pathlib.Path(tempfile.mkdtemp()) / "server.py"
    backup.write_text(original, encoding="utf-8")

    failures = []
    try:
        print("=== baseline (fix in place) ===")
        rc, tail = run_tests()
        print(tail)
        if rc != 0:
            print("BASELINE FAILS -- fix the code before proving reverts")
            return 1
        print("baseline: PASS\n")

        for label, (old, new) in REVERTS.items():
            if old not in original:
                print("SKIP  %s -- anchor not found in server.py" % label)
                failures.append(label)
                continue
            SERVER.write_text(original.replace(old, new, 1), encoding="utf-8")
            rc, tail = run_tests()
            SERVER.write_text(original, encoding="utf-8")
            if rc == 0:
                print("FAIL  %s -- tests still pass, the revert is not pinned" % label)
                failures.append(label)
            else:
                print("OK    %s -> tests FAIL as expected" % label)
    finally:
        SERVER.write_text(original, encoding="utf-8")
        shutil.rmtree(backup.parent, ignore_errors=True)

    print()
    if failures:
        print("REVERT PROOF FAILED for: %s" % ", ".join(failures))
        return 1
    print("REVERT PROOF OK -- all three reverts break the suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

def test_test_fk_severity_split_revert_proof_script():
    """v1.17.91: this file was a script (main() + sys.exit()), so pytest
    collected nothing from it and its assertions had never run.

    The body is untouched: running the file directly still works, and the
    script's own sys.exit(1) on a failed assertion becomes a test failure
    instead of killing the whole pytest session."""
    try:
        _rc = main()
    except SystemExit as _exc:
        raise AssertionError("the script's own check failed and exited with %s" % (_exc.code,))
    assert _rc in (0, None), "the script returned %r" % (_rc,)
