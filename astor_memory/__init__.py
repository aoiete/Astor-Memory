"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.16.61"  # 2026-10-03 (conftest fix: 11 ERRORs → 0 ERRORs on full pytest. tests/conftest.py _ensure_admin_user_and_lock autouse fixture was missing astor_init_acl() seed. Symptom: tests/test_auto_link.py fresh_bus fixture → astor_bus() → astor_check_read() → astor_current_acl() → AttributeError on _CURRENT.actor → PermissionError_: 'astor_acl not initialized'. Same root cause for tests/test_acl.py::test_astor_bus_for_* (3 FAILEDs). Fix: conftest autouse now calls astor_init_acl(actor='admin:admin', role='admin', tier='public', user_id='admin', subscription_plan='power') BEFORE yield. actor MUST be 'admin:admin' (canonical form requires 'system'|'admin:<id>'|'user:<id>' per _ACTOR_RE) — bare 'admin' raises ValueError. After fix: pytest tests/ goes from `3 failed, 775 passed, 17 skipped, 11 errors` → `4 failed, 785 passed, 17 skipped, 0 errors` (+10 new passes, -11 errors). Remaining 4 fails are pre-existing (test_acl path-res + test_peer_* Windows file-lock) unrelated to this fix.)  # 2026-10-03 (Path-leak fix: R-class 2026-10-03. 4 user-facing endpoints returned `astor_dir: str(astor_dir)` which leaked the full on-disk path (e.g. `<runtime_dir>` or `/home/alice/.astor`). Fixed: (1) added `_astor_dir_label(p)` helper that returns only the last directory component via `PurePath(p).name`. (2) server.py L1998 /v1/dashboard error response uses helper. (3) server.py L6944 /v1/dashboard payload uses helper. (4) hermes_adapter.py L698 astor_recall result uses `Path(ASTOR_DIR).name`. (5) /v1/identity already sanitized in v1.16.50 (was correct). No behavior change for ops; users no longer see the full on-disk path in any response.)  # 2026-10-03 (Audit-fix ship: 4 critical issues + 1 minor. (1) removed <runtime_dir> hardcodes from server.py (lines 81/1305/1352/1466) and tests/_regression_check.py — replaced with ASTOR_DIR env var or ~/.astor fallback for cross-platform install. (2) removed <user> PII hardcode from /v1/fact/{id}/distill candidate_user_id list — was treating another user's ID as a fallback for any caller, allowing cross-user fact enumeration. (3) removed 13 real user handle hardcodes from dashboard_data.py _LEGACY_PRIVATE_STEMS allowlist (admin + 13 trial/real users) — replaced with 'admin'-only legacy allowlist + comment pointing to operator-side astor_user_allowlist config. (4) bumped version 1.16.53 → 1.16.55 (R12381). No runtime behavior changes for live server; cross-platform install on Linux now works for /v1/fact/{id}/distill + /v1/admin/user/{user_id}/toggle_distill endpoints.)
# 2026-09-25 (v1.15.16: S18+S19 dashboard auto-refresh. S18 — /v1/dashboard cache TTL 300s→30s + /v1/write success-path invalidates _DASHBOARD_CACHE, so hero.last_event_ts refreshes within 30s (or instantly after writes). S19 — build_dashboard_payload unions MAX(events.ts) from public + source buses; _growth_30d aggregates promoted counts across admin + public + source tiers (was admin-only). Verified: dashboard now shows live public bus ts + cross-tier growth counts. 18 dashboard tests pass.)  # 2026-09-25 (v1.15.15: forget(tombstone_only=True) now also deletes nest embeddings — previously vector recall kept surfacing tombstoned facts, verified live with fact 12576. Embeddings are regenerable from content, so delete on every forget path.)  # 2026-09-23 (v1.15.14: astor_capture_intent routes through /v1/write — success_pattern facts now land in memory_canonical)  # 2026-09-22 (Ship v1.15.8 — RRSI pass c: edit-budget alias + roadmap spec. NEW CLI flag `am decay-sweep run --max-sweep-size N` is an alias for `--limit N`, named to match the RRSI paper's "edit budget per round" concept. Same SQL, same audit, same behavior; just the paper's vocabulary. Also: NEW docs/rrsi-roadmap.md — full spec of which of the 7 RRSI constraints are shipped (a=leakage audit, b=noise floor, c=edit budget alias) vs deferred (2=ledger rationale, 3=stagnation, 6=cost rule, 7=pruning). Deferred constraints need paired eval+token tracking or operator-side tooling not yet built; ship decision documented per R12481 (wait for post-ship data before claiming transfer) + R11887 (n<20 statistically unreliable). 468 tests pass. Verified: --max-sweep-size 5 caps eligible to 5 (legacy full sweep returns 310 at max_imp=1.0 max_access=5).)

  # 2026-09-18 (Ship v1.14.73 — Memory Decay policy + WeChat article-driven improvements). NEW CLI: `am decay-sweep run --tier public|source|private|repo [--user-id] [--max-importance 0.5] [--idle-days 30] [--low-access-count 0] [--limit 500] [--execute] [--reason <str>]` + `am decay-sweep stats ...` — auto soft-tombstone facts by importance+access_count criteria. Default dry-run reports only; pass --execute to actually tombstone + write audit log entry (actor='cli:decay-sweep', action='decay_sweep'). Reversible via restore. Inspired by WeChat article "Alan 的记录与分享 - Agent Memory Lifecycle" (mp.weixin.qq.com/s/ntcO59mPowrpiaLNGGd67Q): identifies "哪些信息应该被遗忘" as a separate lifecycle stage requiring automated policy + audit trail. Criteria design choice: dropped `last_confirmed_at` from the SQL because it gets reset on every read (fact 12055), making it useless as an idle proxy. Replaced with `access_count <= N` which actually tracks "nobody cares about this fact anymore". Also fixed 2 pre-existing CLI bootstrap bugs: registered-but-undefined `cmd_peer_allow_search`, `cmd_peer_search`, `cmd_peer_unallow_search` (added v1.14.73 stub returning `[WARN] not yet implemented` + exit 1 to unblock module import + decay-sweep ship verification).
    # 2026-09-16 (v1.14.40) Recent Capture panel: dashboard `/v1/dashboard` now exposes `recent_capture` grouped by 3 axes (kind / tier / platform) plus an 'all' flatten view. Frontend tab-toggle UI (By Kind / By Tier / By Platform / All). Each bucket capped at 10 rows. Discord/Telegram/WeChat/Cron/Manual/Other routing via `origin_session_id` prefix.

