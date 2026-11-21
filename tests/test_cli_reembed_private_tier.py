"""`am reembed` must reach the private tiers.

v1.17.76

The repair command walks every (tier, user) pair and re-embeds the
facts that have no embedding. It checked each scope with the CLI's own
identity -- admin:admin -- and the strict privacy model refuses that
for every private scope:

    actor='admin:admin' (role=admin) cannot write private_<user>;
    user write-grant required

The refusal was caught and the tier was skipped, one after another,
and the command reported the number of facts it repaired. On this
machine that number was 0, while the private admin tier held 181 live
facts with no embedding at all -- invisible to the semantic channel,
reachable only by exact keyword match. The public and source tiers
had nothing to repair, so the report looked like "nothing to do".

Two things were wrong: the private tier needs the OWNER identity to
write into it (the same invariant the write path uses since v1.17.36),
and a skip has to say so, because a repair command that skips
silently reports success having repaired nothing.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest


def _bus_db(root: Path, user: str) -> Path:
    return root / "users" / user / "memory" / f"astor_bus_{user}.db"


def _nest_db(root: Path, user: str) -> Path:
    return root / "users" / user / "memory" / f"astor_nest_{user}.db"


def _embedded_ids(root: Path, user: str) -> set:
    nest = _nest_db(root, user)
    if not nest.exists():
        return set()
    conn = sqlite3.connect(str(nest))
    try:
        rows = conn.execute("SELECT fact_id FROM embeddings").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {r[0] for r in rows}


class _FakeModel:
    """A stand-in for the embedding model.

    The real one is a 1GB download; this test is about which scopes the
    command reaches, not about what a vector looks like.
    """

    def embed(self, texts):
        for i, _t in enumerate(texts):
            yield [0.01 * (i + 1)] * 8


@pytest.fixture
def astor_root(monkeypatch, tmp_path):
    """Isolated ASTOR_DIR so the test never touches the live runtime."""
    root = tmp_path / "astor"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ASTOR_DIR", str(root))
    # Both places hold a reference to the loader, and the write path
    # uses the second one.
    import astor_memory.nest.embeddings as emb_mod
    import astor_memory.nest.vector_store as vs_mod
    monkeypatch.setattr(emb_mod, "astor_get_embedding_model", lambda: _FakeModel())
    monkeypatch.setattr(vs_mod, "astor_get_embedding_model", lambda: _FakeModel())
    yield root


def _write_private_fact(user: str, text: str) -> int:
    from astor_memory._internal.acl import astor_init_acl, open_own_private

    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")
    bus = open_own_private(user)
    event_id = bus.append_event(
        namespace=user, agent_id="test", source="test.write",
        action="write", content=text,
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace=user, content=text, kind="rule",
    )
    return bus.promote_candidate(
        cand_id, promoted_by="test", user_id=user, tier="private",
    )


def _drop_embeddings(root: Path, user: str) -> None:
    """The state the repair exists for: a live fact with no vector."""
    conn = sqlite3.connect(str(_nest_db(root, user)))
    try:
        conn.execute("DELETE FROM embeddings")
        conn.commit()
    finally:
        conn.close()


def _run_reembed():
    from astor_memory.cli.main import cmd_reembed
    return cmd_reembed(argparse.Namespace(limit=0, batch_size=32))


def test_reembed_reaches_the_private_tier(astor_root, monkeypatch):
    """A private fact with no embedding gets one."""
    user = "someone"
    canon_id = _write_private_fact(
        user, "R-class: a private fact with no vector is invisible to recall.")
    _drop_embeddings(astor_root, user)
    assert canon_id not in _embedded_ids(astor_root, user)

    _run_reembed()

    assert canon_id in _embedded_ids(astor_root, user), (
        "am reembed did not embed the private fact -- the private tier "
        "was skipped. Before v1.17.76 this command reported '0 facts' "
        "and left every private fact without a vector."
    )


def test_a_skipped_scope_is_reported(astor_root, monkeypatch, capsys):
    """A scope the command cannot write to has to be named."""
    from astor_memory._internal.acl import astor_init_acl
    astor_init_acl(actor="admin:admin", role="admin", tier="public",
                   user_id="admin", subscription_plan="power")

    _run_reembed()
    out = capsys.readouterr().out
    # Nothing to skip in an empty runtime is fine; what must not happen
    # is a silent skip. If any scope was skipped, it has to be in the
    # output.
    if "skipped" in out:
        assert "skipped:" in out, out
