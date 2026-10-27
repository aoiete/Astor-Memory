"""astor_memory/_internal/dashboard_auth.py — passcode gate for the dashboard.

The problem this closes. astor can be reached from the public internet
(a Cloudflare tunnel points at it), and the dashboard had no
authentication at all: anyone who opened the URL could read the
aggregate payload -- which includes private-tier fact text, the list of
bound users, and the operator's own working notes -- and could call the
write endpoints directly.

The shape of the fix follows the shape of the problem. There is one
operator and one dashboard, so this is not a user system. It is a
passcode:

  - The passcode lives in ASTOR_DIR, never in the repo and never in
    the response to a request.
  - Entering it returns a bearer token that the browser keeps in
    sessionStorage -- so a reload does not ask again, but closing the
    tab does.
  - The token is what write and sensitive-read requests carry.

What is deliberately NOT gated:

  - Requests from loopback. The CLI, the Hermes adapter, the cron
    jobs and the local dashboard JS all live on this machine and
    already have filesystem access to the runtime directory. Gating
    them would add friction to every script without adding security:
    anything that can read ASTOR_DIR can read the passcode file too.
  - The public tier's read endpoints. That is the whole point of the
    public tier, and the peer federation depends on it. Gating it
    would break peers and serve no purpose.
  - /v1/health and the static assets. The dashboard page has to load
    before a passcode can be typed, and a health probe is not data.

What IS gated:

  - Every mutating request (POST / PUT / DELETE) that is not a
    peer-protocol endpoint. Someone who can delete your memories should
    not be able to do it by guessing a URL.
  - The dashboard aggregate, which reaches private tiers.
  - The peer administration endpoints.

The passcode is compared in constant time. A wrong passcode tells the
caller nothing about whether it was close.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

# Session lifetime. Long enough that a working day is not interrupted,
# short enough that an abandoned browser tab stops being a key.
SESSION_TTL_SECONDS = 12 * 3600

# How long a failed attempt is remembered, per client. This is not
# about making a password unguessable -- a 6-digit passcode is
# guessable -- it is about making online guessing slow enough to notice.
FAIL_COOLDOWN_SECONDS = 5.0
_MAX_FAILS = 8
_FAIL_WINDOW_SECONDS = 600.0

# Header names. The token is bearer-style: the value alone is the
# credential, so it must not be logged.
HDR_TOKEN = "X-Astor-Admin-Token"

# The peer endpoints that may be read without a passcode. These are the
# protocol surface a remote node needs in order to participate: it
# announces itself, it reads the public tier, it checks liveness, and
# it asks whether its own request is open. Nothing here exposes the
# relationship graph, the audit log, or anything but the public tier.
_PUBLIC_PEER_READS = frozenset({
    "/v1/peer/identity",       # who this node is
    "/v1/peer/public_search",   # the public-tier query itself
    "/v1/peer/open-search",     # is this node open to unsolicited queries
    "/v1/peer/health",          # liveness
})
# NOT in the list above: /v1/peer/list (the relationship graph),
# /v1/peer/audit (the query log) and /v1/peer/quarantine/list (pending
# peers). Those are operator data -- they name who this node trusts and
# who is waiting to be let in. A remote node needs to know its own
# identity and to run a public-tier query; it does not need the
# operator's trust graph.
HDR_PASSCODE = "X-Astor-Passcode"


def _runtime_dir() -> Path:
    """Where this node's runtime lives.

    This has to be the same directory the server uses, or the passcode
    ends up somewhere the operator does not think of and the two
    halves of the gate disagree about where the node lives. The server
    resolves it through astor_memory.config.get_default_astor_dir(),
    which reads ASTOR_DIR, then install.json, then ~/.astor. This used
    to look at ASTOR_DIR alone, so on a node installed into a project
    directory the data was in <project>/.astor while the passcode was
    in ~/.astor -- two different nodes, in effect.
    """
    try:
        from ..config import get_default_astor_dir
        return get_default_astor_dir()
    except Exception:
        base = os.environ.get("ASTOR_DIR") or ""
        return Path(base) if base else Path.home() / ".astor"


def _secret_path() -> Path:
    return _runtime_dir() / "identity" / "dashboard_passcode"


def _token_path() -> Path:
    return _runtime_dir() / "identity" / "dashboard_sessions.json"


def passcode_path() -> Path:
    """Where the passcode lives. Operators need this to read it."""
    return _secret_path()


def _legacy_secret_path() -> Path:
    """Where the passcode used to live before the runtime directory was
    resolved the same way the server resolves it.

    A node installed into a project directory kept its data in
    <project>/.astor while the passcode sat in ~/.astor, because this
    module used to read ASTOR_DIR alone and the server read install.json
    as well. Now that both agree, an existing passcode has to be moved
    across or the operator silently gets a different one after an
    update.
    """
    try:
        from ..config import get_default_astor_dir
        current = get_default_astor_dir()
    except Exception:
        return Path.home() / ".astor" / "identity" / "dashboard_passcode"
    legacy = Path.home() / ".astor" / "identity" / "dashboard_passcode"
    if legacy == current / "identity" / "dashboard_passcode":
        return legacy
    return legacy


def get_passcode() -> str:
    """The operator's passcode, creating one if there isn't one yet.

    Creating it lazily means a fresh install works without setup, and
    the value is printed once by the CLI that generates it so the
    operator can write it down.
    """
    path = _secret_path()
    try:
        if path.exists():
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
    except Exception:
        pass
    # Migrate a passcode that was written to the old location, so an
    # update does not quietly change the operator's code.
    try:
        legacy = _legacy_secret_path()
        if legacy.exists() and legacy != path:
            val = legacy.read_text(encoding="utf-8").strip()
            if val:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(val, encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass
                return val
    except Exception:
        pass
    val = secrets.token_urlsafe(12)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(val, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass  # Windows ACLs are not POSIX; best effort only.
    except Exception:
        pass
    return val


def set_passcode(value: str) -> str:
    """Replace the passcode. Returns what was stored."""
    value = (value or "").strip()
    if len(value) < 6:
        raise ValueError("passcode must be at least 6 characters")
    path = _secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    # Changing the passcode invalidates every existing session.
    _save_sessions({})
    return value


def check_passcode(candidate: str) -> bool:
    """Constant-time comparison against the stored passcode."""
    if not candidate:
        return False
    try:
        stored = get_passcode()
    except Exception:
        return False
    return hmac.compare_digest(stored.encode("utf-8"),
                               candidate.strip().encode("utf-8"))


# ---------------------------------------------------------------- sessions

def _load_sessions() -> dict:
    try:
        p = _token_path()
        if not p.exists():
            return {}
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sessions(data: dict) -> None:
    try:
        p = _token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        import json
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def issue_session() -> str:
    """Mint a session token for a correct passcode."""
    token = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    now = time.time()
    # Drop expired sessions while we are here.
    for k in [k for k, v in sessions.items()
              if not isinstance(v, dict) or now - float(v.get("ts") or 0) > SESSION_TTL_SECONDS]:
        sessions.pop(k, None)
    sessions[token] = {"ts": now}
    _save_sessions(sessions)
    return token


def revoke_session(token: str) -> None:
    sessions = _load_sessions()
    if sessions.pop(token, None) is not None:
        _save_sessions(sessions)


def valid_session(token: str) -> bool:
    """Is this a live session token?"""
    if not token:
        return False
    sessions = _load_sessions()
    entry = sessions.get(token)
    if not isinstance(entry, dict):
        return False
    try:
        age = time.time() - float(entry.get("ts") or 0)
    except Exception:
        return False
    if age > SESSION_TTL_SECONDS:
        sessions.pop(token, None)
        _save_sessions(sessions)
        return False
    return True


# ------------------------------------------------------------- rate limit

_fail_state: dict[str, list[float]] = {}


def check_attempt(client: str) -> tuple[bool, dict]:
    """Should this client be allowed to try the passcode right now?

    Returns (allowed, info). The info is for the caller's own logs; it
    deliberately does not say how close the guess was.
    """
    now = time.time()
    fails = [t for t in _fail_state.get(client, []) if now - t < _FAIL_WINDOW_SECONDS]
    if fails:
        _fail_state[client] = fails
    if len(fails) < _MAX_FAILS:
        return True, {"attempts_used": len(fails), "max_attempts": _MAX_FAILS}
    # The window is exhausted: wait until the oldest failure ages out.
    wait = max(0.0, _FAIL_WINDOW_SECONDS - (now - fails[0]))
    return False, {"retry_after_seconds": int(wait) + 1,
                   "max_attempts": _MAX_FAILS}


def record_failure(client: str) -> None:
    now = time.time()
    fails = [t for t in _fail_state.get(client, []) if now - t < _FAIL_WINDOW_SECONDS]
    fails.append(now)
    _fail_state[client] = fails


def record_success(client: str) -> None:
    _fail_state.pop(client, None)


# --------------------------------------------------------------- request

def is_loopback(remote_addr: str) -> bool:
    """Is this request from this machine?"""
    if not remote_addr:
        return False
    addr = remote_addr.strip()
    if addr.startswith("::ffff:"):
        addr = addr[7:]
    return addr in ("127.0.0.1", "::1", "localhost") or addr.startswith("127.")


def is_local_request(remote_addr: str, host: str = "") -> bool:
    """Is this request genuinely from this machine?

    The naive check is the remote address, and it is wrong here. A
    Cloudflare tunnel runs on this machine and forwards to
    127.0.0.1:7803, so a request from the public internet arrives with
    remote_addr = 127.0.0.1 -- indistinguishable from a local script.
    Judging by address alone would have left the public dashboard
    exactly as open as it was before this module existed.

    The host header is what separates them: a local script talks to
    127.0.0.1 and sends no Host or a loopback Host; a request through
    the tunnel carries the public hostname. So the rule is: loopback
    address AND a loopback-or-absent Host.
    """
    if not is_loopback(remote_addr):
        return False
    h = (host or "").strip().lower()
    if not h:
        return True
    # Strip the port.
    if h.startswith("["):          # [::1]:7803
        h = h.split("]", 1)[0].lstrip("[")
    else:
        h = h.split(":", 1)[0]
    if h in ("localhost", "::1"):
        return True
    if h.startswith("127."):
        return True
    if h in ("0.0.0.0", ""):
        return True
    return False



def request_is_authorized(headers, remote_addr: str, method: str,
                          path: str, host: str = "") -> tuple[bool, str]:
    """Decide whether a request may proceed.

    Returns (allowed, reason). The reason is for logs and for the
    client to know whether to prompt for a passcode (401) or to go away
    (403).
    """
    # Local requests are trusted. Everything running on this machine
    # already has the runtime directory. The host header is part of the
    # test because a Cloudflare tunnel on this machine forwards to
    # 127.0.0.1 and would otherwise look local.
    if is_local_request(remote_addr, host):
        return True, "local"

    token = (headers.get(HDR_TOKEN) or "").strip()
    if token and valid_session(token):
        return True, "session"

    # The passcode itself is accepted directly, so a client can do a
    # one-shot authenticated call without holding a session.
    pc = (headers.get(HDR_PASSCODE) or "").strip()
    if pc and check_passcode(pc):
        return True, "passcode"

    # Peer protocol reads are how the federation works, and they expose
    # only the public tier. But the whole /v1/peer/ namespace cannot be
    # open: /v1/peer/list returns the whole relationship graph (who
    # this node trusts, with trust scores and endpoints) and
    # /v1/peer/audit returns the query log. Those are the operator's
    # business, not the protocol's. So the exemption is an explicit
    # list, not a prefix.
    if method in ("GET", "HEAD") and path in _PUBLIC_PEER_READS:
        return True, "peer_read"
    if method in ("GET", "HEAD", "OPTIONS"):
        return False, "need_passcode"
    return False, "need_passcode"
