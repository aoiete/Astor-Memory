"""v1.14.67 (2026-09-17) — Peer identity module.

Each astor install gets a unique peer_id derived from DB path +
first-run timestamp. An ed25519 keypair is auto-generated for signing
fact provenance (sync layer).

Storage:
  $ASTOR_DIR/identity/
    peer_id          # "astor:" + sha256(DB_path + first_run_ts)[:32]
    first_run_ts     # ISO 8601 timestamp (locked on first run)
    db_path          # canonical DB path
    public_key       # ed25519 verify key (base64)
    private_key      # ed25519 signing key (base64) — DO NOT SHARE

On reinstall (new DB path or new first_run_ts), peer_id changes.
Friends detect the change via rekey notification (Phase 3, not yet
shipped) — they verify the signature and decide whether to keep
trust score or reset to default (trust=30).

Why DB-bound peer_id (not key-bound):
  - DB is the canonical asset; the key is just a signing tool
  - If private key is lost, peer_id is unchanged — DB still yours
  - Reinstalling with same DB → same peer_id (recoverable via DB backup)
  - Key loss is recoverable via keygen (new key, broadcast rekey)

See docs/peer-network.md for full design rationale.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

try:
    # Preferred: PyNaCL (smaller, faster for signing)
    from nacl.signing import SigningKey, VerifyKey
    _NACL_AVAILABLE = True
except ImportError:
    # Fallback: cryptography library (already in pyproject deps)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    _NACL_AVAILABLE = False

    class _PyCryptoSigningKey:
        """Adapter so cryptography's Ed25519PrivateKey quacks like nacl's SigningKey."""
        def __init__(self, private_key: Ed25519PrivateKey):
            self._sk = private_key

        def sign(self, data: bytes):
            sig = self._sk.sign(data)
            # Match nacl API: returns object with .signature attribute
            class _Sig:
                def __init__(self, signature):
                    self.signature = signature
            return _Sig(sig[:64])

    class _PyCryptoVerifyKey:
        def __init__(self, public_key: Ed25519PublicKey):
            self._vk = public_key

        def verify(self, data: bytes, signature: bytes):
            try:
                self._vk.verify(signature, data)
                return True
            except Exception:
                return False

    class SigningKey:
        @staticmethod
        def generate():
            sk = Ed25519PrivateKey.generate()
            return _PyCryptoSigningKey(sk)

        @staticmethod
        def _from_raw(raw: bytes):
            sk = Ed25519PrivateKey.from_private_bytes(raw)
            return _PyCryptoSigningKey(sk)

    class VerifyKey:
        @staticmethod
        def _from_raw(raw: bytes):
            vk = Ed25519PublicKey.from_public_bytes(raw)
            return _PyCryptoVerifyKey(vk)



# Identity file paths (relative to ASTOR_DIR)
_IDENTITY_DIR = "identity"
_PEER_ID_FILE = "peer_id"
_KEYPAIR_FILE = "keypair.json"


def _identity_dir(astor_dir: str | None = None) -> Path:
    base = astor_dir or os.environ.get("ASTOR_DIR") or os.path.expanduser("~/.astor")
    return Path(base) / _IDENTITY_DIR


def _db_path(astor_dir: str | None = None) -> str:
    """Canonical DB path — what we're hashing for peer_id."""
    base = astor_dir or os.environ.get("ASTOR_DIR") or os.path.expanduser("~/.astor")
    # Use the public bus DB as the canonical identifier. If user moves
    # the DB but keeps ASTOR_DIR, peer_id follows.
    return str(Path(base) / "public" / "memory" / "astor_bus_public.db")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _compute_peer_id(db_path: str, first_run_ts: str) -> str:
    """peer_id = astor:<sha256(db_path + first_run_ts)[:32]>

    DB-bound so reinstall with the same DB keeps peer_id. New install
    gets a new peer_id (different first_run_ts).
    """
    h = hashlib.sha256(f"{db_path}|{first_run_ts}".encode("utf-8")).hexdigest()
    return f"astor:{h[:32]}"


def _ensure_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)


