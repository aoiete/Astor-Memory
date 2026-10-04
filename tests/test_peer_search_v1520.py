"""Regression tests for v1.15.20 bugfix — peer_search_local AttributeError.

Ship v1.15.19 had a bug where /v1/peer/search called `r.to_dict()` on every
result, but PeerSearchResult has no `to_dict` method — only the enclosing
PeerSearchResponse does. v1.15.20 added `to_dict()` to PeerSearchResult
and used an inline serializer on the REST endpoint. This test pins both.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_search import (
    PeerSearchResult, PeerSearchResponse,
)
from astor_memory.server import create_app
from astor_memory._internal.test_state import astor_close_all_test_state


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        astor_close_all_test_state()
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
            astor_reset_nest()
            try:
                from astor_memory.bus.store import (
                    astor_reset_bus, _BUS_SINGLETONS,
                )
                astor_reset_bus()
                if _BUS_SINGLETONS is not None:
                    for _b in list(_BUS_SINGLETONS.values()):
                        try: _b.close()
                        except Exception: pass
                    _BUS_SINGLETONS.clear()
            except Exception: pass
            close_all_connections()
        except Exception:
            pass
        self._tmp.cleanup()


class TestPeerSearchResultHasToDict(_Tmp, unittest.TestCase):
    """v1.15.20 added to_dict to PeerSearchResult — pin it."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_to_dict_returns_expected_keys(self):
        r = PeerSearchResult(
            source_peer_id="astor:" + "a" * 32,
            source_trust=100, fact_id=42,
            content="hello world", kind="fact",
            tags=("imported", "r132"),
            created_at="2026-09-27T00:00:00Z",
            relevance=0.97,
        )
        d = r.to_dict()
        self.assertEqual(d["source_peer_id"], "astor:" + "a" * 32)
        self.assertEqual(d["source_trust"], 100)
        self.assertEqual(d["fact_id"], 42)
        self.assertEqual(d["content"], "hello world")
        self.assertEqual(d["kind"], "fact")
        self.assertEqual(d["tags"], ["imported", "r132"])
        self.assertEqual(d["created_at"], "2026-09-27T00:00:00Z")
        self.assertEqual(d["relevance"], 0.97)


class TestPeerSearchLocalEndpointReturns200(_Tmp, unittest.TestCase):
    """v1.15.19 sent `r.to_dict()` in /v1/peer/search — AttributeError → 500.
    v1.15.20 inline-serializes each result. Pin the 200 path.
    """

    def setUp(self):
        self.setUp_tmp()
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_search_no_friends_returns_200(self):
        # No peers → targets=0 → returns 200 with empty results.
        resp = self.client.get("/v1/peer/search?q=hello")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["targets"], 0)
        self.assertIn("hint", d)


if __name__ == "__main__":
    unittest.main()
