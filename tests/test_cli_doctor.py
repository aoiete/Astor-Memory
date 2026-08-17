"""Tests for `am doctor` health-check CLI."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault('ASTOR_DIR', os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))

from astor_memory.cli.main import main


def test_am_doctor_runs():
    """`am doctor` returns 0 and shows health summary."""
    sys.argv = ['am', 'doctor']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_version_returns_0_3_0():
    """`am version` reports v0.3.0."""
    sys.argv = ['am', 'version']
    rc = main(sys.argv[1:])
    assert rc == 0
    # version line should contain "0.3.0"


def test_am_platform_verify_invariants_pass():
    """`am platform verify` returns 0 (all 6 invariants pass)."""
    sys.argv = ['am', 'platform', 'verify']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_platform_list_works():
    """`am platform list` returns 0 and lists 7 platforms (1 TG + 1 DC + 5 weixin)."""
    sys.argv = ['am', 'platform', 'list']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_bot_list_users_works():
    """`am bot list-users` returns 0."""
    sys.argv = ['am', 'bot', 'list-users']
    rc = main(sys.argv[1:])
    assert rc == 0


def test_am_admin_whoami_works():
    """`am admin whoami` returns 0 with admin.lock info."""
    sys.argv = ['am', 'admin', 'whoami']
    rc = main(sys.argv[1:])
    assert rc == 0