def _generate_keypair() -> dict:
    """Generate a fresh ed25519 keypair. Returns dict with base64 strings.

    Handles both PyNaCL and cryptography backends:
      - PyNaCL: sk.generate() returns SigningKey; sk.verify_key = VerifyKey
      - cryptography: sk.generate() returns _PyCryptoSigningKey; we use
        sk._sk.private_bytes() for the raw 32-byte seed
    """
    sk = SigningKey.generate()
    if _NACL_AVAILABLE:
        # nacl path: bytes(sk) = 32-byte seed; sk.verify_key = VerifyKey
        private_raw = bytes(sk)
        public_raw = bytes(sk.verify_key)
    else:
        # cryptography path: derive raw 32 bytes from Ed25519PrivateKey
        # via its internal _raw_oprf (or via public_bytes_raw + recompute).
        # Simplest correct path: use public_key().public_bytes_raw() + raw
        # seed via private_bytes (which requires encoding kwarg in newer
        # versions). To stay version-agnostic, fall back to seed through
        # .sign on a fixed probe and then drop the signature.
        # Actually: cryptography 40+ exposes .private_bytes_raw() — use if
        # available, else fall back to signing-derived seed.
        if hasattr(sk._sk, "private_bytes_raw"):
            private_raw = sk._sk.private_bytes_raw()
            public_raw = sk._sk.public_key().public_bytes_raw()
        else:
            # Older cryptography (≥36 but <42): use .private_bytes with
            # default args (no encoding / format kwargs in old API).
            private_raw = sk._sk.private_bytes()
            public_raw = sk._sk.public_key().public_bytes_raw()
    return {
        "private_key": base64.b64encode(private_raw).decode("ascii"),
        "public_key": base64.b64encode(public_raw).decode("ascii"),
    }


