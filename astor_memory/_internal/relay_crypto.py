"""End-to-end encryption for the relay.

The relay is the postman, not the reader: everything it holds is
ciphertext, and this module is the only place that knows how to turn
ciphertext back into a message.

Two key pairs per node, for two different jobs:

  ed25519  -- identity. Signs what the node says ("this search came
              from me"), and key_id = sha256(ed25519 public key)[:16]
              is the node's address on the relay.
  x25519   -- key agreement. A sealed box needs a Diffie-Hellman
              shared secret, and a signing key cannot produce one.

The seal mixes in a fresh ephemeral key for every message, so the same
key pair never seals the same bytes twice.

What this gives, stated exactly:

  - the relay cannot read the content. It holds a box sealed to the
    recipient's x25519 key, and it has neither that key nor the
    sender's ephemeral private key -- the sender throws that one away
    after sealing.
  - the sender needs no long-term encryption key at all: it seals with
    a fresh ephemeral key every time, so nothing about the sender's
    key material is recoverable from the traffic.
  - a later compromise of the RECIPIENT's long-term x25519 key WOULD
    reveal the traffic the relay captured for that recipient, because
    the recipient's key is what the box is sealed to.

The design document called the last property forward secrecy. What the
implementation actually guarantees is that the relay cannot read the
content. Both properties are useful; only one of them is what we
claim, and the difference is written down here rather than left to a
reader of the protocol section to guess.

Implementation note: PyNaCl is an optional dependency and is not
installed everywhere, so this uses `cryptography` -- X25519 for the
agreement, HKDF-SHA256 to derive the symmetric key, ChaCha20-Poly1305
for the AEAD. That is the same shape as NaCl Box (X25519 +
XSalsa20-Poly1305) with an explicit KDF in the middle.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION = 1
_INFO = b"astor-relay-v1"
_NONCE_SIZE = 12  # ChaCha20-Poly1305
_KEY_SIZE = 32


class RelayCryptoError(Exception):
    """Anything that means "this envelope is not for us, or is broken"."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:
        raise RelayCryptoError(f"not valid base64: {exc}") from exc


# --- key material ----------------------------------------------------

def generate_x25519_keypair() -> tuple[str, str]:
    """(public_key_b64, private_key_b64). The private key never leaves
    the node."""
    sk = X25519PrivateKey.generate()
    priv = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64e(pub), _b64e(priv)


def key_id_for(public_key_b64: str) -> str:
    """The node's address: the fingerprint of its identity key.

    The identity key is the ed25519 one -- the same key that signs
    peer messages -- so key_id names the same thing the peer protocol
    already talks about.
    """
    return hashlib.sha256(_b64d(public_key_b64)).hexdigest()[:16]


def registration_payload(key_id: str, public_key_b64: str,
                         nonce: str) -> bytes:
    """The bytes a node signs to prove it holds the private key behind
    the key_id it claims."""
    return f"{key_id}|{public_key_b64}|{nonce}".encode("utf-8")


# --- the sealed box --------------------------------------------------

def _derive(shared: bytes, eph_pub: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=eph_pub + recipient_pub,
        info=_INFO,
    ).derive(shared)


def seal(recipient_x25519_pub_b64: str, plaintext: bytes) -> dict[str, Any]:
    """Seal a message for the recipient.

    The sender contributes a fresh ephemeral key every time; the
    recipient's key is the long-term one its peers hold. That is what
    makes the seal unique per message -- sealing twice with the same
    pair would produce the same ciphertext twice, which tells a reader
    of the relay that two messages are identical.

    Returns a JSON-able envelope. The relay stores and forwards exactly
    this and nothing else.
    """
    try:
        recipient_pub = X25519PublicKey.from_public_bytes(
            _b64d(recipient_x25519_pub_b64))
    except Exception as exc:
        raise RelayCryptoError(
            f"recipient key is not a valid x25519 key: {exc}") from exc

    eph = X25519PrivateKey.generate()

    eph_pub = eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    shared = eph.exchange(recipient_pub)
    key = _derive(shared, eph_pub, _b64d(recipient_x25519_pub_b64))
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aad = eph_pub  # bind the ephemeral key into the tag
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return {
        "v": ENVELOPE_VERSION,
        "eph": _b64e(eph_pub),
        "nonce": _b64e(nonce),
        "ct": _b64e(ct),
    }


def open_envelope(envelope: dict[str, Any],
                  recipient_x25519_priv_b64: str) -> bytes:
    """Open a sealed envelope. Raises RelayCryptoError on anything that
    is not exactly the message we were expecting."""
    if not isinstance(envelope, dict):
        raise RelayCryptoError("envelope is not an object")
    if envelope.get("v") != ENVELOPE_VERSION:
        raise RelayCryptoError(
            f"unsupported envelope version {envelope.get('v')!r}")
    for field in ("eph", "nonce", "ct"):
        if not envelope.get(field):
            raise RelayCryptoError(f"envelope is missing {field!r}")

    try:
        eph_pub = _b64d(envelope["eph"])
        nonce = _b64d(envelope["nonce"])
        ct = _b64d(envelope["ct"])
    except RelayCryptoError:
        raise
    except Exception as exc:
        raise RelayCryptoError(f"envelope is malformed: {exc}") from exc

    try:
        sk = X25519PrivateKey.from_private_bytes(
            _b64d(recipient_x25519_priv_b64))
        peer_pub = X25519PublicKey.from_public_bytes(eph_pub)
    except Exception as exc:
        raise RelayCryptoError(f"key material is unusable: {exc}") from exc

    shared = sk.exchange(peer_pub)
    # the salt has to be the same on both sides: the sender's ephemeral
    # public key and the recipient's own public key -- the receiver
    # knows the second one because it is the public half of the private
    # key it just used.
    own_pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key = _derive(shared, eph_pub, own_pub)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ct, eph_pub)
    except InvalidTag as exc:
        raise RelayCryptoError(
            "envelope failed authentication -- wrong recipient key, "
            "tampered, or not for us") from exc


def is_sealed(envelope: Any) -> bool:
    """A plaintext payload is a downgrade attempt: the client only ever
    accepts a sealed envelope (D: no plaintext fallback)."""
    return (isinstance(envelope, dict)
            and envelope.get("v") == ENVELOPE_VERSION
            and bool(envelope.get("ct"))
            and bool(envelope.get("eph"))
            and bool(envelope.get("nonce")))


def node_x25519_keys(astor_dir: str | None = None) -> tuple[str, str]:
    """This node's x25519 pair, generated on first use and stored next
    to the identity keypair."""
    from .peer_identity import _identity_dir, _KEYPAIR_FILE
    base = astor_dir or os.environ.get("ASTOR_DIR") or os.path.expanduser("~/.astor")
    path = _identity_dir(base) / _KEYPAIR_FILE
    data: dict[str, Any] = {}
    if path.exists():
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pub = data.get("x25519_public_key")
    priv = data.get("x25519_private_key")
    if not (pub and priv):
        pub, priv = generate_x25519_keypair()
        data["x25519_public_key"] = pub
        data["x25519_private_key"] = priv
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(__import__("json").dumps(data, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
    return pub, priv
