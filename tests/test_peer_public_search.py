"""Tests for v1.14.73 Phase 4 — demand-driven Peer Public Search (PPS).

Covers:
  - PeerSearchRequest construct-time validation (replay window, sig format,
    length caps, peer_id format)
  - build_search_request end-to-end signing + verify
  - select_search_targets eligibility filter (trust, blacklist, endpoint;
    allow_search is informational — server enforces opt-in)
  - /v1/peer/public_search endpoint: 200 happy path + all 403/400 gates
    (unknown peer, blacklisted, trust<50, not opted in, bad signature,
    missing/bad req encoding)
  - set_allow_search / receiver-side opt-in flag round-trip
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_identity import (
    init_identity, sign, verify,
)
from astor_memory._internal.peer_relationships import (
    add_peer, get_peer, set_allow_search, close_all_connections,
)
from astor_memory._internal.peer_search import (
    PeerSearchRequest, PeerSearchResponse, PeerSearchResult,
    build_search_request, select_search_targets,
    MIN_TRUST_FOR_SEARCH,
)
from astor_memory._internal.test_state import astor_close_all_test_state


def _fake_peer_id(seed: str = "a") -> str:
    return "astor:" + (seed * 32)


def _future_ok_ts() -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _make_signed_request(
    requestor_peer_id: str,
    query: str = "hello world",
    limit: int = 5,
    *,
    astor_dir: str | None = None,
    sign_key: str | None = None,   # override signing key b64
    pub_key: str | None = None,
) -> PeerSearchRequest:
    """Build a validly signed request (default) using this install's key."""
    me = init_identity(astor_dir)
    ph = PeerSearchRequest(
        requestor_peer_id=requestor_peer_id,
        requestor_pubkey=pub_key or me["public_key"],
        query=query, topic=None, limit=limit,
        timestamp=_future_ok_ts(),
        signature="A" * 86 + "==",
    )
    if sign_key is None:
        sig = sign(ph.canonical_payload(), astor_dir)
    else:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        sk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(sign_key))
        sig = base64.b64encode(sk.sign(ph.canonical_payload())).decode()
    return PeerSearchRequest(
        requestor_peer_id=requestor_peer_id,
        requestor_pubkey=pub_key or me["public_key"],
        query=query, topic=None, limit=limit,
        timestamp=ph.timestamp, signature=sig,
    )


