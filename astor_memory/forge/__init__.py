"""
astor_forge: LLM-based fact extraction with fallback provider chain.

Per Plan § Bus direct entry:
- extract_mode='auto': regex for short, none for long, opt-in LLM
- Default: 0 LLM API calls per ingest
- Regex + heuristic for categorize/score

Per Plan § LLM fallback provider:
- Primary provider, fallback chain
- Retry per provider, then next

Per 9-db layout (2026-08-15 supersede):
- Public state (extractor regex config) lives in module-level constants
- Per-tier persistence (llm_call_log) lives in `astor_forge_<tier>.db`
- astor_forge_for(tier, user_id) opens the per-tier connection

Lock: 2026-08-15. Previously forge was pure in-memory (no DB); now also
persists llm_call_log to audit LLM calls.
"""

from .extractor import (
    astor_extract_facts, AstorFact, AstorExtractMode,
    astor_regex_extract, astor_choose_extract_mode, astor_detect_capture_intent,
)
from .llm_extract import astor_llm_extract
from .schema import astor_init_forge_schema, astor_verify_forge_schema, FORGE_SCHEMA_VERSION

import sqlite3
import threading
__all__ = [
    'astor_extract_facts', 'AstorFact', 'AstorExtractMode',
    'astor_regex_extract', 'astor_choose_extract_mode', 'astor_detect_capture_intent',
    'astor_llm_extract',
    'astor_forge_for', 'astor_forge_log_call',
    'astor_init_forge_schema', 'astor_verify_forge_schema', 'FORGE_SCHEMA_VERSION',
]


_forge_lock = threading.Lock()
_forge_conns: dict[tuple[str, str | None], sqlite3.Connection] = {}


def astor_forge_for(tier: str, user_id: str | None = None) -> sqlite3.Connection:
    """
    Open (or reuse) the per-tier forge SQLite connection.

    Args:
        tier:    'public' | 'source' | 'private'
        user_id: required when tier='private', else None

    Raises PermissionError_ if ACL denies access.

    Returns sqlite3.Connection with llm_call_log + schema_migrations tables.
    """
    from .._internal.acl_layout import get_db_path as _gdp, ensure_layout, Tier, Store
    from .._internal.acl import astor_check_write

    astor_check_write(tier, user_id)
    path = _gdp(Tier(tier), Store.FORGE, user_id)
    key = (tier, user_id)
    with _forge_lock:
        if key in _forge_conns:
            return _forge_conns[key]
        ensure_layout(Tier(tier), Store.FORGE, user_id)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        astor_init_forge_schema(conn)
        _forge_conns[key] = conn
        return conn


def astor_forge_log_call(
    actor: str,
    user_id: str,
    tier: str,
    provider: str,
    operation: str,
    input_hash: str,
    input_length: int,
    output_json: str | None = None,
    success: int = 1,
    error_msg: str | None = None,
    latency_ms: int | None = None,
    model: str | None = None,
    reason: str | None = None,
) -> int:
    """
    Persist one llm_call_log row in the (tier, user_id) forge db.

    Returns the inserted row id.
    """
    conn = astor_forge_for(tier, user_id)
    cur = conn.execute(
        """INSERT INTO llm_call_log
           (actor, user_id, tier, provider, operation, input_hash, input_length,
            output_json, success, error_msg, latency_ms, model, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor, user_id, tier, provider, operation, input_hash, input_length,
         output_json, success, error_msg, latency_ms, model, reason),
    )
    rid = cur.lastrowid
    assert rid is not None
    return rid
