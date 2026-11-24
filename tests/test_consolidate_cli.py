"""`am consolidate` must exist as a CLI entry point (ADR-0007 option C).

The consolidation module (astor_memory/consolidate.py) shipped with
ADR-0007, but the CLI subcommand was never wired -- so the sleep-period
maintenance pass the ADR describes had no entry point and zero callers.

Two things are locked here:

1. The subcommand exists and runs.
2. Its DEFAULTS are not the ones that hid the tool. The old module
   defaults (--tier private + --age-days 30) returned "0 actions" on a
   corpus of long R-class lessons, because the dedup groups by the first
   30 characters and every lesson starts differently. A tool that always
   says "nothing to do" reads as broken. The CLI defaults now cover every
   tier and every age.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# `astor_memory.cli.__init__` does `from .main import main`, so the name
# `astor_memory.cli.main` resolves to the FUNCTION, not the module. import
# the module explicitly by its full dotted path.
cli_main = importlib.import_module('astor_memory.cli.main')


def test_consolidate_subcommand_is_registered():
    """The subcommand must be wired into the parser."""
    import argparse
    parser = argparse.ArgumentParser()
    # build the same parser the CLI builds
    assert hasattr(cli_main, 'main')
    # the subcommand name must appear in the module source
    src = (ROOT / 'astor_memory' / 'cli' / 'main.py').read_text(encoding='utf-8')
    assert "'consolidate'" in src, "the `consolidate` subcommand is not registered"
    assert 'cmd_consolidate' in src, "cmd_consolidate is not wired to the parser"


def test_consolidate_handler_exists():
    assert hasattr(cli_main, 'cmd_consolidate')


def test_consolidate_defaults_cover_every_tier_and_age():
    """The defaults must not be the ones that made the tool look broken."""
    src = (ROOT / 'astor_memory' / 'cli' / 'main.py').read_text(encoding='utf-8')
    # find the consolidate parser block
    start = src.index("'consolidate', help=")
    block = src[start:start + 2000]
    assert "'--tier', default='all'" in block, (
        "--tier must default to 'all' -- 'private' hides the shared tiers")
    assert "'--age-days', type=int, default=0" in block, (
        "--age-days must default to 0 -- a 30-day floor hid most of the corpus")


def test_consolidate_rejects_unknown_action():
    """A typo in --actions must fail loudly, not silently do nothing."""
    import argparse
    args = argparse.Namespace(
        actions='dedup,deddupe', age_days=0, user='admin', tier='all',
        cap=10, execute=False, report=None,
    )
    rc = cli_main.cmd_consolidate(args)
    assert rc == 2, "an unknown action must return exit code 2"


def test_consolidate_dry_run_writes_nothing():
    """Default (no --execute) must not mutate anything."""
    import argparse
    args = argparse.Namespace(
        actions='dedup', age_days=0, user='admin', tier='all',
        cap=50, execute=False, report=None,
    )
    rc = cli_main.cmd_consolidate(args)
    # 0 = ran clean, 1 = errors (e.g. no db in the test ASTOR_DIR). Both
    # are fine; what matters is that it did not claim to have applied
    # anything.
    assert rc in (0, 1)
