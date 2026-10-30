"""The relay: a postman that cannot read the mail.

This is a standalone service. A node registers with it, other nodes
send it sealed envelopes, and it hands them to the owner of the
envelope's recipient key. The relay never holds a private key, never
parses a payload, and never talks to a third party.

Two properties are structural rather than promised:

  - there is no outbound request path in this module. The relay's only
    way to affect the world is to hand bytes to a node that is already
    connected to it. It cannot be turned into an open proxy, because
    it has no "fetch this URL" ability at all. tests/test_relay_server.py
    locks that with a grep assertion, so a later "just for debugging"
    line breaks the build instead of the deployment.
  - the routing table, the mailboxes and the message queue live in
    memory. The relay keeps nothing that would let it read history:
    what it persists is an audit row saying "A asked B something", with
    no content, for seven days.

What is persisted, and why:

    relay_audit.db  -- (timestamp, from_key_id, to_key_id, msg_type).
    The relay necessarily knows who contacted whom; that is the
    communication graph, and it is accepted rather than eliminated
    (risk R7 in the design). The content is not in this table because
    the relay never has it.
"""
from __future__ import annotations

import base64
import datetime as _dt
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from ._internal import relay_crypto as rc

# --- limits (all of them are per-instance and in memory) -------------
CHALLENGE_TTL_SECONDS = 120
DEFAULT_REGISTRATION_TTL = 3600
MAX_REGISTRATION_TTL = 86400
MESSAGE_TTL_SECONDS = 60
MAILBOX_MAX_MESSAGES = 64
MAX_REGISTERED_NODES = 10000
REGISTER_PER_IP_PER_HOUR = 30
SEND_PER_KEY_PER_MINUTE = 60
AUDIT_RETENTION_DAYS = 7
MAX_WAIT_SECONDS = 30


def _now() -> float:
    return time.time()


