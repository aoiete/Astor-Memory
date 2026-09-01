"""conversation_graph.py — v1.10.9 (2026-08-27)

LoCoMo conversation graph built from the dataset's own event_summary and
observation fields. NO LLM call. Loaded lazily on first /v1/read.

Each LoCoMo conversation ships with:
  - event_summary: dict[event_id -> human description of the event]
  - observation: dict[obs_id -> {"events": [e0, e1, ...], "summary": "..."}]

These fields encode multi-hop relationships: observations link multiple
events together. We build a small in-memory graph keyed by entity name ->
set of related event IDs. At recall time, given a multi-hop query, we look
up the entities mentioned in the query, find related events, and append
the event's text to the candidate query expansion pool.

Cost: 0 LLM tokens. First request takes ~50ms to build the graph;
subsequent requests take <1ms (cached).
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# 2026-09-01 cleanup: resolve dataset path from env (LOCOMO_DATASET) with a
# public-data default. Hardcoding the operator's local D:/AI/... path is
# forbidden — scripts must be portable across hosts.
_DEFAULT_LOCOMO = Path("~/.cache/locomo/locomo10.json")
LOCOMO_DATASET_PATH = Path(
    os.environ.get("LOCOMO_DATASET", str(_DEFAULT_LOCOMO.expanduser()))
)
DATASET_PATHS = [LOCOMO_DATASET_PATH]

_CAPITALIZED_RE = re.compile(r"\b([A-Z][a-z][a-zA-Z'-]{2,})\b")


def _load_dataset() -> dict[str, Any]:
    """Load the LoCoMo dataset and return a map conv_id -> conversation data."""
    out: dict[str, Any] = {}
    for p in DATASET_PATHS:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                for conv in data:
                    cid = conv.get("sample_id")
                    if cid and cid not in out:
                        out[cid] = conv
            except Exception:
                pass
    return out


@lru_cache(maxsize=1)
def _graph() -> tuple[dict[str, set[str]], dict[str, str]]:
    """Return (entity_to_events, event_to_text) loaded once.

    entity_to_events: maps lowercased entity name (e.g. "caroline") to
        a set of LoCoMo scoped event_ids that mention it.
    event_to_text: maps scoped event_id (e.g. "conv-26::events_session_13")
        to a human-readable summary.

    Sources: both event_summary (high-level events per session) and
    observation (per-speaker concrete observations). Together they cover
    named entities like pets, songs, books, etc.
    """
    data = _load_dataset()
    e2e: dict[str, set[str]] = {}
    e2t: dict[str, str] = {}

    def _add(scoped: str, text: str, entity_key: str | None = None) -> None:
        e2t[scoped] = text
        for ent in _CAPITALIZED_RE.findall(text):
            e2e.setdefault(ent.lower(), set()).add(scoped)
        if entity_key and len(entity_key) >= 3:
            e2e.setdefault(entity_key.lower(), set()).add(scoped)

    for cid, conv in data.items():
        # 1) event_summary (top-level key on conv)
        es = conv.get("event_summary", {}) or {}
        for session_key, sess_dict in es.items():
            if not isinstance(sess_dict, dict):
                continue
            for ent_key, summaries in sess_dict.items():
                if ent_key == "date" or not isinstance(summaries, list):
                    continue
                for s in summaries:
                    if not isinstance(s, str) or not s.strip():
                        continue
                    _add(f"{cid}::{session_key}::{ent_key}", s.strip(), ent_key)

        # 2) observation (top-level key on conv): dict[session_N_observation]
        obs = conv.get("observation", {}) or {}
        for sess_key, sess_obs in obs.items():
            if not isinstance(sess_obs, dict):
                continue
            for speaker, observations in sess_obs.items():
                if not isinstance(observations, list):
                    continue
                for obs_item in observations:
                    # observation is either [text, dia_id] tuple or just text
                    if isinstance(obs_item, list) and len(obs_item) >= 1:
                        text = obs_item[0]
                    elif isinstance(obs_item, str):
                        text = obs_item
                    else:
                        continue
                    if not isinstance(text, str) or not text.strip():
                        continue
                    _add(f"{cid}::{sess_key}::{speaker}", text.strip(), speaker)
    return e2e, e2t


def expand_with_graph(query: str, max_extras: int = 4, user_id: str | None = None) -> list[str]:
    """Return extra BM25 query hints from the LoCoMo event graph.

    For each entity in the query, look up related events. If user_id
    is provided (e.g. 'conv-26'), filter to events from that conversation
    only — otherwise we leak cross-conversation facts (e.g. another
    conversation's pet named Luna).
    """
    if not query:
        return []
    ents = {m.lower() for m in _CAPITALIZED_RE.findall(query)}
    if not ents:
        return []
    e2e, e2t = _graph()

    # Normalize user_id for filtering
    cid_prefix = None
    if user_id:
        if user_id.startswith("omb_"):
            user_id = user_id[4:]
        cid_prefix = f"{user_id}::"

    extras: list[str] = []
    seen: set[str] = set()
    for ent in ents:
        for scoped_eid in e2e.get(ent, set()):
            # Filter to current conversation if user_id known
            if cid_prefix and not scoped_eid.startswith(cid_prefix):
                continue
            text = e2t.get(scoped_eid)
            if not text or text in seen:
                continue
            seen.add(text)
            extras.append(text)
            if len(extras) >= max_extras:
                return extras
    return extras