def _req_query_string(req: PeerSearchRequest) -> str:
    payload = {
        "requestor_peer_id": req.requestor_peer_id,
        "requestor_pubkey": req.requestor_pubkey,
        "query": req.query,
        "topic": req.topic or "",
        "limit": req.limit,
        "timestamp": req.timestamp,
        "signature": req.signature,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        astor_close_all_test_state()
        # Close lex singletons first — on Windows an open sqlite handle 
        # blocks TemporaryDirectory.cleanup() (PermissionError WinError 32).
        try:
            from astor_memory.nest import lex_index as _lex_mod
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for _lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try:
                        _lex.close()
                    except Exception:
                        pass
                _lex_mod._LEX_SINGLETONS.clear()
        except Exception:
            pass
        try:
            from astor_memory.nest.vector_store import astor_reset_nest
            astor_reset_nest()
        except Exception:
            pass
        close_all_connections()
        # v1.15.25 Ship I: close audit_logger singleton to release file handle
        # before tmpdir cleanup (Windows file locking).
        try:
            from astor_memory._internal.audit_logger import _reset_audit_conn
            _reset_audit_conn()
        except Exception:
            pass
        self._tmp.cleanup()


class TestRequestValidation(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_valid_request_constructs(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            req = _make_signed_request(_fake_peer_id("a"))
        self.assertTrue(req.verify_signature())

    def test_stale_timestamp_rejected(self):
        old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=9)) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.assertRaises(ValueError):
            PeerSearchRequest(
                requestor_peer_id=_fake_peer_id("a"),
                requestor_pubkey=init_identity(str(self.tmpdir))["public_key"],
                query="q", topic=None, limit=5, timestamp=old_ts,
                signature="A" * 86 + "==",
            )

    def test_future_timestamp_beyond_skew_rejected(self):
        fut = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=5)) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.assertRaises(ValueError):
            PeerSearchRequest(
                requestor_peer_id=_fake_peer_id("a"),
                requestor_pubkey=init_identity(str(self.tmpdir))["public_key"],
                query="q", topic=None, limit=5, timestamp=fut,
                signature="A" * 86 + "==",
            )

    def test_limit_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            PeerSearchRequest(
                requestor_peer_id=_fake_peer_id("a"),
                requestor_pubkey="AAAA",
                query="q", topic=None, limit=99,
                timestamp=_future_ok_ts(), signature="A" * 86 + "==",
            )

    def test_bad_peer_id_format_rejected(self):
        with self.assertRaises(ValueError):
            PeerSearchRequest(
                requestor_peer_id="nope",
                requestor_pubkey=init_identity(str(self.tmpdir))["public_key"],
                query="q", topic=None, limit=5,
                timestamp=_future_ok_ts(), signature="A" * 86 + "==",
            )

    def test_sig_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            PeerSearchRequest(
                requestor_peer_id=_fake_peer_id("a"),
                requestor_pubkey=init_identity(str(self.tmpdir))["public_key"],
                query="q", topic=None, limit=5,
                timestamp=_future_ok_ts(), signature="tooshort",
            )

    def test_build_then_verify_roundtrip(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            me = init_identity(str(self.tmpdir))
            req = build_search_request(
                query="hello", requestor_peer_id=me["peer_id"],
                requestor_pubkey=me["public_key"],
                requestor_private_key=me["private_key"],
            )
        self.assertTrue(req.verify_signature())


class TestTargetSelection(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_select_by_trust_and_endpoint(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            good = add_peer(_fake_peer_id("1"), trust=80,
                            endpoint="https://a.example.com")
            lowtrust = add_peer(_fake_peer_id("2"), trust=20,
                                endpoint="https://b.example.com")
            noendpoint = add_peer(_fake_peer_id("3"), trust=90)
            black = add_peer(_fake_peer_id("4"), kind="blacklist", trust=100,
                             endpoint="https://c.example.com")
            targets = select_search_targets(list_peers_all())
        ids = {t["peer_id"] for t in targets}
        self.assertIn(good["peer_id"], ids)
        self.assertNotIn(lowtrust["peer_id"], ids)
        self.assertNotIn(noendpoint["peer_id"], ids)
        self.assertNotIn(black["peer_id"], ids)

    def test_allow_search_flag_is_informational(self):
        # allow_search=False must NOT exclude from targets (server enforces
        # the real opt-in). But allow_search=True kept too.
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            p1 = add_peer(_fake_peer_id("1"), trust=80,
                          endpoint="https://a.example.com")
            set_allow_search(p1["peer_id"], False)
            p2 = add_peer(_fake_peer_id("2"), trust=80,
                          endpoint="https://b.example.com")
            set_allow_search(p2["peer_id"], True)
            targets = select_search_targets(list_peers_all())
        ids = {t["peer_id"] for t in targets}
        self.assertEqual(ids, {p1["peer_id"], p2["peer_id"]})


def list_peers_all():
    from astor_memory._internal.peer_relationships import list_peers
    return list_peers()


class TestPublicSearchEndpoint(_Tmp, unittest.TestCase):
    """Flask test client against /v1/peer/public_search."""

    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity(str(self.tmpdir))
            # Register the requestor as a trusted, opted-in friend.
            self.requestor_id = _fake_peer_id("b")
            add_peer(self.requestor_id, trust=80, alias="bob")
            set_allow_search(self.requestor_id, True)

    def tearDown(self):
        self.tearDown_tmp()

    def _get(self, qs: str):
        return self.client.get(
            "/v1/peer/public_search?req=" + urllib.parse.quote(qs))

    def test_missing_req_param_400(self):
        resp = self.client.get("/v1/peer/public_search")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "missing_req")

    def test_bad_encoding_400(self):
        resp = self._get("!!!not-base64!!!")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "bad_req_encoding")

    def test_missing_field_400(self):
        raw = base64.urlsafe_b64encode(json.dumps({"query": "x"}).encode())
        resp = self._get(raw.decode())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "missing_field")

    def test_stale_request_400(self):
        old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=9)) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        me = init_identity(str(self.tmpdir))
        raw = base64.urlsafe_b64encode(json.dumps({
            "requestor_peer_id": self.requestor_id,
            "requestor_pubkey": me["public_key"],
            "query": "q", "topic": "", "limit": 5,
            "timestamp": old_ts, "signature": "A" * 86 + "==",
        }, separators=(",", ":")).encode()).decode()
        resp = self._get(raw)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_request")

    def test_bad_signature_403(self):
        me = init_identity(str(self.tmpdir))
        # Valid structure, corrupted signature bytes.
        sig = bytearray(base64.b64decode("A" * 86 + "=="))
        sig[0] ^= 0xFF
        raw = base64.urlsafe_b64encode(json.dumps({
            "requestor_peer_id": self.requestor_id,
            "requestor_pubkey": me["public_key"],
            "query": "q", "topic": "", "limit": 5,
            "timestamp": _future_ok_ts(),
            "signature": base64.b64encode(bytes(sig)).decode(),
        }, separators=(",", ":")).encode()).decode()
        resp = self._get(raw)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "bad_signature")

    def test_unknown_peer_403(self):
        me = init_identity(str(self.tmpdir))
        stranger = _fake_peer_id("f")
        req = _make_signed_request(stranger, astor_dir=str(self.tmpdir))
        resp = self._get(_req_query_string(req))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "unknown_peer")

    def test_blacklisted_peer_403(self):
        me = init_identity(str(self.tmpdir))
        foe = _fake_peer_id("e")
        add_peer(foe, kind="blacklist", trust=0)
        set_allow_search(foe, True)
        req = _make_signed_request(foe, astor_dir=str(self.tmpdir))
        resp = self._get(_req_query_string(req))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "peer_blacklisted")

    def test_trust_below_threshold_403(self):
        me = init_identity(str(self.tmpdir))
        acquaintance = _fake_peer_id("c")
        add_peer(acquaintance, trust=MIN_TRUST_FOR_SEARCH - 1)
        set_allow_search(acquaintance, True)
        req = _make_signed_request(acquaintance, astor_dir=str(self.tmpdir))
        resp = self._get(_req_query_string(req))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "trust_below_threshold")

    def test_not_opted_in_403(self):
        me = init_identity(str(self.tmpdir))
        friend = _fake_peer_id("d")
        add_peer(friend, trust=80)
        # allow_search NOT set (default off) → must reject.
        req = _make_signed_request(friend, astor_dir=str(self.tmpdir))
        resp = self._get(_req_query_string(req))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "search_not_allowed")

    def test_opt_in_then_revoke_changes_result(self):
        me = init_identity(str(self.tmpdir))
        friend = _fake_peer_id("d")
        add_peer(friend, trust=80)
        req = _make_signed_request(friend, astor_dir=str(self.tmpdir))
        # Before opt-in: 403
        self.assertEqual(self._get(_req_query_string(req)).status_code, 403)
        # Opt in: passes auth gates (200 — may be empty results, but 200)
        set_allow_search(friend, True)
        self.assertEqual(self._get(_req_query_string(req)).status_code, 200)
        # Revoke: 403 again
        set_allow_search(friend, False)
        self.assertEqual(self._get(_req_query_string(req)).status_code, 403)


