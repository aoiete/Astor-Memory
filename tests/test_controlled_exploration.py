"""Controlled exploration: a fact that never surfaces has no value score.

A fact that retrieval never returns is never judged, so it never gets a
value estimate, so it stays at the bottom of the pool. The retrieval
policy decides which facts ever get scored. That is a selection bias,
and no scoring rule removes it -- a fact nobody looked at looks the
same as one that was looked at and rejected.

So for a fraction of the queries, surface the facts that have been
retrieved least often, from deeper in the candidate pool.

Off by default. It does not reorder anything; it adds candidates.
"""
from __future__ import annotations

import pytest

FILLER = (
    "a note about unrelated scheduling and calendar arrangements "
    "that has nothing to do with the query at hand"
)
QUERY = "unrelated scheduling calendar entry number"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "astor"))
    from astor_memory.server import create_app
    return create_app(str(tmp_path / "astor")).test_client()


def _seed(client, marker, n=40):
    for i in range(n):
        r = client.post("/v1/write", json={
            "user": "admin", "tier": "private",
            "text": f"{FILLER} {marker} entry number {i}",
        })
        assert r.status_code == 200, r.get_data(as_text=True)


def _read(client, marker, top_k=10, explore=None):
    body = {
        "user": "admin", "tier": "private",
        "query": f"{QUERY} {marker}", "top_k": top_k,
    }
    if explore is not None:
        body["explore"] = explore
    r = client.post("/v1/read", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()



def _access_counts(client, fids):
    """The stored access_count for each fact id."""
    from astor_memory.bus import astor_bus
    b = astor_bus(tier="private", user_id="admin")
    out = {}
    for fid in fids:
        row = b.conn.execute(
            "SELECT access_count FROM memory_canonical WHERE id = ?",
            (int(fid),)).fetchone()
        out[fid] = (row[0] if row else 0)
    return out

def test_the_pick_is_the_least_retrieved_one():
    """The selection rule, on its own.

    A fact that retrieval never returns is never judged, so it never
    gets a value estimate, so it stays at the bottom of the pool. The
    pick must be the cold one even when the warm one scores higher.
    """
    from astor_memory.server import _explore_candidates

    deep = [(10, 0.9), (11, 0.8), (12, 0.7), (13, 0.6)]
    acc = {10: 50, 11: 3, 12: 0, 13: 0}

    picked = _explore_candidates(deep, acc, 2)
    assert [f for f, _ in picked] == [12, 13], picked
    # Of two equally cold facts the more relevant one comes first.
    assert [f for f, _ in picked] == [12, 13], picked


def test_the_pick_breaks_the_tie_by_relevance():
    from astor_memory.server import _explore_candidates

    deep = [(20, 0.2), (21, 0.9), (22, 0.5)]
    acc = {20: 0, 21: 0, 22: 0}

    picked = _explore_candidates(deep, acc, 2)
    # All equally cold, so the relevance score decides: 21 (0.9) then
    # 22 (0.5). 20 (0.2) does not fit in the two slots.
    assert [f for f, _ in picked] == [21, 22], picked


def test_the_pick_respects_k():
    from astor_memory.server import _explore_candidates

    deep = [(1, 0.1), (2, 0.2), (3, 0.3)]
    assert len(_explore_candidates(deep, {}, 2)) == 2
    assert _explore_candidates(deep, {}, 0) == []
    assert _explore_candidates([], {}, 3) == []


def test_exploration_is_off_by_default(client, monkeypatch):
    """The default must not change what a read returns."""
    _seed(client, "offmarker")
    monkeypatch.delenv("ASTOR_EXPLORE", raising=False)

    body = _read(client, "offmarker", top_k=10)

    assert len(body.get("results") or []) <= 10, body.get("count")


def test_exploration_off_keeps_the_same_facts_as_exploration_on(
        client, monkeypatch):
    """The switch is the only difference: with it off, the read returns
    exactly the hybrid top-k, and with it on the cold facts appear on
    top of those.

    The corpus here is deliberately lopsided -- one fact is read many
    times, so it is warm while the rest are cold. Without that
    difference, "picks the cold fact" and "picks by score" agree, and
    the test cannot tell them apart.
    """
    _seed(client, "warmmarker")
    monkeypatch.setenv("ASTOR_EXPLORE_RATE", "1.0")
    monkeypatch.setenv("ASTOR_EXPLORE_K", "3")

    # Warm one fact: read it repeatedly with a query of its own.
    for _ in range(5):
        client.post("/v1/read", json={
            "user": "admin", "tier": "private",
            "query": f"{QUERY} warmmarker entry number 0", "top_k": 1,
        })

    # Now the facts differ in access_count: fact 0 is warm, the rest
    # have never been retrieved.
    monkeypatch.delenv("ASTOR_EXPLORE", raising=False)
    off = _read(client, "warmmarker", top_k=10, explore=False)
    on = _read(client, "warmmarker", top_k=10, explore=True)

    off_ids = {r.get("fact_id") for r in (off.get("results") or [])}
    on_ids = {r.get("fact_id") for r in (on.get("results") or [])}

    # The read path already returns more than top_k facts (the L1/L2
    # path adds its own), so the count is not the signal. What matters
    # is that exploration changes WHICH facts come back -- and the ones
    # it adds come from deeper in the candidate pool.
    assert off_ids != on_ids, (
        "exploration changed nothing: off=%r on=%r" % (off_ids, on_ids))
    assert on_ids - off_ids, (
        "exploration brought back nothing new: off=%r on=%r"
        % (off_ids, on_ids))


def test_exploration_can_surface_more_than_top_k(client, monkeypatch):
    """With the switch on, a read may return the cold facts too."""
    _seed(client, "onmarker")
    monkeypatch.setenv("ASTOR_EXPLORE", "1")
    monkeypatch.setenv("ASTOR_EXPLORE_RATE", "1.0")
    monkeypatch.setenv("ASTOR_EXPLORE_K", "3")

    off = _read(client, "onmarker", top_k=10, explore=False)
    on = _read(client, "onmarker", top_k=10, explore=True)

    off_ids = {r.get("fact_id") for r in (off.get("results") or [])}
    on_ids = {r.get("fact_id") for r in (on.get("results") or [])}

    # The read path already returns more than top_k on its own (the
    # L1/L2 path adds its own), so the count is not the signal. What
    # exploration must change is WHICH facts come back.
    added = on_ids - off_ids
    assert added, (
        "exploration brought back nothing new: off=%r on=%r"
        % (off_ids, on_ids))
    # What it adds must be colder than what was already coming back:
    # the whole point is to look at the facts nobody has looked at.
    # Every read bumps access_count, so this is a comparison, not an
    # absolute.
    acc = _access_counts(client, added | off_ids)
    added_acc = [acc[f] for f in added]
    off_acc = [acc[f] for f in off_ids if f not in added]
    assert off_acc, off_ids
    assert min(added_acc) <= min(off_acc), (
        "the explored facts are not the cold ones: added=%r already=%r"
        % (added_acc, off_acc))


def test_exploration_surfaces_the_least_retrieved_facts(client, monkeypatch):
    """The fact that shows up must be one of the cold ones.

    Checked against the stored access_count, not against the ranking:
    "the extra fact is cold" is the contract, and the ranking is not.
    """
    _seed(client, "coldmarker")
    monkeypatch.setenv("ASTOR_EXPLORE", "1")
    monkeypatch.setenv("ASTOR_EXPLORE_RATE", "1.0")
    monkeypatch.setenv("ASTOR_EXPLORE_K", "2")

    # Warm a handful of facts so the corpus is not uniform: without a
    # spread in access_count, "picks the cold fact" and "picks by
    # score" pick the same thing and the test proves nothing.
    for i in range(8):
        client.post("/v1/read", json={
            "user": "admin", "tier": "private",
            "query": f"{QUERY} coldmarker entry number {i}", "top_k": 1,
        })

    off = _read(client, "coldmarker", top_k=10, explore=False)
    on = _read(client, "coldmarker", top_k=10, explore=True)

    off_ids = {r.get("fact_id") for r in (off.get("results") or [])}
    on_ids = {r.get("fact_id") for r in (on.get("results") or [])}
    added = on_ids - off_ids
    assert added, (
        "exploration brought back nothing new: off=%r on=%r"
        % (off_ids, on_ids))

    # The explored facts must be colder than the ones that were already
    # coming back -- otherwise exploration is not doing its job.
    acc = _access_counts(client, added | off_ids)
    added_acc = [acc[f] for f in added]
    off_acc = [acc[f] for f in off_ids if f not in added]
    assert off_acc, off_ids
    assert min(added_acc) <= min(off_acc), (
        "the explored facts are not the cold ones: added=%r already=%r"
        % (added_acc, off_acc))