def _now_iso(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(
        ts if ts is not None else _now(), _dt.timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


class _State:
    """Everything the relay holds, in memory."""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.routes: dict[str, dict[str, Any]] = {}
        self.mailboxes: dict[str, list[dict[str, Any]]] = {}
        self.nonces: dict[str, float] = {}
        self.register_hits: dict[str, list[float]] = {}
        self.send_hits: dict[str, list[float]] = {}

    def sweep(self) -> None:
        now = _now()
        self.nonces = {n: e for n, e in self.nonces.items() if e > now}
        for kid, route in list(self.routes.items()):
            if route["expires_at"] <= now:
                del self.routes[kid]
        for kid, box in list(self.mailboxes.items()):
            kept = [m for m in box if m["expires_at"] > now]
            if kept:
                self.mailboxes[kid] = kept
            else:
                self.mailboxes.pop(kid, None)

    def rate_ok(self, bucket: dict[str, list[float]], key: str,
                limit: int, window: float) -> bool:
        now = _now()
        hits = [t for t in bucket.get(key, []) if t > now - window]
        if len(hits) >= limit:
            bucket[key] = hits
            return False
        hits.append(now)
        bucket[key] = hits
        return True


class _Audit:
    """Who contacted whom, with no content. Seven days."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS relay_audit ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL,"
                " from_key_id TEXT NOT NULL,"
                " to_key_id TEXT NOT NULL,"
                " msg_type TEXT NOT NULL)")

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def record(self, from_key_id: str, to_key_id: str, msg_type: str) -> None:
        cutoff = _now() - AUDIT_RETENTION_DAYS * 86400
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO relay_audit (ts, from_key_id, to_key_id, "
                "msg_type) VALUES (?, ?, ?, ?)",
                (_now(), from_key_id, to_key_id, msg_type))
            con.execute("DELETE FROM relay_audit WHERE ts < ?", (cutoff,))

    def count(self) -> int:
        with self._lock, self._connect() as con:
            return con.execute(
                "SELECT COUNT(*) FROM relay_audit").fetchone()[0]


def create_relay_app(state_dir: str | None = None,
                     audit_path: str | None = None) -> Flask:
    """Build the relay service.

    state_dir/audit_path default next to the rest of the runtime, so a
    relay deployment is a directory and a command.
    """
    base = (state_dir or os.environ.get("ASTOR_RELAY_DIR")
            or os.path.join(os.path.expanduser("~"), ".astor-relay"))
    app = Flask("astor_relay")
    app.config["JSON_SORT_KEYS"] = False
    st = _State()
    audit = _Audit(Path(audit_path or os.path.join(base, "relay_audit.db")))
    app.astor_state = st
    app.astor_audit = audit

    def _client_ip() -> str:
        # the relay is meant to sit behind a reverse proxy, so the
        # forwarded header is the honest answer when it is there
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.remote_addr or "unknown"

    @app.get("/v1/relay/health")
    def health():
        with st.lock:
            st.sweep()
            routes = len(st.routes)
        return jsonify({
            "ok": True,
            "service": "astor-relay",
            "registered_nodes": routes,
            "audit_rows": audit.count(),
        })

    @app.post("/v1/relay/challenge")
    def challenge():
        """Hand out the nonce the node signs to prove it holds the
        private key behind the key_id it claims."""
        with st.lock:
            st.sweep()
            nonce = base64.b64encode(secrets.token_bytes(24)).decode()
            st.nonces[nonce] = _now() + CHALLENGE_TTL_SECONDS
        return jsonify({"nonce": nonce, "expires_at": _now_iso()})

    @app.post("/v1/relay/register")
    def register():
        body = request.get_json(silent=True) or {}
        key_id = str(body.get("key_id") or "")
        public_key = str(body.get("public_key") or "")
        peer_id = str(body.get("peer_id") or "")
        nonce = str(body.get("nonce") or "")
        signature = str(body.get("signature") or "")
        ttl = int(body.get("ttl_seconds") or DEFAULT_REGISTRATION_TTL)

        if not (key_id and public_key and nonce and signature):
            return jsonify({"ok": False,
                            "error": "key_id, public_key, nonce and "
                                     "signature are all required"}), 400

        # the key_id must be the fingerprint of the key that signed --
        # otherwise anybody could claim an address
        if rc.key_id_for(public_key) != key_id:
            return jsonify({"ok": False,
                            "error": "key_id does not match public_key"}), 400

        with st.lock:
            st.sweep()
            if nonce not in st.nonces:
                return jsonify({"ok": False,
                                "error": "unknown or expired nonce"}), 400
            del st.nonces[nonce]
            ip = _client_ip()
            if not st.rate_ok(st.register_hits, f"ip:{ip}",
                              REGISTER_PER_IP_PER_HOUR, 3600):
                return jsonify({"ok": False,
                                "error": "too many registrations from this "
                                         "address"}), 429
            if key_id not in st.routes and len(st.routes) >= MAX_REGISTERED_NODES:
                return jsonify({"ok": False,
                                "error": "relay is full"}), 503

        # verify the signature over (key_id|public_key|nonce). A node
        # that cannot sign does not enter the routing table.
        payload = rc.registration_payload(key_id, public_key, nonce)
        try:
            from ._internal.peer_identity import verify as _verify
            ok = _verify(payload, signature, public_key)
        except Exception:
            ok = False
        if not ok:
            return jsonify({"ok": False,
                            "error": "signature does not verify against "
                                     "public_key"}), 401

        expires = _now() + max(60, min(ttl, MAX_REGISTRATION_TTL))
        with st.lock:
            st.routes[key_id] = {
                "key_id": key_id,
                "peer_id": peer_id,
                "public_key": public_key,
                "expires_at": expires,
            }
        return jsonify({"ok": True, "expires_at": _now_iso(expires)})

    @app.get("/v1/relay/lookup/<key_id>")
    def lookup(key_id: str):
        with st.lock:
            st.sweep()
            route = st.routes.get(key_id)
            if not route:
                return jsonify({"found": False}), 404
            # "online" is about the relay, not about whether the peer is
            # reachable directly -- the UI has to show the two apart
            online = route["expires_at"] > _now()
            return jsonify({
                "found": True,
                "online": online,
                "peer_id": route.get("peer_id") or "",
                "expires_at": _now_iso(route["expires_at"]),
            })

    @app.post("/v1/relay/send")
    def send():
        body = request.get_json(silent=True) or {}
        payload = body.get("payload")
        to_key_id = str(body.get("to_key_id") or "")
        from_key_id = str(body.get("from_key_id") or "")
        msg_type = str(body.get("msg_type") or "")
        reply_to = body.get("reply_to")

        if not (payload and to_key_id and from_key_id):
            return jsonify({"ok": False,
                            "error": "payload, to_key_id and from_key_id "
                                     "are all required"}), 400
        # the relay refuses to carry anything it cannot treat as opaque
        # bytes: a plaintext payload here means the sender tried to
        # downgrade, and carrying it would help that succeed
        if not isinstance(payload, dict) or not payload.get("ct"):
            return jsonify({"ok": False,
                            "error": "payload must be a sealed envelope"}), 400

        with st.lock:
            st.sweep()
            if not st.rate_ok(st.send_hits, from_key_id,
                              SEND_PER_KEY_PER_MINUTE, 60):
                return jsonify({"ok": False,
                                "error": "rate limit"}), 429
            box = st.mailboxes.setdefault(to_key_id, [])
            if len(box) >= MAILBOX_MAX_MESSAGES:
                box.pop(0)
            msg_id = secrets.token_hex(8)
            box.append({
                "msg_id": msg_id,
                "msg_type": msg_type,
                "payload": payload,
                "from_key_id": from_key_id,
                "received_at": _now_iso(),
                "expires_at": _now() + MESSAGE_TTL_SECONDS,
                "reply_to": reply_to,
            })
            st.lock.notify_all()

        audit.record(from_key_id, to_key_id, msg_type or "unknown")
        return jsonify({"ok": True, "queued": True, "msg_id": msg_id})

    @app.get("/v1/relay/poll/<key_id>")
    def poll(key_id: str):
        try:
            wait = float(request.args.get("wait", "0") or 0)
        except ValueError:
            wait = 0.0
        wait = max(0.0, min(wait, MAX_WAIT_SECONDS))

        deadline = _now() + wait
        with st.lock:
            st.sweep()
            while True:
                box = st.mailboxes.get(key_id) or []
                if box or _now() >= deadline:
                    break
                st.lock.wait(timeout=max(0.0, deadline - _now()))
            box = st.mailboxes.get(key_id) or []
            messages = [{
                "msg_id": m["msg_id"],
                "msg_type": m["msg_type"],
                "payload": m["payload"],
                "from_key_id": m["from_key_id"],
                "received_at": m["received_at"],
                "reply_to": m["reply_to"],
            } for m in list(box)]
        return jsonify({"messages": messages})

    @app.post("/v1/relay/ack")
    def ack():
        body = request.get_json(silent=True) or {}
        key_id = str(body.get("key_id") or "")
        msg_ids = body.get("msg_ids") or []
        if not (key_id and isinstance(msg_ids, list)):
            return jsonify({"ok": False,
                            "error": "key_id and msg_ids are required"}), 400
        with st.lock:
            box = st.mailboxes.get(key_id) or []
            keep = [m for m in box if m["msg_id"] not in set(msg_ids)]
            removed = len(box) - len(keep)
            if keep:
                st.mailboxes[key_id] = keep
            else:
                st.mailboxes.pop(key_id, None)
        return jsonify({"ok": True, "acked": removed})

    @app.get("/")
    def index():
        return jsonify({
            "service": "astor-relay",
            "ok": True,
            "note": "postman, not reader -- the relay never holds a "
                    "private key and never parses a payload",
        })

    return app


def main() -> int:
    """Run the relay with waitress (the same server astor itself uses)."""
    import argparse
    ap = argparse.ArgumentParser(description="astor relay service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7900)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--threads", type=int, default=16)
    args = ap.parse_args()

    app = create_relay_app(state_dir=args.state_dir)
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=args.threads)
    except ImportError:
        app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