class TestAllowSearchFlag(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_unknown_peer_returns_false(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            self.assertFalse(set_allow_search(_fake_peer_id("z"), True))

    def test_roundtrip(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            p = add_peer(_fake_peer_id("1"), trust=50)
            self.assertTrue(set_allow_search(p["peer_id"], True))
            row = get_peer(p["peer_id"])
            self.assertTrue((row["metadata"] or {}).get("allow_search"))
            self.assertTrue(set_allow_search(p["peer_id"], False))
            row = get_peer(p["peer_id"])
            self.assertFalse((row["metadata"] or {}).get("allow_search"))

    def test_preserves_other_metadata(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            p = add_peer(_fake_peer_id("1"), trust=50,
                         metadata={"note": "college friend"})
            set_allow_search(p["peer_id"], True)
            row = get_peer(p["peer_id"])
            self.assertEqual(row["metadata"].get("note"), "college friend")
            self.assertTrue(row["metadata"].get("allow_search"))


class TestResponseShape(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        r = PeerSearchResult(
            source_peer_id=_fake_peer_id("1"), source_trust=100,
            fact_id=1, content="hello", kind="fact", tags=("x",),
            created_at="2026-09-27T00:00:00Z", relevance=0.9,
        )
        resp = PeerSearchResponse(
            requestor_peer_id=_fake_peer_id("2"),
            results=(r,), truncated=False,
        )
        d = resp.to_dict()
        self.assertEqual(d["results"][0]["content"], "hello")
        self.assertEqual(d["results"][0]["tags"], ["x"])
        # JSON roundtrip
        json.dumps(d)  # must not raise

    def test_relevance_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            PeerSearchResult(
                source_peer_id=_fake_peer_id("1"), source_trust=100,
                fact_id=1, content="c", kind="fact", tags=(),
                created_at="", relevance=1.5,
            )


if __name__ == "__main__":
    unittest.main()
