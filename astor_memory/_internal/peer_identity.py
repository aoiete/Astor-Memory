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
    base = astor_dir or os.environ.get("ASTOR_DIR", "D:/AI/Astor-Memory-Runtime")
    return Path(base) / _IDENTITY_DIR


def _db_path(astor_dir: str | None = None) -> str:
    """Canonical DB path — what we're hashing for peer_id."""
    base = astor_dir or os.environ.get("ASTOR_DIR", "D:/AI/Astor-Memory-Runtime")
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
    if _NACL_AVAILABLE:
        sk = SigningKey(priv_raw)
        sig = sk.sign(data).signature
    else:
        ed_sk = Ed25519PrivateKey.from_private_bytes(priv_raw)
        # cryptography 40+ returns full 64-byte signature directly (sig
        # starts with raw sig); older versions return same. Ed25519.sign
        # returns 64 bytes always.
        sig = ed_sk.sign(data)
    return base64.b64encode(sig).decode("ascii")


def verify(data: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Verify a signature against a public key. Returns True if valid."""
    try:
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
