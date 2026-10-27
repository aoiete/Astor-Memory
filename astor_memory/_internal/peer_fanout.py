"""Auto peer-search on a local miss (2026-10-27).

`/v1/read` used to reach the peer network only when the caller passed
`peer_fanout: true`. That is a good default for a library -- nobody
should be surprised by a network round trip -- but it means the whole
federation is invisible to an agent that does not know the flag exists.
Hermes's adapter did not send it, so the peers sat there configured and
unqueried.

This module is the switch that makes the behaviour a node-level
setting instead of a per-call argument.

What it costs, precisely (the numbers are the code's, not an estimate):

  - Local recall FOUND something -> nothing changes. No peer is
    contacted, no latency is added. The switch only matters on a miss.
  - Local recall found NOTHING -> the node signs a request and asks
    every eligible peer (trust >= 50, has an endpoint, not blocked)
    for their public tier. Each peer gets `PEER_TIMEOUT_SECONDS` (3s)
    and the whole fan-out gets `TOTAL_TIMEOUT_SECONDS` (10s).

So "on" does not mean "every read is slow". It means "a read that found
nothing may take up to 10 extra seconds instead of returning empty".

Off by default? No -- the operator asked for it on, because a node with
peers configured should use them. The env var `ASTOR_PEER_FANOUT_DEFAULT`
can still force it off for a node that wants strictly local recall, and
a request can still opt out per call with `peer_fanout: false`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# The operator's decision, persisted next to the other peer settings so a
# restart does not silently change it -- the same reason open-search moved
# from an in-memory dict to disk.
_STATE_FILE = "peer_fanout.json"

# Default ON. A node with no eligible peers pays nothing: the fan-out
# short-circuits on an empty target list.
_DEFAULT_ON = True


def _state_path(astor_dir=None) -> Path:
    base = os.environ.get("ASTOR_DIR") or astor_dir or ""
    root = Path(base) if base else Path.home() / ".astor"
    return root / "identity" / _STATE_FILE


def _load(astor_dir=None) -> bool | None:
    """Read the stored decision. None means "not set, use the default"."""
    path = _state_path(astor_dir)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "enabled" in data:
        return bool(data["enabled"])
    return None


def _save(enabled: bool, astor_dir=None) -> None:
    path = _state_path(astor_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": bool(enabled)}, indent=2),
                        encoding="utf-8")
    except Exception:
        pass


def is_enabled(astor_dir=None) -> bool:
    """Should /v1/read fall back to the peer network on a local miss?

    Precedence: ASTOR_PEER_FANOUT_DEFAULT env (operator override, wins)
    > the stored dashboard decision > ON.
    """
    env = os.environ.get("ASTOR_PEER_FANOUT_DEFAULT")
    if env is not None and env.strip() != "":
        return env.strip() not in ("0", "false", "False", "no", "off")
    stored = _load(astor_dir)
    if stored is not None:
        return stored
    return _DEFAULT_ON


def set_enabled(enabled: bool, astor_dir=None) -> bool:
    """Persist the operator's decision. Returns what was stored."""
    enabled = bool(enabled)
    _save(enabled, astor_dir)
    return enabled


def status(astor_dir=None) -> dict:
    """What the dashboard shows: the decision and where it came from."""
    env = os.environ.get("ASTOR_PEER_FANOUT_DEFAULT")
    stored = _load(astor_dir)
    if env is not None and env.strip() != "":
        source = "env"
    elif stored is not None:
        source = "dashboard"
    else:
        source = "default"
    return {
        'enabled': is_enabled(astor_dir),
        'source': source,
        'stored': stored,
        'env': env,
        'note': ('on a local miss only: local hits stay fast; a miss may '
                 'cost up to 10s asking peers (3s per peer)'),
    }
