# SYNC_HERMES_SCRIPT=True
"""Weekly: move what the private tier learned into the shared tier.

The private tier is where the methods, patterns, models and workflows
accumulate. The public tier is what peers read. This mines one for the
other, once a week.

The script is the job. No model is involved: the mining is
deterministic, and a weekly job that wakes a model to run a query is a
weekly job that costs money and can still be wrong.

The one thing this script cannot assume is its own interpreter. A
scheduled job runs under whatever Python the scheduler happens to
bootstrap, and that one may not have astor's dependencies -- a bare
interpreter cannot even import the package, because importing it pulls
in the vector store and with it numpy. So the script finds an
interpreter that can, and hands the work to it. The mining itself
never changes; only the interpreter that runs it does.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR_NAME = "Astor-Data"


def _reconfigure_streams() -> None:
    """A Windows console defaults to GBK, and a method fact can hold
    any character. The print then dies with UnicodeEncodeError halfway
    through the run, which in a cron means a job that fails on content
    that had nothing wrong with it.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8",
                               errors="replace")
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8",
                                errors="replace")
    except (AttributeError, ValueError, TypeError):
        pass


def _candidate_interpreters():
    """Interpreters worth trying, best first."""
    env = os.environ.get("ASTOR_PYTHON", "").strip()
    if env:
        yield env
    yield sys.executable
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = REPO / rel
        if p.exists():
            yield str(p)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for name in ("python.exe", "python3"):
            p = Path(d) / name
            if p.exists():
                yield str(p)


def _can_import_astor(candidate: str) -> bool:
    """The real test is an import, not a version string: importing the
    package pulls in the vector store, so a probe that only reads
    __version__ would pass on an interpreter that cannot actually run
    the mining."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    try:
        proc = subprocess.run(
            [candidate, "-c", "import astor_memory"],
            env=env, capture_output=True, text=True, timeout=120)
        return proc.returncode == 0
    except Exception:
        return False


def _find_interpreter() -> str | None:
    seen: set[str] = set()
    for candidate in _candidate_interpreters():
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        if _can_import_astor(candidate):
            return candidate
    return None


def main() -> int:
    _reconfigure_streams()

    runtime = os.environ.get("ASTOR_DIR", "").strip()
    if runtime and not Path(runtime).is_absolute():
        print("mine-public-weekly: ASTOR_DIR must be an absolute path, "
              "got %r" % runtime)
        return 2

    interpreter = _find_interpreter()
    if interpreter is None:
        print("mine-public-weekly: no interpreter on this host can import "
              "astor_memory; the mining needs one with the package's "
              "dependencies installed. Set ASTOR_PYTHON to point at it.")
        return 2

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONIOENCODING"] = "utf-8"

    # v1.17.8: this is the AUTOMATIC run, so it reads the switch. The
    # operator said "turn off automatic and it becomes manual" -- and
    # this job is the automatic part. Off means this job does
    # nothing; the manual command still works.
    #
    # The check runs in the interpreter that can import astor, because
    # that is the one whose copy of the code is the one in force.
    probe = subprocess.run(
        [interpreter, "-c",
         "import sys;from astor_memory._internal.bot_binding import "
         "distill_auto_allowed;sys.stdout.write('1' if "
         "distill_auto_allowed('admin', own=True) else '0')"],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    if probe.returncode == 0 and probe.stdout.strip() == "0":
        print("mine-public-weekly: automatic mining is switched off in the "
              "dashboard; nothing to do this week. The manual command "
              "(`am mine-public --apply`) still works.")
        return 0

    cmd = [interpreter, "-m", "astor_memory.cli.main", "mine-public", "--apply"]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800)
    except Exception as exc:
        print("mine-public-weekly: mining failed: %s: %s"
              % (type(exc).__name__, exc))
        return 1

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    for line in out.splitlines():
        print(line)
    if proc.returncode != 0:
        print("mine-public-weekly: exit %d" % proc.returncode)
        for line in err.splitlines()[-5:]:
            print("  " + line)
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
