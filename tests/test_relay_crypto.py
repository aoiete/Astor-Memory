"""The relay must never be able to read a message.

v1.17.20 / relay slice 1. These tests pin the properties the whole
relay design rests on:

  - a sealed envelope round-trips
  - a tampered envelope fails, loudly
  - the wrong recipient key fails, loudly
  - a plaintext payload is not accepted (no downgrade)
  - the sender's long-term key does not appear in the envelope
  - key_id is the fingerprint of the identity key, and two different
    keys do not share one
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory._internal import relay_crypto as rc  # noqa: E402


class TestRelayCrypto(unittest.TestCase):

    def setUp(self):
        self.a_pub, self.a_priv = rc.generate_x25519_keypair()
        self.b_pub, self.b_priv = rc.generate_x25519_keypair()
        self.msg = b"the search request, in the clear before sealing"

    def test_round_trip(self):
        env = rc.seal(self.b_pub, self.msg)
        self.assertEqual(rc.open_envelope(env, self.b_priv), self.msg)

    def test_the_sender_key_is_not_in_the_envelope(self):
        env = rc.seal(self.b_pub, self.msg)
        blob = repr(env)
        self.assertNotIn(self.a_priv, blob)
        self.assertNotIn(self.b_pub, blob)

    def test_the_plaintext_is_not_in_the_envelope(self):
        env = rc.seal(self.b_pub, self.msg)
        self.assertNotIn(b"the search request".decode(), repr(env))

    def test_a_tampered_envelope_fails(self):
        env = rc.seal(self.b_pub, self.msg)
        raw = bytearray(base64.b64decode(env["ct"]))
        raw[0] ^= 0x01
        env["ct"] = base64.b64encode(bytes(raw)).decode()
        with self.assertRaises(rc.RelayCryptoError):
            rc.open_envelope(env, self.b_priv)

    def test_the_wrong_recipient_key_fails(self):
        env = rc.seal(self.b_pub, self.msg)
        _c_pub, c_priv = rc.generate_x25519_keypair()
        with self.assertRaises(rc.RelayCryptoError):
            rc.open_envelope(env, c_priv)

    def test_a_tampered_ephemeral_key_fails(self):
        env = rc.seal(self.b_pub, self.msg)
        raw = bytearray(base64.b64decode(env["eph"]))
        raw[0] ^= 0x01
        env["eph"] = base64.b64encode(bytes(raw)).decode()
        with self.assertRaises(rc.RelayCryptoError):
            rc.open_envelope(env, self.b_priv)

    def test_plaintext_is_refused(self):
        # a relay that answered with a plain string would be trying to
        # make the client fall back to plaintext
        self.assertFalse(rc.is_sealed("just a string"))
        self.assertFalse(rc.is_sealed({"payload": "raw"}))
        self.assertFalse(rc.is_sealed({"v": 1, "ct": "x"}))
        env = rc.seal(self.b_pub, self.msg)
        self.assertTrue(rc.is_sealed(env))

    def test_an_unknown_version_is_refused(self):
        env = rc.seal(self.b_pub, self.msg)
        env["v"] = 99
        with self.assertRaises(rc.RelayCryptoError):
            rc.open_envelope(env, self.b_priv)

    def test_each_message_gets_a_fresh_ephemeral_key(self):
        e1 = rc.seal(self.b_pub, self.msg)
        e2 = rc.seal(self.b_pub, self.msg)
        self.assertNotEqual(e1["eph"], e2["eph"])
        self.assertNotEqual(e1["ct"], e2["ct"])

    def test_key_id_is_the_fingerprint_of_the_identity_key(self):
        k1 = rc.key_id_for(self.a_pub)
        k2 = rc.key_id_for(self.b_pub)
        self.assertEqual(len(k1), 16)
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, rc.key_id_for(self.a_pub))

    def test_the_registration_payload_binds_key_id_and_key(self):
        p = rc.registration_payload("abc123", self.a_pub, "nonce-1")
        self.assertIn(b"abc123", p)
        self.assertIn(self.a_pub.encode(), p)
        self.assertIn(b"nonce-1", p)


if __name__ == "__main__":
    unittest.main()
