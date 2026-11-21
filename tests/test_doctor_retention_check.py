"""astor-doctor must report the retention gap.

v1.17.77

A fact that is in the store but has no embedding is captured and not
retained: the semantic channel cannot reach it, so it comes back only
on an exact keyword match. On this machine 181 of the 5430 live facts
in the private admin tier were in that state, and two of the
hand-labelled answers on the paraphrase set were among them.

The check that would have said so did not exist, and the command that
was supposed to repair them could not reach the tier they were in --
it reported "0 facts" while skipping every private scope. The
diagnostic that exposed this (capture and retention measured
separately, arXiv 2607.01523) is worth keeping as a routine check
rather than as something a paper has to be read for.

The doctor also never collected the per-user tiers at all: it globbed
public/memory, private/memory, source/memory and users/*.db, and the
per-user databases live in users/<name>/memory/. The tier with the
most facts was not in the scan.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "astor_doctor.py"


def _load_doctor():
    spec = importlib.util.spec_from_file_location("astor_doctor_mod", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["astor_doctor_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A runtime with one user tier: a bus with a live fact, and a nest
    with no embedding for it."""
    mem = tmp_path / "users" / "someone" / "memory"
    mem.mkdir(parents=True)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))

    bus = sqlite3.connect(str(mem / "astor_bus_someone.db"))
    bus.execute("""CREATE TABLE memory_canonical (
        id INTEGER PRIMARY KEY, content TEXT, candidate_id INTEGER,
        event_id INTEGER, namespace TEXT, tombstoned INTEGER DEFAULT 0)""")
    bus.execute("""INSERT INTO memory_canonical
        (id, content, candidate_id, event_id, namespace, tombstoned)
        VALUES (1, 'a fact nobody can recall semantically', 1, 1, 'someone', 0)""")
    bus.commit()
    bus.close()

    nest = sqlite3.connect(str(mem / "astor_nest_someone.db"))
    nest.execute("""CREATE TABLE embeddings (
        fact_id INTEGER PRIMARY KEY, embedding BLOB, model_name TEXT,
        dim INTEGER, user_id TEXT, tier TEXT)""")
    nest.commit()
    nest.close()
    return tmp_path


def test_the_retention_gap_is_reported(runtime, capsys):
    doctor = _load_doctor()
    rc = doctor.cmd_check(argparse_ns())
    out = capsys.readouterr().out
    assert "without an embedding" in out, out
    assert "1" in out.split("without an embedding")[0].rsplit("retention:", 1)[-1], out
    assert rc == 1, (
        "a live fact with no embedding is a real gap -- the check has to "
        "exit non-zero so a cron or a person notices")


def test_a_fully_embedded_tier_is_clean(runtime, capsys):
    doctor = _load_doctor()
    nest = sqlite3.connect(str(runtime / "users" / "someone" / "memory"
                               / "astor_nest_someone.db"))
    nest.execute("""INSERT INTO embeddings
        (fact_id, embedding, model_name, dim, user_id, tier)
        VALUES (1, X'00', 'm', 4, 'someone', 'private')""")
    nest.commit()
    nest.close()

    rc = doctor.cmd_check(argparse_ns())
    out = capsys.readouterr().out
    assert "0 without an embedding" in out, out


def test_the_user_tiers_are_collected(runtime):
    """The per-user databases are one level deeper than the glob looked."""
    doctor = _load_doctor()
    names = {p.name for p in doctor.collect_dbs()}
    assert "astor_bus_someone.db" in names, names
    assert "astor_nest_someone.db" in names, names


def argparse_ns():
    import argparse
    return argparse.Namespace(apply=False)