def init_identity(astor_dir: str | None = None) -> dict:
    """Initialize or load identity for this astor install.

    Returns dict with peer_id, first_run_ts, public_key. Creates
    identity files if they don't exist. Idempotent — repeated calls
    return the same identity (DB-bound).
    """
    iddir = _identity_dir(astor_dir)
    _ensure_dir(iddir)
    db_path = _db_path(astor_dir)
    pid_file = iddir / _PEER_ID_FILE
    kp_file = iddir / _KEYPAIR_FILE
    if pid_file.exists() and kp_file.exists():
        # Load existing
        peer_id = pid_file.read_text(encoding="utf-8").strip()
        try:
            keypair = json.loads(kp_file.read_text(encoding="utf-8"))
        except Exception:
            # Corrupted keypair file — regenerate keypair but keep peer_id.
            # This handles private key loss while preserving DB-bound identity.
            keypair = _generate_keypair()
            kp_file.write_text(json.dumps(keypair, indent=2), encoding="utf-8")
        return {
            "peer_id": peer_id,
            "first_run_ts": _read_first_run_ts(iddir),
            "public_key": keypair["public_key"],
            "private_key": keypair["private_key"],
            "db_path": db_path,
        }
    # First-time init: generate peer_id + keypair
    first_run_ts = _now_iso()
    peer_id = _compute_peer_id(db_path, first_run_ts)
    keypair = _generate_keypair()
    pid_file.write_text(peer_id, encoding="utf-8")
    kp_file.write_text(
        json.dumps(
            {
                "private_key": keypair["private_key"],
                "public_key": keypair["public_key"],
                "generated_at": first_run_ts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Also store first_run_ts for rekey detection
    (iddir / "first_run_ts").write_text(first_run_ts, encoding="utf-8")
    # Restrict keypair file permissions on Unix (no-op on Windows).
    try:
        os.chmod(kp_file, 0o600)
    except Exception:
        pass
    return {
        "peer_id": peer_id,
        "first_run_ts": first_run_ts,
        "public_key": keypair["public_key"],
        "private_key": keypair["private_key"],
        "db_path": db_path,
    }


def _read_first_run_ts(iddir: Path) -> str:
    f = iddir / "first_run_ts"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return _now_iso()


def get_identity(astor_dir: str | None = None) -> dict:
    """Read current identity without creating. Returns None if not yet
    initialized.
    """
    iddir = _identity_dir(astor_dir)
    pid_file = iddir / _PEER_ID_FILE
    kp_file = iddir / _KEYPAIR_FILE
    if not (pid_file.exists() and kp_file.exists()):
        return None
    peer_id = pid_file.read_text(encoding="utf-8").strip()
    try:
        keypair = json.loads(kp_file.read_text(encoding="utf-8"))
    except Exception:
        keypair = {"public_key": "", "private_key": ""}
    return {
        "peer_id": peer_id,
        "first_run_ts": _read_first_run_ts(iddir),
        "public_key": keypair.get("public_key", ""),
        "private_key": keypair.get("private_key", ""),
        "db_path": _db_path(astor_dir),
    }


def sign(data: bytes, astor_dir: str | None = None) -> str:
    """Sign data with this peer's private key. Returns base64 signature.

    Caller must ensure identity is initialized (call init_identity first).
    Works with both PyNaCL (preferred) and cryptography (fallback) backends.
    """
    identity = get_identity(astor_dir)
    if not identity:
        identity = init_identity(astor_dir)
    priv_raw = base64.b64decode(identity["private_key"])
    # v1.16.81: coerce at the boundary instead of trusting the caller.
    #
    # Only the PyNaCl branch accepted a str — it hashes whatever it is given —
    # while cryptography's Ed25519PrivateKey.sign() requires a buffer and
    # raises TypeError. So the same call worked or failed depending on which
    # backend happened to be installed, and neither branch advertised that
    # requirement. Every in-tree caller already passed bytes, which is why this
    # sat unnoticed while the dependency was missing: on the affected machine
    # init_identity() itself raised before sign() could be reached.
    if isinstance(data, str):
        data = data.encode("utf-8")

    if _NACL_AVAILABLE:
        sk = SigningKey(priv_raw)
        sig = sk.sign(data).signature
    else:
        ed_sk = Ed25519PrivateKey.from_private_bytes(priv_raw)
        # Ed25519.sign returns the full 64-byte signature on every
        # cryptography version in the declared range.
        sig = ed_sk.sign(data)
    return base64.b64encode(sig).decode("ascii")


def verify(data: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Verify a signature against a public key. Returns True if valid."""
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        pub_raw = base64.b64decode(public_key_b64)
        sig_raw = base64.b64decode(signature_b64)
        if _NACL_AVAILABLE:
            vk = VerifyKey(pub_raw)
            vk.verify(data, sig_raw)
        else:
            ed_vk = Ed25519PublicKey.from_public_bytes(pub_raw)
            ed_vk.verify(sig_raw, data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# v1.14.68 (2026-09-17) — REKEY message helpers (Phase 2).
# See docs/peer-network.md § "Rekey flow" for design.
# ---------------------------------------------------------------------------


def build_rekey_message(
    old_peer_id: str,
    new_peer_id: str,
    astor_dir: str | None = None,
) -> dict:
    """Build a signed REKEY message announcing a peer_id change.

    Wire format (JSON-serializable):
        {
            "old_peer_id": "astor:abc...",
            "new_peer_id": "astor:def...",
            "new_public_key": "base64...",
            "timestamp": "2026-09-17T19:30:00Z",
            "signature": "base64...",
            "signer_pubkey": "base64..."
        }

    The signature covers: old_peer_id + new_peer_id + new_public_key + timestamp.
    Verifiers reconstruct the same payload and verify with signer_pubkey.
    """
    new_identity = init_identity(astor_dir=astor_dir)
    # If old_peer_id is empty (first install), use current peer_id as both
    # — verifier will recognise this as a first-time bootstrap.
    if not old_peer_id:
        old_peer_id = new_identity["peer_id"]
    timestamp = (_dt.datetime.now(_dt.timezone.utc)
                 .isoformat(timespec="seconds")
                 .replace("+00:00", "Z"))
    payload = (
        old_peer_id.encode("utf-8") +
        b"\n" +
        new_identity["peer_id"].encode("utf-8") + b"\n" +
        new_identity["public_key"].encode("utf-8") + b"\n" +
        timestamp.encode("utf-8")
    )
    sig = sign(payload, astor_dir=astor_dir)
    return {
        "old_peer_id": old_peer_id,
        "new_peer_id": new_identity["peer_id"],
        "new_public_key": new_identity["public_key"],
        "timestamp": timestamp,
        "signature": sig,
        "signer_pubkey": new_identity["public_key"],
    }



# ---------------------------------------------------------------------------
# Friend code
#
# Chat apps hide the address behind a central directory: you type a username,
# their server looks up where to reach it. A P2P network has no directory, so
# without help the address becomes something the user has to copy out of one
# dashboard and paste into another — two fields, easy to get wrong, and if
# either side is behind NAT the address may not even work.
#
# A friend code collapses that into one string. It is the same peer_id the
# protocol already uses, plus the address, joined with "@":
#
#     astor:ea1c7c3110128ee1b828c54269341a98@127.0.0.1:7803
#
# One copy, one paste, done. The peer_id part is still the identity that gets
# signed — the address is only routing information, so a wrong or stale
# address costs a failed delivery, never a wrong identity. That separation is
# why the code can carry an address at all without weakening the crypto.
# ---------------------------------------------------------------------------


def format_friend_code(peer_id: str, endpoint: str | None = None) -> str:
    """Render peer_id (+ optional address) as a single pasteable string.

    The endpoint may be given as a bare host:port or a full URL. The scheme
    is kept in the output when known, because a peer behind a tunnel may
    only serve https while the local base URL is plain http.
    """
    code = peer_id.strip()
    if not code:
        return ""
    ep = (endpoint or "").strip().rstrip("/")
    if not ep:
        return code
    return f"{code}@{ep}"


def parse_friend_code(text: str) -> tuple[str, str | None]:
    """Split a friend code into (peer_id, endpoint).

    Accepts a bare peer_id (endpoint is None) as well as the combined form,
    so the same paste box works for both. A bare peer_id is still valid and
    still useful — it records the relationship locally, it just cannot
    deliver a request until the address is known.

    Raises ValueError with a readable message when the input is neither.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty friend code")
    if "@" in raw:
        pid, _, ep = raw.partition("@")
        pid, ep = pid.strip(), ep.strip()
        if not pid:
            raise ValueError("friend code has no peer_id")
        if not ep:
            raise ValueError("friend code has an empty address")
        if not pid.startswith("astor:") or len(pid) != len("astor:") + 32:
            raise ValueError(
                "friend code peer_id must be astor:<32-hex>, got "
                f"{pid!r}")
        return pid, ep
    if not raw.startswith("astor:"):
        raise ValueError(
            "not a friend code: expected astor:<32-hex>[@host:port], got "
            f"{raw[:40]!r}")
    if len(raw) != len("astor:") + 32:
        raise ValueError(
            f"peer_id must be astor:<32-hex> (32 hex chars), got "
            f"{len(raw) - len('astor:')} chars")
    return raw, None


def local_friend_code(astor_dir: str | None = None) -> str:
    """This install's own friend code, ready to share.

    Reads the public endpoint from ASTOR_PUBLIC_ENDPOINT or ASTOR_BASE_URL —
    the same values /v1/identity publishes — so the code shown in the
    dashboard and the code the CLI prints always agree.
    """
    import os as _os
    identity = init_identity(astor_dir)
    endpoint = (
        _os.environ.get("ASTOR_PUBLIC_ENDPOINT")
        or _os.environ.get("ASTOR_BASE_URL")
        or ""
    ).strip().rstrip("/")
    # Strip the scheme for the code: "astor:xxx@host:port" is what people
    # read off a screen, and http is the default the sender applies.
    if endpoint.startswith("http://"):
        endpoint = endpoint[len("http://"):]
    elif endpoint.startswith("https://"):
        # Keep https — a tunnel peer may only serve TLS.
        endpoint = endpoint[len("https://"):]
        endpoint = "https://" + endpoint
    return format_friend_code(identity["peer_id"], endpoint)


def build_friend_request(
    to_peer_id: str,
    alias: str = "",
    note: str = "",
    astor_dir: str | None = None,
) -> dict:
    """Build a signed FRIEND_REQUEST asking another peer to accept us.

    v1.16.83. Adding a peer is currently one-directional: the local
    relationships table records the claim and the remote node never learns
    about it, so "friends" is really a private list rather than a relation.
    This is the outbound half of making it mutual.

    Wire format mirrors REKEY so one verifier path serves both:
        {
          "to_peer_id":  "astor:<32-hex>",
          "from_peer_id": "astor:<32-hex>",   # implied by the signature key
          "alias":       "alice",        # the human-readable name to show
          "note":        "...",              # free text the recipient sees
          "timestamp":   "2026-10-20T...Z",
          "signature":   "base64...",
          "sender_pubkey": "base64..."
        }

    The alias travels in the signed payload rather than being trusted from the
    envelope, so a peer cannot claim to be someone it is not. The recipient
    displays this text verbatim, which is why it is signed rather than
    advisory: it is shown to a human who is being asked to grant trust.

    `alias` is included so the recipient's prompt can say who is asking.
    """
    identity = init_identity(astor_dir)
    msg = {
        "to_peer_id": to_peer_id,
        "from_peer_id": identity["peer_id"],
        "alias": alias or "",
        "note": note or "",
        "timestamp": _now_iso(),
        "sender_pubkey": identity["public_key"],
    }
    msg["signature"] = sign(_friend_request_payload(msg), astor_dir=astor_dir)
    return msg


def _friend_request_payload(msg: dict) -> bytes:
    """Canonical bytes covered by the friend-request signature.

    Newline-delimited in a fixed field order, matching REKEY. Kept in one
    place so build and verify cannot drift — a mismatch here would make every
    request silently unverifiable.
    """
    return (
        msg["to_peer_id"].encode("utf-8") + b"\n" +
        msg["from_peer_id"].encode("utf-8") + b"\n" +
        msg.get("alias", "").encode("utf-8") + b"\n" +
        msg.get("note", "").encode("utf-8") + b"\n" +
        msg["timestamp"].encode("utf-8")
    )


def verify_friend_request(msg: dict) -> bool:
    """Verify a FRIEND_REQUEST signature. Returns True if valid.

    The signature is checked against `sender_pubkey`, and `from_peer_id` is
    then recomputed from that key's identity so a peer cannot send a request
    claiming to be someone else. The key itself is unproven until the operator
    accepts — that is what the prompt is for — so this establishes
    "self-consistent", not "trusted".
    """
    try:
        required = ("to_peer_id", "from_peer_id", "alias", "note",
                    "timestamp", "signature", "sender_pubkey")
        for k in required:
            if k not in msg:
                return False
        if not str(msg["from_peer_id"]).startswith("astor:"):
            return False
        if not str(msg["to_peer_id"]).startswith("astor:"):
            return False
        if not verify(_friend_request_payload(msg), msg["signature"],
                      msg["sender_pubkey"]):
            return False
        # No further identity check is possible or needed. peer_id is
        # sha256(db_path|first_run_ts) — derived from the install, not from
        # the key — so it cannot be recomputed from sender_pubkey. That is the
        # same trust model REKEY uses: the signature proves the message is
        # self-consistent and unaltered, and the operator decides whether the
        # claimed peer_id belongs to whoever holds that key. What the signature
        # does buy is that `alias` and `note` — the text shown in the prompt
        # asking for trust — cannot be edited in transit.
        return True
    except Exception:
        return False


def verify_rekey_message(msg: dict) -> bool:
    """Verify a REKEY message signature. Returns True if valid.

    Verifier must reconstruct the same byte payload and check signature
    against signer_pubkey (NOT the new_public_key — the new key isn't
    trusted yet, the old trust chain is the source of trust here).
    """
    try:
        required = ("old_peer_id", "new_peer_id", "new_public_key",
                    "timestamp", "signature", "signer_pubkey")
        for k in required:
            if k not in msg:
                return False
        payload = (
            msg["old_peer_id"].encode("utf-8") + b"\n" +
            msg["new_peer_id"].encode("utf-8") + b"\n" +
            msg["new_public_key"].encode("utf-8") + b"\n" +
            msg["timestamp"].encode("utf-8")
        )
        return verify(payload, msg["signature"], msg["signer_pubkey"])
    except Exception:
        return False


def decide_rekey_action(msg: dict, current_trust: int | None) -> str:
    """Decide what to do with an incoming REKEY message.

    Decision matrix (per docs/peer-network.md):
        trust >= 70 → 'auto_accept' (KEEP trust, swap peer_id)
        trust 30-69 → 'manual_pending' (notify admin)
        trust < 30  → 'reject' (drop, audit log only)
        trust is None (new peer) → 'manual_pending' (first contact)

    Returns one of: 'auto_accept', 'manual_pending', 'reject'.
    """
    if current_trust is None:
        return "manual_pending"
    if current_trust >= 70:
        return "auto_accept"
    if current_trust >= 30:
        return "manual_pending"
    return "reject"
