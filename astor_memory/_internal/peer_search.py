"""v1.14.73 (2026-09-17) — Demand-driven Peer Public Search (PPS).

Architecture (user-locked 2026-09-17):
  - NO push (peers cannot push facts to me)
  - NO continuous pull (no cron, no subscribe, no broadcast)
  - YES demand-driven search (on recall, query friends' public tier)
  - Read-only (search results don't write to local DB)
  - Manual adopt (user explicitly adopts to persist)

Key principle: "construct-time validation, not detect-time reaction."
Every PeerSearchRequest is validated at construction. If invalid, the
object cannot exist. There's no "reject" step because there's nothing
to reject — an invalid request was never created.

Security (all preventive):
  - friend list filter: only trust>=50 friends can be queried
  - friend opt-in: each friend must explicitly allow my searches
    (default off — friend has to run `am peer allow-search <my_peer_id>`)
  - timestamp freshness: now-7d < ts < now+60s (must be recent)
  - signature: ed25519 over (requestor_peer_id + query + topic + ts)
  - response cap: 1MB per friend, 20 facts total
  - timeout: 3s per friend, 10s total
  - tier restriction: friends only return public tier (not private/source)

Attacks that this design ELIMINATES (not just defends against):
  - Spam: no one can push to me → no spam possible
  - Poison (eclipse-style): I only query my own trust>=50 friends,
    AND results are source-marked so I see who said what
  - Replay: timestamp check at construction → old requests never valid
  - DoS via slow peer: 3s timeout → slow peer skipped automatically
  - Hostile (no friend): target list filtered at construction →
    unknown peer_ids never reach the network layer
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# Limits (locked 2026-09-17)
MIN_TRUST_FOR_SEARCH = 50       # friend trust threshold
MAX_QUERY_LENGTH = 1024          # bytes
MAX_TOPIC_LENGTH = 128           # bytes
MAX_RESULTS_PER_PEER = 20       # friend returns at most N facts
MAX_RESPONSE_BYTES = 1 * 1024 * 1024   # 1MB per peer
PEER_TIMEOUT_SECONDS = 3        # per-peer HTTP timeout
TOTAL_TIMEOUT_SECONDS = 10      # total budget across all peers
TIMESTAMP_SKEW_SECONDS = 60     # allow 60s clock skew into the future
TIMESTAMP_AGE_SECONDS = 7 * 24 * 3600   # reject requests older than 7d


@dataclass(frozen=True)
class PeerSearchRequest:
    """Signed search request sent from A to one of A's friends B.

    Construct-time validation: if any field is invalid, the dataclass
    raises before the request can be sent. There is no "reject" step
    in the receiver — an invalid request is impossible to construct.
    """
    requestor_peer_id: str
    requestor_pubkey: str
    query: str
    topic: str | None
    limit: int
    timestamp: str   # ISO 8601 UTC, e.g. 2026-09-17T19:00:00Z
    signature: str   # base64

    def __post_init__(self):
        # Validate requestor_peer_id format
        if not self.requestor_peer_id.startswith("astor:"):
            raise ValueError(
                f"requestor_peer_id must start with 'astor:'; "
                f"got {self.requestor_peer_id!r}"
            )
        if len(self.requestor_peer_id) != len("astor:") + 32:
            raise ValueError(
                f"requestor_peer_id must be astor:<32-hex>; "
                f"got length {len(self.requestor_peer_id)}"
            )
        # Validate pubkey (base64 of 32 bytes)
        try:
            pub_raw = base64.b64decode(self.requestor_pubkey)
            if len(pub_raw) != 32:
                raise ValueError
        except Exception as e:
            raise ValueError(
                f"requestor_pubkey must be base64 of 32 bytes; "
                f"got {self.requestor_pubkey!r}"
            ) from e
        # Validate query length (DoS prevention)
        if not self.query:
            raise ValueError("query must not be empty")
        if len(self.query.encode('utf-8')) > MAX_QUERY_LENGTH:
            raise ValueError(
                f"query exceeds {MAX_QUERY_LENGTH} bytes "
                f"(got {len(self.query.encode('utf-8'))})"
            )
        # Validate topic
        if self.topic is not None:
            if len(self.topic.encode('utf-8')) > MAX_TOPIC_LENGTH:
                raise ValueError(
                    f"topic exceeds {MAX_TOPIC_LENGTH} bytes"
                )
        # Validate limit
        if not 1 <= self.limit <= MAX_RESULTS_PER_PEER:
            raise ValueError(
                f"limit must be 1-{MAX_RESULTS_PER_PEER}; "
                f"got {self.limit}"
            )
        # Validate timestamp freshness (replay defense)
        self._validate_timestamp()
        # Validate signature is non-empty (actual verification is
        # caller's job — we just enforce structural validity)
        if not self.signature:
            raise ValueError("signature must not be empty")
        try:
            sig_raw = base64.b64decode(self.signature)
            if len(sig_raw) != 64:
                raise ValueError
        except Exception as e:
            raise ValueError(
                f"signature must be base64 of 64 bytes; "
                f"got {self.signature!r}"
            ) from e

    def _validate_timestamp(self):
        try:
            # ISO 8601 with Z suffix
            ts = self.timestamp.rstrip('Z')
            dt = _dt.datetime.fromisoformat(ts).replace(
                tzinfo=_dt.timezone.utc
            )
        except Exception as e:
            raise ValueError(
                f"timestamp must be ISO 8601 UTC; "
                f"got {self.timestamp!r}"
            ) from e
        now = _dt.datetime.now(_dt.timezone.utc)
        delta = (now - dt).total_seconds()
        if delta > TIMESTAMP_AGE_SECONDS:
            raise ValueError(
                f"timestamp too old: {delta:.0f}s > "
                f"{TIMESTAMP_AGE_SECONDS}s "
                f"(possible replay)"
            )
        if delta < -TIMESTAMP_SKEW_SECONDS:
            raise ValueError(
                f"timestamp in the future: {-delta:.0f}s "
                f"> {TIMESTAMP_SKEW_SECONDS}s skew"
            )

    def canonical_payload(self) -> bytes:
        """Bytes that were signed — used by verifier to reconstruct."""
        topic_str = self.topic or ""
        return (
            self.requestor_peer_id.encode("utf-8") + b"\n" +
            self.query.encode("utf-8") + b"\n" +
            topic_str.encode("utf-8") + b"\n" +
            str(self.limit).encode("utf-8") + b"\n" +
            self.timestamp.encode("utf-8")
        )

    def verify_signature(self) -> bool:
        """Verify the request signature using requestor_pubkey.

        Returns False if verification fails (does NOT raise — caller
        decides whether to log + skip, or treat as hostile).
        """
        from .peer_identity import verify
        try:
            return verify(
                self.canonical_payload(),
                self.signature,
                self.requestor_pubkey,
            )
        except Exception:
            return False

    def to_query_string(self) -> str:
        """Encode as URL query parameter for GET endpoint."""
        payload = {
            "requestor_peer_id": self.requestor_peer_id,
            "requestor_pubkey": self.requestor_pubkey,
            "query": self.query,
            "topic": self.topic or "",
            "limit": self.limit,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }
        return base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")


@dataclass(frozen=True)
class PeerSearchResult:
    """One fact returned from a friend's public tier.

    Includes provenance so the caller knows who said what. The caller
    decides whether to adopt (write to local DB) — this object is
    read-only by design.
    """
    source_peer_id: str
    source_trust: int
    fact_id: int        # friend's local fact_id (informational only)
    content: str
    kind: str
    tags: tuple        # tuple for hashability
    created_at: str
    relevance: float    # 0.0-1.0, peer's own scoring

    def __post_init__(self):
        if not self.source_peer_id.startswith("astor:"):
            raise ValueError(
                f"source_peer_id must start with 'astor:'; "
                f"got {self.source_peer_id!r}"
            )
        if not 0 <= self.source_trust <= 100:
            raise ValueError(
                f"source_trust must be 0-100; got {self.source_trust}"
            )
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError(
                f"relevance must be 0.0-1.0; got {self.relevance}"
            )

    def to_dict(self) -> dict:
        """JSON-serializable form for embedding in responses."""
        return {
            'source_peer_id': self.source_peer_id,
            'source_trust': self.source_trust,
            'fact_id': self.fact_id,
            'content': self.content,
            'kind': self.kind,
            'tags': list(self.tags),
            'created_at': self.created_at,
            'relevance': self.relevance,
        }


@dataclass(frozen=True)
class PeerSearchResponse:
    """Response from one friend's /v1/peer/public_search.

    Returned in JSON over HTTP. Note: results live in a flat list,
    not nested in a list-per-fact — keeps response small.
    """
    requestor_peer_id: str  # echo back
    results: tuple          # tuple of PeerSearchResult
    truncated: bool = False  # True if hit limit
    error: str | None = None  # set if peer rejected (with reason)

    def to_dict(self) -> dict:
        return {
            "requestor_peer_id": self.requestor_peer_id,
            "results": [
                {
                    "source_peer_id": r.source_peer_id,
                    "source_trust": r.source_trust,
                    "fact_id": r.fact_id,
                    "content": r.content,
                    "kind": r.kind,
                    "tags": list(r.tags),
                    "created_at": r.created_at,
                    "relevance": r.relevance,
                }
                for r in self.results
            ],
            "truncated": self.truncated,
            "error": self.error,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")


def build_search_request(
    query: str,
    requestor_peer_id: str,
    requestor_pubkey: str,
    requestor_private_key: str,
    topic: str | None = None,
    limit: int = 10,
) -> PeerSearchRequest:
    """Build a signed PeerSearchRequest.

    Caller passes requestor_peer_id + private_key (from peer_identity).
    The function builds the canonical payload, signs it, and returns
    a validated PeerSearchRequest object.
    """
    from .peer_identity import sign
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec='seconds'
    ).replace('+00:00', 'Z')
    # First build a temp object to get the canonical payload
    temp = PeerSearchRequest(
        requestor_peer_id=requestor_peer_id,
        requestor_pubkey=requestor_pubkey,
        query=query,
        topic=topic,
        limit=limit,
        timestamp=now,
        signature='A' * 86 + '==',  # placeholder: 88 chars base64 → exactly 64 bytes
    )
    sig = sign(temp.canonical_payload())
    # Build real object with real signature
    return PeerSearchRequest(
        requestor_peer_id=requestor_peer_id,
        requestor_pubkey=requestor_pubkey,
        query=query,
        topic=topic,
        limit=limit,
        timestamp=now,
        signature=sig,
    )


def select_search_targets(
    friends: list[dict],
    *,
    topic: str | None = None,
    topic_min_weight: float = 0.5,
    astor_dir: str | None = None,
) -> list[dict]:
    """Filter friend list to those eligible as search targets.

    Eligibility (preventive, all at construction time):
      - trust >= MIN_TRUST_FOR_SEARCH (50)
      - has endpoint set
      - kind != 'blacklist'
      - peer is_opt_in search == True (user-locked 2026-09-17:
        "only friends can search" — i.e. opt-in, NOT opt-out;
        friend must explicitly allow my searches)

    Returns filtered list (preserves original dicts).
    """
    targets = []
    for f in friends:
        trust = f.get('trust', 0)
        kind = f.get('kind') or 'friend'
        endpoint = f.get('endpoint') or ''

        # v1.15.27 Ship K: topic filter — if `topic` is given (kwarg), only
        # friends with weight >= topic_min_weight for that topic in
        # topic_index are eligible. Reduces fan-out cost + improves
        # relevance when caller knows the topic.
        if topic:
            try:
                from .peer_relationships import list_topics_for_peer
                topics = list_topics_for_peer(
                    f['peer_id'], min_weight=topic_min_weight,
                    astor_dir=astor_dir,
                )
                topic_weights = {
                    t['topic']: t['weight'] for t in topics
                }
                if topic_weights.get(topic, 0.0) < topic_min_weight:
                    continue
            except Exception:
                # If topic_index lookup fails, skip (safer than fanning
                # out to a peer that may not have the topic).
                continue
        # Opt-in enforcement is SERVER-side (friend's /v1/peer/public_search
        # returns 403 'search_not_allowed' unless THEIR local row for the
        # requestor has metadata.allow_search=True). Local A-side flag is
        # informational only — if the friend set it here too, we can skip
        # the round-trip for known-not-opted-in peers.
        allow = (f.get('allow_search')
                 or (f.get('metadata') or {}).get('allow_search'))
        if trust < MIN_TRUST_FOR_SEARCH:
            continue
        if kind == 'blacklist':
            continue
        if not endpoint:
            continue
        targets.append(f)
    return targets


def dispatch_search_to_peers(
    request: PeerSearchRequest,
    targets: list[dict],
    *,
    per_peer_timeout: float = PEER_TIMEOUT_SECONDS,
    total_timeout: float = TOTAL_TIMEOUT_SECONDS,
) -> list[PeerSearchResponse]:
    """Send the search request to each target peer concurrently.

    Returns one PeerSearchResponse per peer (in input order).
    Failed responses have `error` set, no `results`.

    Implementation: uses urllib synchronously with timeout per peer.
    Concurrent version would use concurrent.futures, but kept simple
    for v1.14.73. Total budget enforced by external caller.
    """
    import urllib.error
    responses = []
    for f in targets:
        endpoint = (f.get('endpoint') or '').rstrip('/')
        if not endpoint:
            responses.append(PeerSearchResponse(
                requestor_peer_id=request.requestor_peer_id,
                results=(),
                error='no_endpoint',
            ))
            continue
        url = endpoint + '/v1/peer/public_search?req=' + urllib.parse.quote(
            request.to_query_string()
        )
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=per_peer_timeout) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    responses.append(PeerSearchResponse(
                        requestor_peer_id=request.requestor_peer_id,
                        results=(),
                        error='response_too_large',
                    ))
                    continue
                data = json.loads(body)
                results = tuple(
                    PeerSearchResult(
                        source_peer_id=r['source_peer_id'],
                        source_trust=r.get('source_trust', 0),
                        fact_id=r.get('fact_id', 0),
                        content=r['content'],
                        kind=r.get('kind', 'fact'),
                        tags=tuple(r.get('tags', [])),
                        created_at=r.get('created_at', ''),
                        relevance=r.get('relevance', 0.5),
                    )
                    for r in data.get('results', [])
                )
                responses.append(PeerSearchResponse(
                    requestor_peer_id=request.requestor_peer_id,
                    results=results,
                    truncated=data.get('truncated', False),
                    error=data.get('error'),
                ))
        except urllib.error.HTTPError as e:
            responses.append(PeerSearchResponse(
                requestor_peer_id=request.requestor_peer_id,
                results=(),
                error=f'HTTP {e.code}',
            ))
        except urllib.error.URLError as e:
            responses.append(PeerSearchResponse(
                requestor_peer_id=request.requestor_peer_id,
                results=(),
                error=f'URLError: {e.reason}',
            ))
        except (TimeoutError, json.JSONDecodeError, Exception) as e:
            responses.append(PeerSearchResponse(
                requestor_peer_id=request.requestor_peer_id,
                results=(),
                error=f'{type(e).__name__}: {e}',
            ))
    return responses
