"""The read cache must not serve a fanout response to a plain read.

`peer_fanout` changes the result: a fanout read returns the peer
network's hits on top of the local ones. The cache key covers every
input that changes the result -- kinds, wing, session_id, consult were
all added to it for exactly that reason -- and peer_fanout was missed.

The symptom is invisible in the response body. A cached fanout read
comes back with the peer results folded in, and a cached plain read
comes back without them, but both look like a normal answer. The
`cached: true` flag is the only hint, and a caller who has never seen
a fanout response does not know to look for one.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def client():
    """A Flask test client with the app in a temp ASTOR_DIR."""
    import os
    import tempfile

    tmp = tempfile.mkdtemp(prefix="astor-cache-test-")
    os.environ["ASTOR_DIR"] = tmp
    os.environ["ASTOR_TEST_NO_PREWARM"] = "1"
    from astor_memory.server import create_app

    app = create_app(astor_dir=tmp)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _read(client, query, **extra):
    body = {"query": query, "tier": "public"}
    body.update(extra)
    return client.post("/v1/read", data=json.dumps(body),
                       content_type="application/json")


def test_fanout_and_plain_read_do_not_share_a_cache_entry(client):
    """A peer_fanout read and a plain read of the same query differ."""
    query = "cache-key-fanout-probe-xyzzy-12345"

    plain = _read(client, query).get_json()
    assert plain is not None
    assert plain.get("cached") is not True

    fanned = _read(client, query, peer_fanout=True).get_json()
    assert fanned is not None
    # The fanout read must not be served the plain read's cached body.
    assert fanned.get("cached") is not True
    # And the two must not be the same object.
    assert fanned.get("results") is not plain.get("results")


def test_a_cached_response_carries_its_own_flag(client):
    """The cached response is served, and it says so."""
    query = "cache-key-repeat-probe-abc-98765"
    _read(client, query)
    second = _read(client, query).get_json()
    assert second.get("cached") is True


def test_opt_out_is_not_served_a_cached_fanout(client, monkeypatch):
    """peer_fanout=false must not receive a cached fanout response.

    The failure this guards against: a node that fans out by default
    caches the fanout response, and a caller that explicitly opts out
    of the peer network still gets the peer's facts back.
    """
    import astor_memory.server as srv

    # Make the dispatch produce something observable.
    calls = []

    def _fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"peer_results": [{"fact_id": 999999, "content": "from a peer"}],
                "per_peer": [], "local_error": None}

    monkeypatch.setattr(
        "astor_memory._internal.peer_recall.dispatch_peer_fanout",
        _fake_dispatch)

    query = "cache-key-optout-probe-zzz-55555"
    fanned = _read(client, query, peer_fanout=True).get_json()
    assert fanned is not None
    assert fanned.get("cached") is not True
    assert calls, "the fanout dispatch never ran"

    opted_out = _read(client, query, peer_fanout=False).get_json()
    assert opted_out is not None
    assert opted_out.get("cached") is not True, (
        "a peer_fanout=false read was served the cached fanout response")