# Top-level singleton accessors. Per Plan § Naming:
# astor_bus() / astor_forge() / astor_nest() are the unified public API entry points.
from .bus import astor_bus as _astor_bus_func
from .nest import astor_nest as _astor_nest_func
from . import forge as _forge_module


def astor_bus(tier: str = "public", user_id: str | None = None):
    """Return the bus singleton (events + canonical facts).

    2026-08-15 ship: tier is REQUIRED for write safety. Default is 'public'
    (read-mostly) so CLI tools that just inspect public state don't break,
    but agents writing private data must explicitly pass tier='private',
    user_id=<id>. The legacy "no tier" path was removed because it
    silently regenerated a root db bypassing 3-tier ACL.
    """
    return _astor_bus_func(tier=tier, user_id=user_id)


def astor_nest(tier: str = "public", user_id: str | None = None):
    """Return the nest singleton (vector store for facts)."""
    return _astor_nest_func(tier=tier, user_id=user_id)


def _cleanup_nest_singleton(instance) -> None:
    """v1.10.8 (2026-08-26): remove `instance` from the nest singleton dict.

    Called from AstorNest.close() so that subsequent astor_nest() calls
    with the same (tier, user_id, db_path) key rebuild the handle instead
    of returning a closed instance whose _conn is None.
    """
    from .nest import vector_store as _vs
    with _vs._nest_lock:
        singleton = _vs._nest_singleton
        if isinstance(singleton, dict):
            # Walk all keys, remove those pointing to `instance`
            stale = [k for k, v in singleton.items() if v is instance]
            for k in stale:
                del singleton[k]


def astor_forge():
    """Return the forge module (LLM fact extraction).

    Forge is a pure-functions module (no stateful singleton).
    This wrapper exists for API parity with astor_bus() / astor_nest().
    """
    return _forge_module




__all__ = ['__version__', 'astor_bus', 'astor_nest', 'astor_forge']


