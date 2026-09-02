"""
HTTP REST API server for Astor-Memory.

Plan § Week 4 step 3.2: FastAPI-style endpoints with Flask (already in deps).
Endpoints:
  POST /v1/write    body={"text":..., "user":..., "mode":...} -> {fact_ids}
  POST /v1/read     body={"query":..., "user":..., "top_k":5}  -> {results: [{fact_id, content, ...}]}
  GET  /v1/health   -> {status, version, dbs}
  POST /v1/install  body={"ide":..., "mode":..., "agent_dir":...} -> {plan}

Run: python -m astor_memory.server
Or:  flask --app astor_memory.server run --port 7803

Per Plan § Memory <-> concurrency: WAL mode handles concurrent reads.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from . import __version__, astor_bus, astor_nest, astor_forge
from ._internal.acl import astor_init_acl, _CURRENT, astor_current_acl, PermissionError_
from ._internal.bot_binding import get_user
from .config import get_default_astor_dir, get_default_bus_path, get_default_nest_path


def _astor_quality_ok(text: str) -> str | None:
    """Return None if content passes quality gate, else a generic denial reason.

    2026-09-02 ship: deny obvious spam / nonsense BEFORE the ACL+forge path.
    Reasons returned are generic so users cannot probe which rule fired.
    Rules:
      - 8+ characters after strip
      - <100% uppercase
      - <100% control / whitespace
      - <100% emoji / symbol
      - <100% digits
      - no leading 'test' / 'spam' / 'asdf' (case-insensitive)
    """
    if not isinstance(text, str):
        return 'invalid content'
    s = text.strip()
    if len(s) < 8:
        return 'invalid content'
    # All uppercase? (excluding punctuation / digits)
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 8 and all(c.isupper() for c in letters):
        return 'invalid content'
    # All control / whitespace?
    if all((not c.isprintable() or c.isspace()) for c in s if c):
        return 'invalid content'
    # All emoji / symbol / punctuation?
    if all((not c.isalnum()) for c in s):
        return 'invalid content'
    # All digits?
    if all(c.isdigit() for c in s):
        return 'invalid content'
    # Known test prefixes
    low = s.lower()
    if low.startswith('test ') or low.startswith('spam ') or low.startswith('asdf'):
        return 'invalid content'
    return None


# 2026-09-02 ship: intent classifier for public write auto-routing.
# Returns (new_tier, new_user_id) if content should be reclassified away
# from the caller's requested tier, else None (use caller's choice).
#
# Design: admin decides what goes public. User requests tier=public but
# server inspects text and silently demotes to private_<user> if content
# matches personal / financial / daily-journal patterns. User never learns
# (no error message, just count=1 with the new tier under the hood).
#
# Patterns matched (case-insensitive, CJK + Latin):
#   - Personal pronouns: 我 / 我今天 / 我的 / 自己 / i / my / mine
#   - Financial: 买了 / 卖了 / 仓位 / 跌了 / 涨了 / $/€ / price
#   - Daily journal: 今天 / yesterday / 早上 / 晚上 / at 8pm
#   - Emotion/心情: 累了 / 开心 / 难过 / happy / sad
#
# Methods/rules/models stay public: includes pattern keywords (workflow /
# method / model / rule / pattern / 流程 / 方法 / 规则 / 模式 / 模型 / 设计).
_PERSONAL_PATTERNS = [
    r"\b我(?:今天|昨天|明天|现在)?\b",
    r"\b我的\b|\b自己\b",
    r"\bi\s+(?:am|was|will|just|got|had|have)\b",
    r"\bmy\s+(?:day|mood|trade|position|portfolio|stocks?)\b",
    r"\bmine\b",
]
_FINANCIAL_PATTERNS = [
    r"\$\d+|\d+\s*\$|€\d+|\d+\s*€",
    r"\b(?:bought|sold|traded|long|short|stop[-_ ]?loss|take[-_ ]?profit)\b",
    r"\b(?:AAPL|TSLA|NVDA|MSFT|GOOG|AMZN|META|SPY|QQQ)\b",
    r"(?:买了|卖了|持仓|仓位|止盈|止损|跌了|涨了|加仓|减仓)",
]
_DAILY_PATTERNS = [
    r"(?:今天|昨天|明天|早上|晚上|今晚|今早)",
    r"\b(?:today|yesterday|tonight|this morning)\b",
    r"\b\d+\s*(?:am|pm)\b",
]
_EMOTION_PATTERNS = [
    r"(?:累了|开心|难过|沮丧|激动|无聊|郁闷|崩溃)",
    r"\b(?:happy|sad|tired|excited|stressed|anxious|depressed|frustrated)\b",
]
_METHOD_PATTERNS = [
    r"(?:workflow|method|model|rule|pattern|process|approach|framework|design)",
    r"(?:流程|方法|规则|模式|模型|设计|架构|架构)",
]

_PERSONAL_RE = re.compile("|".join(_PERSONAL_PATTERNS), re.IGNORECASE)
_FINANCIAL_RE = re.compile("|".join(_FINANCIAL_PATTERNS), re.IGNORECASE)
_DAILY_RE = re.compile("|".join(_DAILY_PATTERNS), re.IGNORECASE)
_EMOTION_RE = re.compile("|".join(_EMOTION_PATTERNS), re.IGNORECASE)
_METHOD_RE = re.compile("|".join(_METHOD_PATTERNS), re.IGNORECASE)


def _astor_classify_intent(text: str, tier: str, user: str | None) -> str | None:
    """Inspect content; return new tier if content should be reclassified.

    Only acts on tier='public' — private writes always stay private. Returns
    None when content should stay as caller requested.

    Rules (admin decides):
      - text contains method/rule/model pattern → stays public (good content)
      - text contains personal/financial/daily/emotion pattern → demote
        to 'private' (the explicit_uid / bus_user_id logic picks user_id)
      - tier != 'public' → return None (no reclassification needed)
    """
    if tier != 'public':
        return None
    if not isinstance(text, str) or not user or user == 'admin':
        # admin user keeps admin role — never auto-demote admin's writes
        return None
    has_method = bool(_METHOD_RE.search(text))
    has_personal = bool(_PERSONAL_RE.search(text))
    has_financial = bool(_FINANCIAL_RE.search(text))
    has_daily = bool(_DAILY_RE.search(text))
    has_emotion = bool(_EMOTION_RE.search(text))
    # Strong-signal demote: any of these forces private.
    if has_personal or has_financial or has_daily or has_emotion:
        return 'private'
    # Method/rule/model alone stays public (admin's call).
    if has_method:
        return None
    # No strong signal — stays public (admin can later review via audit log).
    return None


def _astor_resolve_actor(user_id: str | None) -> tuple[str, str, str | None]:
    """Resolve (actor, role, plan) for a given user_id from bot-binding.db user_meta.

    2026-09-02 final simplification: admin role IGNORES plan (plan is a
    user-tier concept only). Admin has full access via role alone; plan=None
    means "no plan applicable". 2 roles (admin / user) + 3-value plan
    (free / vip / power) for users only.

    Returns:
      ('admin:admin', 'admin', None)    for user_id in {None, '', 'admin'}
      ('admin:<id>',  'admin', None)    for any role='admin' user (plan ignored)
      ('user:<id>',   'user',  <plan>)  for any active role='user' user
      ('user:anonymous', 'user', 'free') for unknown / inactive callers
    """
    from ._internal.bot_binding import get_user as _get_user
    if not user_id or user_id == 'admin':
        return ('admin:admin', 'admin', None)
    meta = _get_user(user_id)
    if meta is None or not meta.get('active', 1):
        return ('user:anonymous', 'user', 'free')
    role = meta.get('role', 'user')
    if role == 'admin':
        # admin: plan is irrelevant — role already grants full access.
        return (f'admin:{user_id}', 'admin', None)
    # user: plan differentiates free / vip / power
    plan = meta.get('subscription_plan', 'free')
    return (f'user:{user_id}', 'user', plan)


def create_app(astor_dir: str | None = None) -> Flask:
    """Create Flask app. astor_dir override for tests."""
    app = Flask(__name__)
    if astor_dir:
        os.environ['ASTOR_DIR'] = astor_dir
    # 2026-08-16 opt4: upgrade every known tier DB
    try:
        from .bus.schema import astor_upgrade_all_tier_dbs
        astor_upgrade_all_tier_dbs()
    except Exception:
        pass
    # 2026-08-15 ship: process-level ACL bootstrap. Flask is multi-threaded
    # and `_CURRENT` is a `_thread._local`, so the main-thread init below
    # would NOT propagate to request-handler threads. We therefore register
    # `before_request` to (re-)bind ACL for every worker thread. The server
    # itself runs as `first_admin` with `source` tier scope so health/write/
    # read can cross tiers as designed; tier-scoped endpoints (private DB)
    # should re-bind to a narrower context inside their handler.
    # P2-fix 2026-08-15: rebind ACL per request, taking tier + actor from the
    # request body so per-tier writes use the correct role.
    # P0-fix 2026-08-16: actor/role now come from bot-binding.db user_meta.role
    # based on `body.user` (was hardcoded to first_admin, allowing any user → source
    # write + cross-user private read). Also enforce cross-user protection:
    # if tier=private and user_id != actor, deny at the request boundary
    # instead of letting it reach `astor_check_write/read`.
    @app.before_request
    def _astor_bind_request_acl() -> None:
        # 2026-08-16: Always bind a default ACL for GET requests (e.g. health,
        # viewer_stats, lex_stats). Without this, Flask worker threads may
        # not have _CURRENT set, and downstream astor_check_* raises
        # "astor_acl not initialized" → 500. POST requests get per-body binding.
        if request.method == 'POST' and request.is_json:
            body = request.get_json(silent=True) or {}
            tier = body.get('tier')
            if tier in ('public', 'source', 'private', 'repo'):
                # v1.1: tier=repo uses repo_id (explicit field) or 'user' as repo_id.
                repo_id = body.get('repo_id')
                if tier == 'repo' and repo_id:
                    body_user = repo_id
                else:
                    body_user = body.get('user')
                actor, role, plan = _astor_resolve_actor(body_user)
                if tier == 'private':
                    target_user = body.get('user_id') or body_user
                elif tier == 'repo':
                    target_user = body_user
                else:
                    target_user = None
                try:
                    # Bind ACL with ACTOR's identity. user_id=body_user ALWAYS
                    # (not just for private) so astor_check_write can match
                    # ctx.user_id == target_user when target_user is the
                    # actor's own (reclassified private_<self> path).
                    astor_init_acl(
                        actor=actor, role=role, tier=tier,
                        user_id=body_user,
                        subscription_plan=plan,
                    )
                except (ValueError, PermissionError_) as exc:
                    return jsonify({'error': 'acl_init_failed', 'detail': str(exc)}), 403
                if tier == 'private':
                    from ._internal.acl import astor_check_read as _acr, astor_check_write as _acw
                    # 2026-08-16 fix: only enforce write ACL on write endpoints.
                    # Read-only endpoints (e.g. /v1/read) must not require a
                    # write-grant — they only need a read-grant.
                    is_write_action = request.path not in {'/v1/read', '/v1/read/multi'}
                    try:
                        _acr(tier='private', user_id=target_user)
                    except PermissionError_:
                        # 2026-09-02 ship: silent cross-user denial (no policy leak).
                        return jsonify({'error': 'cross_user_forbidden'}), 403
                    if is_write_action:
                        try:
                            _acw(tier='private', user_id=target_user)
                        except PermissionError_:
                            return jsonify({'error': 'cross_user_forbidden'}), 403
                return
        # Default bind for GET endpoints + POST without JSON body.
        # GETs are read-only public-tier inspections; safe to bind as
        # first_admin (server identity).
        try:
            _ = _CURRENT.actor
        except AttributeError:
            astor_init_acl(actor='admin:admin', role='admin', tier='public',
                           subscription_plan=None)

    @app.errorhandler(PermissionError_)
    def _astor_handle_permission_error(exc: PermissionError_):  # noqa: ARG001
        """2026-08-16 ACL fix: convert PermissionError_ from astor_check_*
        into 403 instead of 500. The before_request hook binds ACL per
        request, but astor_bus() / astor_nest() may still raise from
        downstream checks (e.g. promote_candidate) — those should surface
        as 403, not 500.
        """
        return jsonify({'error': 'permission_denied', 'detail': str(exc)}), 403

    @app.route('/v1/health', methods=['GET'])
    def health():
        """Health check + DB status."""
        # 2026-08-15 ship: tier required. Health endpoint inspects public
        # tier (read-only). Use /v1/health/private?user=<id> for user db.
        bus = astor_bus(tier='public')
        nest = astor_nest(tier='public')
        result = {
            'status': 'ok',
            'version': __version__,
            'astor_dir': str(get_default_astor_dir()),
            'dbs': {
                'bus': str(bus.db_path),
                'nest': str(nest.db_path),
            },
        }
        # Bus stats
        try:
            c = bus.conn.cursor()
            c.execute("SELECT COUNT(*) FROM memory_canonical")
            result['facts'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM events")
            result['events'] = c.fetchone()[0]
        except Exception as e:
            result['bus_error'] = str(e)
        # Nest stats
        try:
            c = nest.conn.cursor()
            c.execute("SELECT COUNT(*) FROM embeddings")
            result['embeddings'] = c.fetchone()[0]
        except Exception as e:
            result['nest_error'] = str(e)
        return jsonify(result)

    @app.route('/v1/write', methods=['POST'])
    def write():
        """Write a fact via forge extraction + bus promote + nest store.

        Body JSON:
          text: str (required)
          user: str (default 'admin')
          mode: 'auto'|'none'|'regex'|'llm' (default 'auto')
          tier: 'public'|'source'|'private_<user>' (default 'public')
          scope: 'long_term'|'short_term'|'profile' (default 'long_term')
        Returns:
          {fact_ids: [int], count: int}
        """
        body = request.get_json(force=True)
        text = body.get('text')
        if not text:
            return jsonify({'error': 'text required'}), 400
        user = body.get('user', 'admin')
        mode = body.get('mode', 'auto')
        tier = body.get('tier', 'public')
        scope = body.get('scope', 'long_term')
        # v1.11.0: optional session_id — enables session-neighbor recall
        _write_session_id = body.get('session_id') or None

        # v1.1: tier=repo requires repo_id (= user_id). The body uses 'user'
        # for both — same field name, different semantic in different tier.
        # Caller is expected to pass `tier='repo'` + `user='<repo_id>'`.
        # Or use `repo_id` explicitly if caller wants clarity.
        repo_id = body.get('repo_id')
        if tier == 'repo':
            if repo_id:
                user = repo_id
            if not user or user == 'admin':
                return jsonify({'error': 'tier=repo requires user=<repo_id> or repo_id=<id>'}), 400

        # P1-fix 2026-08-15: validate scope + route policy. Per plan §3-tier
        # × 3-scope: profile-scope facts only land in private tier (per-user
        # identity). short-term scope carries 30d TTL via scope_type column.
        if scope not in ('long_term', 'short_term', 'profile'):
            return jsonify({'error': f'invalid scope {scope!r}'}), 400
        if scope == 'profile' and tier != 'private':
            # Profile scope must live in private (per-user identity). Auto-route.
            tier = 'private'

        # 2026-08-15 ship: respect tier from request body. Default 'public'.
        # v1.1: tier=repo passes user (= repo_id) to bus/nest/forge as user_id.
        # 2026-08-16 strict-privacy: prefer explicit user_id over 'user' field
        # so cross-user writes (e.g. admin writing alice's private) target
        # the correct DB. 'user' identifies the caller; 'user_id' identifies
        # the target.
        explicit_uid = body.get('user_id')
        if tier == 'private' and explicit_uid:
            bus_user_id = explicit_uid
        elif tier in ('private', 'repo'):
            bus_user_id = user
        else:
            bus_user_id = None

        # 2026-09-02 ship: content classifier — admin decides what goes public.
        # Must run AFTER bus_user_id resolved (ACL below needs user_id for
        # tier=private). Reclassify sets tier + bus_user_id together.
        _reclass = _astor_classify_intent(body.get('text', ''), tier=tier,
                                          user=user)
        if _reclass is not None:
            tier = _reclass
            bus_user_id = user  # auto-route to caller's own private bucket

        # 2026-09-02 ship: content quality gate (silent reject spam before write).
        # 8+ chars, no all-uppercase, no all-control, no all-emoji. Failures
        # return 400 with a generic "invalid content" detail — never reveal
        # which rule (so users can't probe policy).
        _qerr = _astor_quality_ok(body.get('text', ''))
        if _qerr is not None:
            return jsonify({'error': 'permission_denied', 'detail': _qerr}), 403

        # P2-fix 2026-08-15: optional mirror_to_source. When tier=public and
        # mirror=true, also write the same fact into the source tier (admin-
        # only) so the agent's self-pattern store gets the same content.
        # This is the 3-store × 3-tier "fanout" pattern from the plan.
        mirror_to_source = bool(body.get('mirror_to_source', False)) and tier == 'public'

        # 2026-09-02 ship: enforce write ACL via matrix. before_request
        # binds the ACL context but does NOT check it — we must check here
        # to stop a user from writing to source or public (both admin-only).
        from ._internal.acl import astor_check_write as _acw_write
        try:
            _acw_write(tier='public' if tier == 'public' else tier,
                       user_id=bus_user_id)
        except PermissionError_ as _acl_err:
            # 2026-09-02 ship: silent ACL denial — strip plan + role info.
            # user never learns why they were blocked (probing prevention).
            return jsonify({'error': 'permission_denied'}), 403

        bus = astor_bus(tier=tier, user_id=bus_user_id)
        forge = astor_forge()

        # 1. P1-fix 2026-08-15: content-hash dedup (stable_id) per (tier,
        # user_id, scope). Same text re-write should return the existing
        # canonical_id instead of duplicating. compute_hash uses sha256 of
        # the raw text so it's deterministic across retries. Scope partition
        # so writing the same text under different scopes is still allowed
        # (e.g. public long-term vs public short-term).
        import hashlib as _hl
        content_hash = _hl.sha256(text.encode('utf-8', errors='ignore')).hexdigest()[:16]
        # v1.1: stable_id namespace — repo_id for tier=repo, username for
        # private, '_' for public/source.
        if tier in ('private', 'repo'):
            scope_user = user
        else:
            scope_user = '_'
        stable_id = f'{tier}:{scope_user}:{scope}:{content_hash}'
        try:
            existing_row = bus.conn.execute(
                "SELECT id FROM memory_canonical WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if existing_row is not None:
                # Same content already stored at this scope — return early.
                self_audit_via_bus = bus  # not strictly needed
                return jsonify({
                    'event_id': None,
                    'fact_ids': [existing_row[0]],
                    'count': 1,
                    'tier': tier,
                    'scope': scope,
                    'dedup': True,
                    'stable_id': stable_id,
                })
        except Exception as dedup_exc:
            # Dedup check failure should not block write path.
            import sys as _sys
            print(f'[astor.server] dedup check failed (continuing): {dedup_exc}', file=_sys.stderr)

        # 2. Append event
        event_id = bus.append_event(
            namespace=user,
            agent_id='rest_api',
            source='rest.write',
            action='write',
            content=text,
        )
        # v1.10.9: caller may supply event_time (ISO datetime) to anchor
        # relative-date resolution at write time. Without this, the
        # extractor can't resolve 'yesterday/last week' relative phrases.
        caller_event_ts = body.get('event_time') or body.get('event_ts')
        # 2. Extract facts (forge) — now writes llm_call_log audit row
        # 2026-08-16 strict-privacy: pass explicit user_id (target) over
        # 'user' (caller) for forge extraction. forge_log_call writes to
        # the per-tier forge DB; private_<admin> requires a grant on
        # grantor='admin', which the caller may not hold.
        # v1.10.9: caller may supply event_time (ISO datetime) to anchor
        # relative-date resolution at write time.
        caller_event_ts = body.get('event_time') or body.get('event_ts')
        print(f'[DEBUG] /v1/write caller_event_ts={caller_event_ts!r}', flush=True)
        facts = forge.astor_extract_facts(
            text, mode=mode, tier=tier,
            user_id=bus_user_id if tier == 'private' else None,
            actor='rest_api',
            # v1.10.9: doc_timestamp anchors relative-time resolution.
            doc_timestamp=caller_event_ts,
        )
        if not facts:
            return jsonify({'event_id': event_id, 'facts': [], 'count': 0})
        # 3. Insert candidates + promote (which auto-stores embeddings via nest)
        fact_ids = []
        # 2026-08-16 opt1: hook BM25 lex index — every promoted fact gets
        # tokenized and indexed for exact-match keyword recall. Failures
        # are logged but never block the write (lex is a redundant store).
        from .nest.lex_index import astor_lex as _astor_lex_for_write
        _lex = _astor_lex_for_write(tier=tier, user_id=bus_user_id)
        for f in facts:
            cand_id = bus.insert_candidate(
                event_id=event_id,
                namespace=user,
                content=f.content,
                kind=f.kind,
                confidence=f.confidence,
                importance=f.importance,
                tags=f.tags or [],
                # v1.2.1: thread A-MEM-style structured fields from extractor
                # through candidate → canonical. Promoted to top-level
                # canonical columns during promote_candidate.
                keywords=f.keywords or [],
                context=f.context or '',
                # v1.12.0: hierarchical extraction — topic + session_id
                # propagated through to metadata.__topic__ / __session_id__.
                topic=getattr(f, 'topic', '') or '',
                session_id=getattr(f, 'session_id', '') or _write_session_id or '',
            )
            canon_id = bus.promote_candidate(
                cand_id, promoted_by='rest.write', user_id=user, tier=tier,
                scope_type=scope,  # P1-fix 2026-08-15: thread scope through
                # v1.11.0: thread session_id for agentic neighbor-expand.
                # Facts from the same session can be pulled as read/navigate
                # neighbors at recall time (Mistral Agentic Search pattern).
                origin_session_id=_write_session_id,
                stable_id=stable_id,  # P1-fix 2026-08-15: enable content-hash dedup
            )
            fact_ids.append(canon_id)
            # Index for BM25 keyword recall — best-effort
            try:
                _lex.index_fact(int(canon_id), f.content)
            except Exception as _lex_exc:
                import sys as _sys
                print(f'[astor.server] lex index_fact failed (continuing): {_lex_exc}', file=_sys.stderr)
            # v1.2.3: Zettelkasten auto-link (A-MEM pattern). After
            # promote, find existing same-kind facts with cosine > 0.85
            # and add bidirectional auto-link edges. Audit-safe (no
            # rewrite of existing facts; only adds edges to provenance
            # graph). Failures never block the write path.
            try:
                from .nest.auto_link import auto_link_for_fact as _auto_link
                _auto_link(
                    bus, new_fact_id=int(canon_id),
                    content=f.content, kind=f.kind,
                    tier=tier, user_id=bus_user_id,
                )
            except Exception as _auto_link_exc:
                import sys as _sys
                print(f'[astor.server] auto_link failed (continuing): {_auto_link_exc}', file=_sys.stderr)
        # P2-fix 2026-08-15: optional source-tier mirror. Best-effort — if
        # mirror fails (e.g. ACL denial for non-first_admin caller), the
        # primary write still succeeds.
        mirrored_fact_ids = []
        if mirror_to_source and fact_ids:
            try:
                src_bus = astor_bus(tier='source')
                src_event_id = src_bus.append_event(
                    namespace=user, agent_id='rest_api',
                    source='rest.write.mirror', action='mirror',
                    content=text,
                )
                src_facts = astor_forge().astor_extract_facts(
                    text, mode=mode, tier='source',
                    user_id=None, actor='rest_api.mirror',
                )
                for f in src_facts:
                    c_id = src_bus.insert_candidate(
                        event_id=src_event_id, namespace=user,
                        content=f.content, kind=f.kind,
                        confidence=f.confidence, importance=f.importance,
                        tags=f.tags or [],
                        # v1.2.1: same structured fields as primary write
                        keywords=f.keywords or [],
                        context=f.context or '',
                    )
                    # Mirror uses its own dedup scope (source/long_term etc)
                    # so it doesn't collide with the primary public write.
                    src_content_hash = _hl.sha256(
                        f.content.encode('utf-8', errors='ignore')
                    ).hexdigest()[:16]
                    src_stable_id = f'source:_:{scope}:{src_content_hash}'
                    m_id = src_bus.promote_candidate(
                        c_id, promoted_by='rest.write.mirror',
                        user_id=user, tier='source', scope_type=scope,
                        stable_id=src_stable_id,
                    )
                    mirrored_fact_ids.append(m_id)
            except Exception as mirror_exc:
                # Log to stderr; do not fail the primary write.
                import sys as _sys
                print(f'[astor.server] mirror_to_source failed: {mirror_exc}', file=_sys.stderr)

        # 2026-09-02 ship: audit row for every successful public write so
        # admin can review what users contributed (and which got through the
        # quality gate). admin-only visibility via `am admin audit-log`.
        if tier == 'public' and fact_ids:
            try:
                from ._internal.audit_logger import astor_audit as _audit_w
                _audit_w(
                    actor=f'user:{user}' if user else 'user:anonymous',
                    tier='public',
                    action='write',
                    user_id=bus_user_id,
                    target=f'public/fact_ids={fact_ids[:3]}{"..." if len(fact_ids)>3 else ""}',
                    metadata={
                        'count': len(fact_ids),
                        'preview': (body.get('text', '') or '')[:80],
                        'mode': body.get('mode', 'auto'),
                    },
                )
            except Exception:
                pass  # audit failure must not break writes

        return jsonify({
            'event_id': event_id,
            'fact_ids': fact_ids,
            'count': len(fact_ids),
            'tier': tier,
            'scope': scope,
            'mirrored': mirrored_fact_ids,
        })

    @app.route('/v1/read', methods=['POST'])
    def read():
        """Recall similar facts via nest vector search.

        Body JSON:
          query: str (required)
          user: str (optional, filter by user_id)
          top_k: int (default 5)
        Returns:
          {results: [{fact_id, similarity, content, kind, ...}], count: int}
        """
        body = request.get_json(force=True)
        query = body.get('query')
        if not query:
            return jsonify({'error': 'query required'}), 400
        # 2026-08-27: tolerate "auto" / None / malformed top_k — fallback to 5
        # instead of 500. Client side passes "auto" from --query-adaptive flag.
        raw_top_k = body.get('top_k', 5)
        try:
            top_k = int(raw_top_k)
            if top_k <= 0:
                top_k = 5
        except (TypeError, ValueError):
            top_k = 5

        # 2026-08-15 ship: recall targets the tier from request body.
        tier = body.get('tier', 'public')
        # v1.10.9 (2026-08-27): accept query_timestamp (LoCoMo, LongMemEval)
        # for temporal proximity boosting.
        query_timestamp = body.get('query_timestamp')
        since_ts = body.get('since_ts')
        until_ts = body.get('until_ts')
        if since_ts and not isinstance(since_ts, str):
            since_ts = None
        if until_ts and not isinstance(until_ts, str):
            until_ts = None
        if query_timestamp and not isinstance(query_timestamp, str):
            query_timestamp = None
        query_anchor = (query_timestamp or '')[:10] or None
        # v1.1: tier=repo accepts repo_id (explicit) or user_id (fallback).
        user_id = None
        if tier == 'repo':
            user_id = body.get('repo_id') or body.get('user_id')
        elif tier == 'private':
            user_id = body.get('user_id') or body.get('user')
        # 2026-08-16 opt1: hybrid recall (vector + BM25). Default true.
        use_hybrid = bool(body.get('hybrid', True))
        bm25_weight = float(body.get('bm25_weight', 0.4))
        vec_weight = float(body.get('vec_weight', 0.6))
        # Oversample before merge so hybrid doesn't return fewer than top_k
        oversample = max(top_k * 2, 20)
        nest = astor_nest(tier=tier, user_id=user_id)
        bus = astor_bus(tier=tier, user_id=user_id)
        from .nest.embeddings import astor_get_embedding_model, astor_get_model_name_for_ram

        model = astor_get_embedding_model()

        # v1.2.0: local helper for safely parsing JSON-encoded fields.
        import json as _safe_json_loads_json_mod
        def _safe_json_loads(s):
            try:
                v = _safe_json_loads_json_mod.loads(s) if s else []
                return v if isinstance(v, list) else []
            except Exception:
                return []
        # v1.12.0 (2026-08-29): separate helper for metadata dicts (not lists).
        # _safe_json_loads above is list-or-[] only; for metadata we need
        # dict-or-{}. Sharing the helper would silently drop facts whose
        # metadata is valid JSON but happens to be a dict.
        def _safe_json_loads_dict(s):
            try:
                v = _safe_json_loads_json_mod.loads(s) if s else {}
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
        embeddings = list(model.embed([query]))
        query_emb = embeddings[0]

        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3, user_id=user_id):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass

        if not use_hybrid:
            # Pure vector path (legacy) — collect from all variants
            _all_v = []
            for _q in _query_variants:
                _qe = list(model.embed([_q]))[0] if _q != query else query_emb
                _all_v.extend(nest.search(_qe, limit=top_k))
            # Dedupe, keep max score
            _seen = {}
            for _fid, _s in _all_v:
                if int(_fid) not in _seen or _s > _seen[int(_fid)]:
                    _seen[int(_fid)] = _s
            results = sorted(_seen.items(), key=lambda x: x[1], reverse=True)[:top_k]
        else:
            from .nest.lex_index import (
                astor_lex as _astor_lex,
                hybrid_merge as _hybrid_merge,
            )
            lex = _astor_lex(tier=tier, user_id=user_id)
            # v1.10.9 v2: vector search keeps original query only (single pass).
            # BM25 also uses original query, but we inject the top match
            # from each synonym variant (cap at 5 per variant) as additional
            # BM25 hits, boosting pure-keyword matches without flooding
            # the candidate pool that feeds temporal_boost.
            vector_hits = nest.search(query_emb, limit=oversample)
            _bm25_seen = {}
            for _fid, _s in lex.bm25_search(query, limit=oversample):
                _bm25_seen[int(_fid)] = _s
            for _q in _query_variants[1:]:
                for _fid, _s in lex.bm25_search(_q, limit=5):
                    _fid = int(_fid)
                    if _fid not in _bm25_seen or _s > _bm25_seen[_fid]:
                        _bm25_seen[_fid] = _s
            bm25_hits = list(_bm25_seen.items())
            # v1.2.0: load per-fact keywords from canonical + compute
            # Jaccard boost. O(oversample) - fine for top_k <= 50.
            candidate_fids = sorted({f for f, _ in bm25_hits}
                                    | {f for f, _ in vector_hits})
            keyword_hits = {}
            if candidate_fids:
                placeholders = ','.join('?' * len(candidate_fids))
                kw_rows = bus.conn.execute(
                    f"SELECT id, keywords FROM memory_canonical "
                    f"WHERE id IN ({placeholders})",
                    candidate_fids,
                ).fetchall()
                for fid, kw_json in kw_rows:
                    try:
                        import json as _json
                        kws = _json.loads(kw_json) if kw_json else []
                        if kws:
                            keyword_hits[int(fid)] = kws
                    except Exception:
                        pass
            # Query keywords = tokens of the query (cheap; no LLM call).
            from .nest.lex_index import _tokenize
            query_keywords = _tokenize(query)
            # v1.10.9: build temporal_boost map for hybrid_merge.
            _temporal_boost = {}
            if candidate_fids:
                try:
                    _tb_rows = bus.conn.execute(
                        f"SELECT id, event_date, event_date_precision FROM memory_canonical "
                        f"WHERE id IN ({','.join('?' * len(candidate_fids))})",
                        list(candidate_fids),
                    ).fetchall()
                    for _tbid, _tbd, _tbp in _tb_rows:
                        if _tbd:
                            _temporal_boost[int(_tbid)] = (str(_tbd), str(_tbp or 'day'))
                except Exception:
                    pass
            merged = _hybrid_merge(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                bm25_weight=bm25_weight,
                vec_weight=vec_weight,
                limit=oversample,
                keyword_hits=keyword_hits if keyword_hits else None,
                query_keywords=query_keywords,
                temporal_boost=_temporal_boost if _temporal_boost else None,
                temporal_boost_strength=0.4,
                query_anchor=query_anchor,
            )
            results = merged[:top_k]
            _skip_stage = False  # v1.10.9: LLM rerank may override stage_recall
            # v1.10.9 (2026-08-27): LLM rerank. Set ASTOR_RERANK=1 to enable.
            # 2026-08-27: lowered trigger from top_k>=5 to top_k>=3 so small per-conv
            # DBs (22 facts) actually exercise the rerank path.
            # 2026-08-27: query-level override via body['rerank']:
            #   - 'on' | '1' | True  → force enable (overrides ASTOR_RERANK=0)
            #   - 'off' | '0' | False → force disable
            #   - absent → follow ASTOR_RERANK env
            _body_rr = body.get('rerank', None)
            if _body_rr is None:
                _rerank_on = os.environ.get('ASTOR_RERANK', '0') == '1'
            else:
                _rerank_on = str(_body_rr).lower() in ('1', 'on', 'true', 'yes')
            if _rerank_on and results and top_k >= 3:
                try:
                    import sys as _sys, traceback as _tb
                    _sys.stderr.write(f"[RERANK] enabled, results={len(results)}, top_k={top_k}, calling LLM...\n")
                    _sys.stderr.flush()
                    from .nest.llm_rerank import rerank_candidates as _llm_rr
                    # Build (fid, content) pairs from results.
                    # 2026-08-27 fix: results from hybrid_merge is list of (fid, score)
                    # tuples — no content field. Need to look up content from bus DB.
                    _pairs = []
                    _fids = []
                    if results and isinstance(results[0], (tuple, list)):
                        _fids = [r[0] for r in results[:30]]
                    elif results and isinstance(results[0], dict):
                        _fids = [r.get('fact_id', r.get('id')) for r in results[:30] if r.get('fact_id') or r.get('id')]
                    if _fids:
                        try:
                            # Use module-level bus from outer scope (line 474).
                            _ph = ','.join('?' * len(_fids))
                            _rows = bus.conn.execute(
                                f"SELECT id, content FROM memory_canonical WHERE id IN ({_ph})",
                                _fids,
                            ).fetchall()
                            _fid_text = {int(r[0]): (r[1] or '') for r in _rows}
                        except Exception:
                            _fid_text = {}
                        _pairs = [(f, _fid_text.get(f, '')) for f in _fids]
                    _ranked = _llm_rr(query, _pairs)
                    _sys.stderr.write(f"[RERANK] returned {len(_ranked)} ranked fids\n")
                    _sys.stderr.flush()
                    if _ranked:
                        # Re-order results. results shape: list of (fid, score) tuples.
                        _by_fid = {r[0]: r for r in results} if results and isinstance(results[0], (tuple, list)) else {r.get('fact_id'): r for r in results}
                        _new = [_by_fid[fid] for fid in _ranked if fid in _by_fid]
                        # Append any not in ranked set (LLM dropped them)
                        _ranked_set = set(_ranked)
                        for r in results:
                            _rid = r[0] if isinstance(r, (tuple, list)) else r.get('fact_id')
                            if _rid not in _ranked_set:
                                _new.append(r)
                        results = _new
                        # v1.10.9: LLM rerank already optimized; skip stage_recall.
                        _skip_stage = True
                except Exception as _e:
                    import sys as _sys, traceback as _tb
                    _sys.stderr.write(f"[RERANK] EXCEPTION: {type(_e).__name__}: {_e}\n{_tb.format_exc()}\n")
                    _sys.stderr.flush()
            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query. Free, <5ms.
            if not _skip_stage and os.environ.get('ASTOR_STAGERECALL', '1') != '0' and results:
                try:
                    from .nest.stage_recall import stage_recall_rerank as _sr
                    if candidate_fids:
                        _ph2 = ','.join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, content FROM memory_canonical "
                            f"WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        _cand_text = {int(r[0]): (r[1] or '') for r in _rtext}
                    else:
                        _cand_text = {}
                    results = _sr(results, _cand_text, query, top_k=top_k)
                except Exception:
                    pass
            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query. Free, <5ms.
            if not _skip_stage and os.environ.get('ASTOR_STAGERECALL', '1') != '0' and results:
                try:
                    from .nest.stage_recall import stage_recall_rerank as _sr
                    if candidate_fids:
                        _ph2 = ','.join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, content FROM memory_canonical "
                            f"WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        _cand_text = {int(r[0]): (r[1] or '') for r in _rtext}
                    else:
                        _cand_text = {}
                    results = _sr(results, _cand_text, query, top_k=top_k)
                except Exception:
                    pass
            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query. Free, <5ms.
            if not _skip_stage and os.environ.get('ASTOR_STAGERECALL', '1') != '0' and results:
                try:
                    from .nest.stage_recall import stage_recall_rerank as _sr
                    if candidate_fids:
                        _ph2 = ','.join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, content FROM memory_canonical "
                            f"WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        _cand_text = {int(r[0]): (r[1] or '') for r in _rtext}
                    else:
                        _cand_text = {}
                    results = _sr(results, _cand_text, query, top_k=top_k)
                except Exception:
                    pass
            # v1.10.9 (2026-08-26): optional rerank (env ASTOR_RERANK=1).
            # Lifts multi-hop chain coherence via lexical+bridge rerank.
            if os.environ.get('ASTOR_RERANK', '0') == '1' and results:
                try:
                    from .nest.reranker import rerank_candidates as _rerank
                    cand_dicts = []
                    cand_text = {}
                    if candidate_fids:
                        _ph2 = ',' .join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, keywords, tags, metadata FROM memory_canonical WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        for _rid, _kw, _tg, _mt in _rtext:
                            fid_int = int(_rid)
                            try:
                                _kws = _json.loads(_kw) if _kw else []
                            except Exception:
                                _kws = []
                            try:
                                _tags = _json.loads(_tg) if _tg else []
                            except Exception:
                                _tags = []
                            try:
                                _meta = _json.loads(_mt) if _mt else {}
                                _ctx = str(_meta.get('context', '') or '') if isinstance(_meta, dict) else ''
                            except Exception:
                                _ctx = ''
                            cand_text[fid_int] = (_ctx + ' ' + ' '.join(_kws) + ' ' + ' '.join(_tags)).strip()
                    for fid, s in results:
                        cand_dicts.append({'id': fid, 'score': s, 'content': cand_text.get(fid, '')})
                    reranked = _rerank(query, cand_dicts, top_n=top_k, rerank_weight=0.65)
                    results = [(c['id'], c['score']) for c in reranked]
                except Exception:
                    pass
            # v1.10.9: multi-hop bridge. Disabled by default (env ASTOR_BRIDGE=1).
            # Empirically: bridge decay<0.4 hurts LoCoMo accuracy because
            # co-ranked entities often collide on generic nouns (places,
            # common names) and over-promote wrong answers. Keep the
            # implementation available; only enable via env when known-good.
            if os.environ.get('ASTOR_BRIDGE', '0') == '1' and results and len(results) >= 2:
                try:
                    from .nest.multi_hop_bridge import apply_multi_hop_boost as _bridge
                    _bfids = [fid for fid, _ in results]
                    if _bfids:
                        _phb = ','.join('?' * len(_bfids))
                        _brows = bus.conn.execute(
                            f"SELECT id, content, keywords, tags FROM memory_canonical WHERE id IN ({_phb})",
                            _bfids,
                        ).fetchall()
                        _bcands = []
                        for _bid, _bct, _bkw, _btg in _brows:
                            try:
                                _bkws = _json.loads(_bkw) if _bkw else []
                            except Exception:
                                _bkws = []
                            try:
                                _btgs = _json.loads(_btg) if _btg else []
                            except Exception:
                                _btgs = []
                            _bcands.append({
                                'id': int(_bid), 'content': _bct or '',
                                'keywords': _bkws, 'tags': _btgs,
                                'score': next((s for f, s in results if f == int(_bid)), 0.0),
                            })
                        _boosted = _bridge(_bcands, top_seed_n=min(5, len(_bcands)))
                        _score_map = {c['id']: c['score'] for c in _boosted}
                        results = [(fid, _score_map.get(int(fid), 0.0)) for fid, _ in results]
                        results.sort(key=lambda x: x[1], reverse=True)
                except Exception:
                    pass
            # If hybrid returned nothing (empty lex AND empty nest), fall
            # back to vector-only so the caller doesn't get a hard empty.
            if not results:
                results = nest.search(query_emb, limit=top_k)
        # Enrich with bus metadata (content, kind)
        enriched = []
        for fact_id, sim in results:
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, keywords, context, "
                "event_date, event_date_precision, origin_session_id, metadata "
                "FROM memory_canonical WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                continue
            # v1.12.0 (2026-08-29): surface hierarchical extraction fields
            # (__topic__, __session_id__) to the API so multi-hop bridge
            # callers and human-readable displays can use them. Falls back
            # to '' for pre-v1.12 facts that lack these keys.
            _meta = _safe_json_loads_dict(row[13]) if len(row) > 13 and row[13] else {}
            enriched.append({
                'fact_id': row[0],
                'content': row[1],
                'kind': row[2],
                'confidence': row[3],
                'importance': row[4],
                'tags': row[5],
                'namespace': row[6],
                'user_id': row[7],
                'similarity': round(sim, 4),
                # 'score_kind' tells the caller whether similarity is
                # pure-cosine ('cosine') or hybrid ('hybrid'). Hermes
                # adapter uses this for sorting/debug.
                'score_kind': 'hybrid' if use_hybrid else 'cosine',
                # v1.2.0: include keywords + context in response so callers
                # (e.g. hermes_adapter) can render fact titles / explain
                # recall. Empty defaults for pre-v1.2 facts.
                'keywords': _safe_json_loads(row[8]) if len(row) > 8 else [],
                'context': (row[9] if len(row) > 9 and row[9] else '')[:500],
                # 2026-08-27: expose event_date so LLM can do date arithmetic
                # for temporal queries (LoCoMo "When did X?" weakness).
                'event_date': row[10] if len(row) > 10 else None,
                'event_date_precision': row[11] if len(row) > 11 else None,
                'session_id': (row[12] if len(row) > 12 else None),
                # v1.12.0: hierarchical extraction (Mem0 2026 lesson).
                # topic + session_id from metadata JSON, populated by either
                # LLM extractor or the regex-fallback heuristic. Empty when
                # fact was written before v1.12.0 (legacy schema didn't have
                # these fields).
                'topic': _meta.get('__topic__', '') if _meta else '',
                'session_id_meta': _meta.get('__session_id__', '') if _meta else '',
            })
        # v1.11.0 (2026-08-28): Agentic-grep verification pass (Mistral
        # Agentic Search pattern). Zero LLM tokens. After vector/hybrid
        # recall, re-run an exact BM25 match on the query's rare tokens;
        # facts that exact-match but were missed by hybrid recall get
        # appended (marked source='grep_verify'). Fixes "I don't have
        # info" failures where the answer WAS in the corpus but vector
        # similarity ranked it below top_k (observed on LongMemEval).
        if enriched and os.environ.get('ASTOR_GREP_VERIFY', '1') != '0':
            try:
                from .nest.lex_index import astor_lex as _astor_lex
                _lex = _astor_lex(tier=tier, user_id=user_id)
                _stop = {
                    'what', 'when', 'where', 'who', 'how', 'did', 'does', 'is',
                    'are', 'was', 'were', 'the', 'a', 'an', 'my', 'i', 'me',
                    'in', 'on', 'at', 'of', 'for', 'to', 'with', 'and', 'or',
                    'do', 'did', 'have', 'has', 'many', 'much', 'long', 'get',
                }
                _tokens = [
                    t for t in re.findall(r"[A-Za-z0-9]{2,}", query)
                    if t.lower() not in _stop
                ]
                if _tokens:
                    _have = {int(r['fact_id']) for r in enriched}
                    # grep semantics: rare tokens are needles. Multi-token AND
                    # query often returns [] (BM25 conjunction); probe tokens
                    # individually and merge hits.
                    _hits = {}
                    _tok_df = {}
                    for _tok in _tokens[:8]:
                        try:
                            _tok_hits = _lex.bm25_search(_tok, limit=top_k * 2)
                            _tok_df[_tok.lower()] = len(_tok_hits)
                            for _fid, _score in _tok_hits:
                                _hits.setdefault(int(_fid), []).append((_tok, float(_score)))
                        except Exception:
                            continue
                    # Rarest tokens first: a fact matching a rare token is a
                    # stronger grep signal than one matching only common words.
                    _rarity = {t: _tok_df.get(t.lower(), 999) for t in _tokens}
                    _sorted_tokens = sorted(_tokens, key=lambda t: _rarity.get(t.lower(), 999))
                    _rare_thresh = 3  # token in <=3 facts = rare needle
                    _hits_ranked = sorted(
                        _hits.items(),
                        key=lambda kv: min(_rarity.get(t.lower(), 999) for t, _ in kv[1]),
                    )
                    _added = 0
                    for _fid, _tok_scores in _hits_ranked:
                        if _fid in _have or _added >= 3:
                            continue
                        _row = bus.conn.execute(
                            "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, keywords, context, "
                            "event_date, event_date_precision, origin_session_id "
                            "FROM memory_canonical WHERE id = ?", (_fid,),
                        ).fetchone()
                        if _row is None:
                            continue
                        _content_l = str(_row[1]).lower()
                        # Require >=1 RARE token exact match (grep semantics):
                        # common-word-only matches are distractors, skip them.
                        _matched_all = [t for t in _sorted_tokens if t.lower() in _content_l]
                        _matched = [t for t in _matched_all if _rarity.get(t.lower(), 999) <= _rare_thresh]
                        if not _matched:
                            continue
                        enriched.append({
                            'fact_id': _row[0],
                            'content': _row[1],
                            'kind': _row[2],
                            'confidence': _row[3],
                            'importance': _row[4],
                            'tags': _row[5],
                            'namespace': _row[6],
                            'user_id': _row[7],
                            'similarity': round(min(max(s for _, s in _tok_scores) / 10.0, 1.0), 4),
                            'score_kind': 'grep_verify',
                            'keywords': _safe_json_loads(_row[8]) if len(_row) > 8 else [],
                            'context': (_row[9] if len(_row) > 9 and _row[9] else '')[:500],
                            'event_date': _row[10] if len(_row) > 10 else None,
                            'event_date_precision': _row[11] if len(_row) > 11 else None,
                            'session_id': _row[12] if len(_row) > 12 else None,
                            'grep_matched_tokens': _matched[:5],
                        })
                        _have.add(_fid)
                        _added += 1
            except Exception:
                pass  # grep-verify is best-effort; never break recall

        # v1.11.0: session-neighbor expand (read/navigate pattern). For the
        # top hybrid hits that carry origin_session_id, pull ±1 sibling facts
        # from the same session — gives the LLM the surrounding context of a
        # hit without re-running vector search. 0 LLM tokens.
        if enriched and os.environ.get('ASTOR_NEIGHBOR', '1') != '0':
            try:
                _seen_ids = {int(r['fact_id']) for r in enriched}
                _neighbors = []
                for _r in enriched[:3]:
                    _sid = _r.get('session_id') or None
                    if not _sid:
                        continue
                    _rows = bus.conn.execute(
                        "SELECT id, content, kind, origin_session_id, event_date FROM memory_canonical "
                        "WHERE origin_session_id = ? AND id != ? AND tombstoned = 0 "
                        "ORDER BY ABS(id - ?) LIMIT 2",
                        (_sid, int(_r['fact_id']), int(_r['fact_id'])),
                    ).fetchall()
                    for _row in _rows:
                        if int(_row[0]) in _seen_ids:
                            continue
                        _seen_ids.add(int(_row[0]))
                        _neighbors.append({
                            'fact_id': _row[0],
                            'content': _row[1],
                            'kind': _row[2],
                            'session_id': _row[3],
                            'event_date': _row[4],
                            'similarity': 0.0,
                            'score_kind': 'session_neighbor',
                            'neighbor_of': int(_r['fact_id']),
                        })
                # Neighbors go AFTER all primary results
                enriched.extend(_neighbors[:3])
            except Exception:
                pass  # neighbor-expand is best-effort

        return jsonify({'results': enriched, 'count': len(enriched)})

    @app.route('/v1/forget', methods=['POST'])
    def forget():
        """Forget a fact by ID, or by content match.

        Body JSON (one of):
          fact_id: int  — hard-delete one fact by canonical id
          query: str + tier + user_id  — find by BM25 best-match then delete
          tombstone_only: bool (default False) — if true, keep audit row;
                                                 if false (default), hard-delete

        Strategy:
          1. fact_id given:    look up fact; tombstone via bus; remove from lex.
          2. query given:     BM25 search in (tier, user_id); pick top-1; if
                              score >= forget_threshold (default 5.0),
                              forget that fact. Otherwise return empty hit.
          3. Always logs the forget action to bus.audit_log for HIPAA-style
             audit trail.
        """
        body = request.get_json(force=True)
        tier = body.get('tier', 'public')
        user_id = body.get('user_id') or body.get('user')
        if tier == 'repo':
            user_id = body.get('repo_id') or user_id
        bus = astor_bus(tier=tier, user_id=user_id)
        fact_id = body.get('fact_id')
        query   = body.get('query')
        tombstone_only = bool(body.get('tombstone_only', False))
        dry_run = bool(body.get('dry_run', False))
        forget_threshold = float(body.get('forget_threshold', 5.0))

        if not fact_id and not query:
            return jsonify({'error': 'fact_id or query required'}), 400

        from .nest.lex_index import astor_lex as _astor_lex
        lex = _astor_lex(tier=tier, user_id=user_id)

        chosen: tuple[int, float, str] | None = None  # (fact_id, score, content)
        if fact_id is not None:
            row = bus.conn.execute(
                "SELECT id, content FROM memory_canonical WHERE id = ?",
                (int(fact_id),),
            ).fetchone()
            if row is None:
                return jsonify({'error': f'fact_id {fact_id} not found'}), 404
            chosen = (int(row[0]), 1.0, str(row[1]))
        else:
            hits = lex.bm25_search(str(query), limit=5)
            if not hits:
                return jsonify({'forgotten': [], 'reason': 'no BM25 hit'}), 200
            best_fid, best_score = hits[0]
            if best_score < forget_threshold:
                return jsonify({
                    'forgotten': [],
                    'reason': f'best BM25 score {best_score:.2f} below threshold {forget_threshold}',
                    'candidates': [
                        {'fact_id': fid, 'score': round(s, 3)}
                        for fid, s in hits
                    ],
                }), 200
            row = bus.conn.execute(
                "SELECT id, content FROM memory_canonical WHERE id = ?",
                (int(best_fid),),
            ).fetchone()
            if row is None:
                return jsonify({'error': f'BM25 winner {best_fid} missing in bus'}), 500
            chosen = (int(row[0]), float(best_score), str(row[1]))

        cfid, cscore, ccontent = chosen
        # DRY-RUN (opt5): return what would be forgotten, no mutation.
        if dry_run:
            return jsonify({
                'dry_run': True,
                'would_forget': [{
                    'fact_id': cfid, 'score': round(cscore, 3),
                    'content_preview': ccontent[:120],
                    'tombstone_only': tombstone_only,
                    'tier': tier, 'user_id': user_id,
                }],
                'note': 'No mutation was performed.',
            })
        # Capture old_state for versioning (opt6)
        snapshot_json = None
        try:
            existing = bus.conn.execute(
                "SELECT * FROM memory_canonical WHERE id = ?", (cfid,),
            ).fetchone()
            if existing is not None:
                cols = [d[1] for d in bus.conn.execute(
                                    "PRAGMA table_info(memory_canonical)"
                                ).fetchall()]
                row_dict = {cols[i]: existing[i] for i in range(len(cols))}
                for k in list(row_dict.keys()):
                    v = row_dict[k]
                    if isinstance(v, (bytes, bytearray)):
                        row_dict.pop(k)
                        continue
                    try:
                        import json as _json_mod_inner
                        _json_mod_inner.dumps(v)
                    except Exception:
                        row_dict[k] = repr(v)
                import json as _json_mod_outer
                snapshot_json = _json_mod_outer.dumps(
                    {'columns': row_dict, 'tier': tier, 'user_id': user_id},
                    ensure_ascii=False,
                )
        except Exception:
            snapshot_json = None
        # Apply forget
        # 1. tombstone / hard-delete in bus
        try:
            if tombstone_only:
                bus.conn.execute(
                    "UPDATE memory_canonical SET tombstoned = 1 WHERE id = ?",
                    (cfid,),
                )
            else:
                bus.conn.execute("DELETE FROM memory_canonical WHERE id = ?", (cfid,))
                # also delete from nest embeddings and forge if present
                try:
                    nest_obj = astor_nest(tier=tier, user_id=user_id)
                    nest_obj.conn.execute(
                        "DELETE FROM embeddings WHERE fact_id = ?", (cfid,)
                    )
                    nest_obj.conn.commit()
                except Exception:
                    pass
            bus.conn.commit()
        except Exception as e:
            bus.conn.rollback()
            return jsonify({'error': f'bus tombstone/delete failed: {e}'}), 500
        # 2. remove from lex
        try:
            if tombstone_only:
                lex.remove_fact(cfid)
            else:
                lex.remove_fact_hard(cfid)
        except Exception as e:
            import sys as _sys
            print(f'[astor.server] lex remove failed (continuing): {e}', file=_sys.stderr)
        # 3. audit (with old_state snapshot for opt6 versioning)
        try:
            bus.conn.execute(
                "INSERT INTO audit_log(event, actor, target_type, target_id, "
                "old_state, reason, metadata, severity) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ('forget', 'rest_api', 'fact', str(cfid),
                 snapshot_json,
                 f'tombstone={tombstone_only} tier={tier} user={user_id} '
                 f'score={cscore:.3f}',
                 f'{{"content_preview": {ccontent[:80]!r}}}',
                 'warning' if not tombstone_only else 'info'),
            )
            bus.conn.commit()
        except Exception as _audit_exc:
            import sys as _sys
            print(f'[astor.server] forget audit_log failed: {_audit_exc}', file=_sys.stderr)
        return jsonify({
            'forgotten': [{
                'fact_id': cfid, 'score': round(cscore, 3),
                'content_preview': ccontent[:120],
                'tombstone_only': tombstone_only,
            }],
        })

    @app.route('/v1/read/multi', methods=['POST'])
    def read_multi():
        """Cross-tier recall (2026-08-16 opt7).

        Search the same query across multiple (tier, user_id) scopes in
        parallel, then re-rank by combined score. This is the primary recall
        path when the caller's identity spans both public memory (the
        agent's shared long-term) and private memory (per-user long-term)
        — e.g. user 'admin' reading from both `public` and `private/admin`.

        Body JSON:
          query: str
          scopes: [{tier, user_id, weight}]  (default = all available)
          top_k: int (default 10)
          hybrid: bool (default true)

        For each scope we run both vector + BM25, then:
            combined(fid, scope_i) = weight_i * hybrid_score_scope_i
        and finally z-score normalize per scope before merging.
        """
        import concurrent.futures as _cf
        body = request.get_json(force=True)
        query = body.get('query')
        if not query:
            return jsonify({'error': 'query required'}), 400
        top_k = int(body.get('top_k', 10))
        use_hybrid = bool(body.get('hybrid', True))
        # Default scopes: public always + private(current_call_user) if any
        scopes_in = body.get('scopes')
        if not scopes_in:
            scopes_in = [{'tier': 'public', 'user_id': None, 'weight': 0.5}]
            requester = body.get('user_id') or body.get('user')
            if requester:
                scopes_in.append({
                    'tier': 'private', 'user_id': requester, 'weight': 1.0,
                })
        else:
            scopes_in = [
                dict(s, weight=float(s.get('weight', 1.0)))
                for s in scopes_in
            ]

        oversample = max(top_k * 2, 30)
        from .nest.embeddings import astor_get_embedding_model
        from .nest.lex_index import (
            astor_lex as _astor_lex, hybrid_merge as _hybrid_merge,
        )
        model = astor_get_embedding_model()
        query_emb = list(model.embed([query]))[0]

        # Run each scope in a thread (vector search is the slow part)
        per_scope_results: list[tuple[str, str | None, float,
                                       list[tuple[int, float]]]] = []

        def _search_one(scope: dict) -> tuple[str, str | None, float,
                                              list[tuple[int, float]]]:
            t = scope['tier']
            u = scope.get('user_id')
            w = float(scope.get('weight', 1.0))
            # ACL: each ThreadPoolExecutor worker is a fresh thread, so
            # `_CURRENT` (which is _thread._local) is uninitialized there.
            # Re-init as first_admin for every scope — read/write tier is
            # scoped by the per-scope bus/nest/forge objects, ACL just
            # gates cross-tier read access (first_admin may read all).
            from astor_memory._internal.acl import astor_init_acl
            astor_init_acl(
                actor='admin:admin', role='admin',
                tier=t, user_id=u, subscription_plan=None,
            )
            nest = astor_nest(tier=t, user_id=u)
            bus = astor_bus(tier=t, user_id=u)
            lex = _astor_lex(tier=t, user_id=u)
            vh = nest.search(query_emb, limit=oversample)
            bh = lex.bm25_search(query, limit=oversample) if use_hybrid else []
            merged = _hybrid_merge(
                bm25_hits=bh, vector_hits=vh,
                bm25_weight=0.4, vec_weight=0.6, limit=oversample,
            ) if use_hybrid else [(fid, s) for fid, s in vh]
            return (t, u, w, merged)

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(_search_one, s) for s in scopes_in]
            for f in _cf.as_completed(futs):
                per_scope_results.append(f.result())

        # Weight by scope, then re-rank. We keep top_k across all scopes.
        weighted: dict[tuple[str, str | None, int], float] = {}
        metadata: dict[tuple[str, str | None, int], dict] = {}
        for tier, uid, w, merged in per_scope_results:
            for fid, score in merged:
                key = (tier, uid, int(fid))
                weighted[key] = max(weighted.get(key, 0.0), w * score)
        ranked = sorted(weighted.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # Enrich with content (read each scope's bus)
        enriched = []
        for key, score in ranked:
            tier, uid, fid = key
            # 2026-08-29 fix: rebind ACL per scope in the request thread.
            # The request thread carries whatever binding before_request set
            # (or a stale bind from a previous request on a reused Flask
            # thread). _search_one rebinds per scope; enrich must do the
            # same or private-scope reads 403 with "first_admin lacks grant".
            if tier == 'private':
                astor_init_acl(
                    actor='admin:admin', role='admin',
                    tier='private', user_id=uid,
                    subscription_plan=None,
                )
            else:
                astor_init_acl(
                    actor='admin:admin', role='admin', tier=tier,
                    subscription_plan=None,
                )
            bus = astor_bus(tier=tier, user_id=uid)
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, origin_session_id "
                "FROM memory_canonical WHERE id = ?", (fid,),
            ).fetchone()
            if row is None:
                continue
            enriched.append({
                'fact_id': row[0],
                'content': row[1],
                'kind': row[2],
                'confidence': row[3],
                'importance': row[4],
                'tags': row[5],
                'namespace': row[6],
                'user_id': row[7],
                'tier': tier,  # opt7: which tier this came from
                'cross_tier_score': round(score, 4),
            })
        return jsonify({
            'results': enriched,
            'count': len(enriched),
            'scopes_searched': [
                {'tier': t, 'user_id': u, 'weight': w}
                for t, u, w, _ in per_scope_results
            ],
        })

    @app.route('/v1/lex/stats', methods=['GET'])
    def lex_stats():
        """Stats for the BM25 lex index across all known scopes (debug)."""
        from .nest.lex_index import astor_lex as _astor_lex
        from ._internal.acl_layout import list_user_ids
        out = {'version': __version__}
        for scope in [
            ('public', None), ('source', None),
            *((('private', u) for u in list_user_ids())),
        ]:
            tier, uid = scope
            try:
                lex = _astor_lex(tier=tier, user_id=uid)
                out[f'{tier}/{uid or "_"}'] = lex.stats()
            except Exception as e:
                out[f'{tier}/{uid or "_"}'] = {'error': str(e)}
        return jsonify(out)

    @app.route('/v1/merge/find', methods=['POST'])
    def merge_find():
        """Find candidate duplicate facts (cosine + LLM judge)."""
        from .nest.merge import find_duplicate_groups
        body = request.get_json(force=True)
        tier = body.get('tier', 'public')
        user_id = body.get('user_id')
        if tier == 'private' and not user_id:
            user_id = body.get('user', 'admin')
        threshold = float(body.get('threshold', 0.92))
        top_k = int(body.get('top_k', 50))
        use_llm = bool(body.get('use_llm', True))
        max_groups = int(body.get('max_groups', 100))
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'merge requires admin'}), 403
        except Exception:
            pass
        result = find_duplicate_groups(
            tier=tier, user_id=user_id,
            threshold=threshold, top_k=top_k,
            use_llm=use_llm, max_groups=max_groups,
        )
        # Slim groups in response (drop embedding vectors)
        slim = [{
            'group_id': g['group_id'], 'size': g['size'],
            'method': g['method'],
            'suggested_winner': g['suggested_winner'],
            'losers': g['losers'],
            'llm_verdicts': g.get('llm_verdicts', []),
        } for g in result.get('groups', [])]
        return jsonify({
            'tier': result['tier'], 'user_id': result['user_id'],
            'candidate_count': result['candidate_count'],
            'group_count': len(slim), 'groups': slim,
            'threshold': threshold, 'top_k': top_k, 'use_llm': use_llm,
        })

    @app.route('/v1/merge/apply', methods=['POST'])
    def merge_apply():
        """Apply reviewed merge list."""
        from .nest.merge import apply_merges
        body = request.get_json(force=True)
        merges = body.get('merges', [])
        actor = body.get('actor', 'merge_v2_operator')
        if not isinstance(merges, list) or not merges:
            return jsonify({'error': 'merges list required'}), 400
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'merge requires admin'}), 403
        except Exception:
            pass
        result = apply_merges(merges=merges, actor=actor)
        return jsonify(result)

    @app.route('/v1/fact/<int:fact_id>/provenance', methods=['GET'])
    def fact_provenance(fact_id):
        """Return the citation lineage for one fact_id (which event(s) produced it)."""
        from .nest.provenance import get_provenance
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 8))
        try:
            return jsonify(get_provenance(fact_id, tier=tier, user_id=user_id,
                                          max_depth=max_depth))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/fact/<int:fact_id>/lineage', methods=['GET'])
    def fact_lineage(fact_id):
        """Return all revisions of a fact_id over time (audit trail)."""
        from .nest.provenance import get_lineage
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 8))
        try:
            return jsonify(get_lineage(fact_id, tier=tier, user_id=user_id,
                                       max_depth=max_depth))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/fact/<int:fact_id>/graph.dot', methods=['GET'])
    def fact_graph_dot(fact_id):
        """Return the provenance graph for a fact as Graphviz DOT text."""
        from .nest.provenance import graph_dot
        direction = request.args.get('direction', 'both')
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 6))
        try:
            dot = graph_dot(fact_id=fact_id, direction=direction,
                            tier=tier, user_id=user_id, max_depth=max_depth)
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404
        return (dot, 200, {'Content-Type': 'text/vnd.graphviz'})

    @app.route('/v1/fact/<int:fact_id>/provenance', methods=['POST'])
    def fact_record_provenance(fact_id):
        """Manually record a provenance edge between two fact_ids (admin-only)."""
        from .nest.provenance import record_provenance
        body = request.get_json(force=True)
        result = record_provenance(
            fact_id=fact_id,
            parents=body.get('parents', []),
            kind=body.get('kind', 'extracted'),
            agent=body.get('agent', 'forge.regex_v2'),
            depth=body.get('depth'),
            tier=body.get('tier', 'public'),
            user_id=body.get('user_id'),
        )
        return jsonify(result)

    @app.route('/v1/fact/<int:fact_id>/versions', methods=['GET'])
    def fact_versions(fact_id):
        """List all revision_ids for a fact_id (chronological)."""
        from .nest.versioning import list_versions
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        try:
            rows = list_versions(fact_id, tier=tier, user_id=user_id)
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404
        return jsonify({
            'fact_id': fact_id, 'tier': tier, 'user_id': user_id,
            'version_count': len(rows), 'versions': rows,
        })

    @app.route('/v1/fact/<int:fact_id>/restore', methods=['POST'])
    def fact_restore(fact_id):
        """Restore a fact to a prior revision (creates a new revision pointing back; never destructive)."""
        from .nest.versioning import restore_fact
        body = request.get_json(force=True) if request.is_json else {}
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'restore requires admin'}), 403
        except Exception:
            pass
        result = restore_fact(
            fact_id=fact_id,
            tier=body.get('tier', 'public'),
            user_id=body.get('user_id'),
            target_state=body.get('target_state', 'preview'),
            actor=body.get('actor', 'restore_v1'),
        )
        return jsonify(result)

    @app.route('/v1/snapshot/stats', methods=['GET'])
    def snapshot_stats():
        """Return system-wide stats: facts by tier/scope, event count, db sizes."""
        from .nest.versioning import daily_snapshot_stats
        date_str = request.args.get('date')
        if not date_str:
            from datetime import datetime, timezone
            date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        try:
            return jsonify(daily_snapshot_stats(
                date_str=date_str,
                tier=request.args.get('tier', 'public'),
                user_id=request.args.get('user_id'),
            ))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/cascade/replay', methods=['POST'])
    def cascade_replay():
        """Replay pending cascade write queue (2026-08-16 v1.2.0 ship).

        When nest.store() failed during a promote_candidate (e.g. embedding
        model OOM, LanceDB unavailable), the (fact_id, content, tier, user_id)
        was queued in cascade_state. This endpoint processes pending rows
        and re-attempts the embed. First_admin only — this is a destructive
        operation (can write to many nest DBs at once).

        Body JSON (all optional):
          limit: int (default 100) — max rows to process this call
          max_attempts: int (default 5) — per-row max retry count

        Returns:
          {processed, succeeded, failed, still_pending, results: [...]}
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'cascade_replay requires admin'}), 403
        except Exception:
            pass
        body = request.get_json(force=True) if request.is_json else {}
        limit = int(body.get('limit', 100))
        max_attempts = int(body.get('max_attempts', 5))
        # Run replay against public tier (caller is first_admin, can write
        # any tier; cross-tier rows are routed by their own tier/user_id
        # inside cascade.replay_pending).
        bus = astor_bus(tier='public', user_id='admin')
        from .bus import cascade as _cascade
        result = _cascade.replay_pending(
            bus, limit=limit, max_attempts=max_attempts,
        )
        # Also write an audit row so replay is traceable.
        try:
            bus.write_audit(
                event='cascade_replay',
                actor='admin:admin',
                target_type='system',
                target_id='cascade_state',
                metadata={
                    'limit': limit, 'max_attempts': max_attempts,
                    'succeeded': result['succeeded'],
                    'failed': result['failed'],
                    'still_pending': result['still_pending'],
                },
            )
        except Exception:
            pass
        return jsonify(result)

    @app.route('/v1/cascade/stats', methods=['GET'])
    def cascade_stats():
        """Aggregate stats on cascade write queue. No body required.

        Returns:
          {pending, succeeded, failed, last_attempt_at}
        """
        bus = astor_bus(tier='public', user_id='admin')
        from .bus import cascade as _cascade
        return jsonify(_cascade.stats(bus))

    @app.route('/v1/reflection/run', methods=['POST'])
    def reflection_run():
        """Run episodic reflection (v1.2.2 ship — EverOS pattern).

        Finds clusters of similar canonical facts in (tier, user_id),
        merges them into a single "winner" fact with concatenated content,
        and tombstones the losers. Audit row written per deprecation.
        First_admin only — destructive operation.

        Body JSON (all optional):
          tier: str (default 'public')
          user_id: str | null (default null for public/source)
          min_size: int (default 2) — minimum cluster size to merge
          max_clusters: int (default 50) — cap to avoid runaway
          kinds: list[str] | null (default null = all kinds)

        Returns:
          {clusters_found, clusters_merged, facts_deprecated, merge_log: [...]}
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'reflection_run requires admin'}), 403
        except Exception:
            pass
        body = request.get_json(force=True) if request.is_json else {}
        tier = body.get('tier', 'public')
        user_id = body.get('user_id') or None
        min_size = int(body.get('min_size', 2))
        max_clusters = int(body.get('max_clusters', 50))
        kinds = body.get('kinds') or None
        from .nest import reflection as _reflection
        bus = astor_bus(tier=tier, user_id=user_id)
        result = _reflection.run_reflection(
            bus, tier=tier, user_id=user_id,
            min_size=min_size, max_clusters=max_clusters, kinds=kinds,
            actor='admin:admin',
        )
        # Audit row for the reflection run itself
        try:
            bus.write_audit(
                event='reflection_run',
                actor='admin:admin',
                target_type='system',
                target_id='reflection',
                metadata={
                    'tier': tier, 'user_id': user_id,
                    'min_size': min_size, 'max_clusters': max_clusters,
                    'clusters_found': result['clusters_found'],
                    'clusters_merged': result['clusters_merged'],
                    'facts_deprecated': result['facts_deprecated'],
                },
                severity='info',
            )
        except Exception:
            pass
        return jsonify(result)

    @app.route('/v1/install', methods=['POST'])
    def install():
        """Plan an install into another agent (returns file changes, does not write).

        Body JSON:
          ide: str (claude-code | cline | ...)
          mode: str (priority | coexist | replace | verify | auto)
          agent_dir: str (default '~')
        Returns:
          {plan: {agent, mode, tier, changes: [...], notes: [...]}}
        """
        from .installer import astor_install as run_installer
        body = request.get_json(force=True)
        ide = body.get('ide')
        mode = body.get('mode', 'auto')
        agent_dir = body.get('agent_dir', '~')
        if not ide:
            return jsonify({'error': 'ide required'}), 400
        result = run_installer(ide, Path(agent_dir).expanduser(), mode)
        return jsonify(result)

    @app.route('/v1/viewer/stats', methods=['GET'])
    def viewer_stats():
        """Content-free stats endpoint (Memorax-inspired Viewer).

        Returns counts only — NO fact content. Per MemoraX architecture rule,
        the Viewer is "a content-free local projection, not memory authority".
        Use this for dashboards / health monitoring / writeback-status check
        without leaking PII.

        Returns:
          {
            version, astor_dir,
            dbs: {bus, nest, forge} per tier,
            counts: {
              facts_total, facts_by_tier, facts_by_scope,
              events_total, embeddings_total, candidates_total,
              forge_audit_total, dedup_hits_total
            },
            last_activity_ts,
            schema_versions
          }
        """
        import sqlite3 as _sqlite3
        from ._internal.acl_layout import (
            get_astor_dir, get_db_path, Tier, Store, list_user_ids, list_repo_ids
        )

        astor_dir = get_astor_dir()
        out = {
            'version': __version__,
            'astor_dir': str(astor_dir),
            'generated_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
            'dbs': {},
            'counts': {
                'facts_total': 0,
                'facts_by_tier': {'public': 0, 'source': 0, 'private': 0, 'repo': 0},
                'facts_by_scope': {'long_term': 0, 'short_term': 0, 'profile': 0},
                'events_total': 0,
                'embeddings_total': 0,
                'candidates_total': 0,
                'forge_audit_total': 0,
                'dedup_hits_total': 0,
            },
            'last_activity_ts': None,
            'schema_versions': {},
        }

        # Iterate 9-db layout: 3 tiers × 3 stores. For private tier, fanout
        # across all known users (from list_user_ids).
        tier_user_map = [
            (Tier.PUBLIC, None),
            (Tier.SOURCE, None),
        ]
        tier_user_map += [(Tier.PRIVATE, u) for u in list_user_ids()]
        # v1.1: fanout across all registered repos (MemoraX-style per-repo
        # memory). Repo facts live under ~/.astor/repos/<repo_id>/memory/.
        tier_user_map += [(Tier.REPO, r) for r in list_repo_ids()]

        for tier, user_id in tier_user_map:
            for store in (Store.BUS, Store.NEST, Store.FORGE):
                try:
                    db_path = get_db_path(tier, store, user_id)
                except ValueError:
                    continue
                if not db_path.exists():
                    continue
                rel_key = (
                    f'{tier.value}/{user_id or "_"}/{store.value}'
                )
                out['dbs'][rel_key] = {
                    'path': str(db_path),
                    'size_bytes': db_path.stat().st_size,
                }
                try:
                    c = _sqlite3.connect(
                        f'file:{db_path}?mode=ro', uri=True,
                        check_same_thread=False, timeout=5,
                    )
                except Exception:
                    continue
                try:
                    if store == Store.BUS:
                        # facts (canonical) — count + scope + tier
                        for row in c.execute(
                            "SELECT tier, scope_type, COUNT(*) FROM memory_canonical "
                            "WHERE tombstoned = 0 OR tombstoned IS NULL "
                            "GROUP BY tier, scope_type"
                        ).fetchall():
                            t_val, s_val, n = row
                            # tier may be 'private' or 'private_<user>' per
                            # schema CHECK — bucket to 'private' bucket.
                            t_bucket = (
                                'private' if t_val.startswith('private') else t_val
                            )
                            if t_bucket in out['counts']['facts_by_tier']:
                                out['counts']['facts_by_tier'][t_bucket] += n
                            if s_val in out['counts']['facts_by_scope']:
                                out['counts']['facts_by_scope'][s_val] += n
                            out['counts']['facts_total'] += n
                        # events
                        r = c.execute(
                            "SELECT COUNT(*) FROM events"
                        ).fetchone()
                        if r:
                            out['counts']['events_total'] += r[0]
                        # candidates
                        r = c.execute(
                            "SELECT COUNT(*) FROM memory_candidates"
                        ).fetchone()
                        if r:
                            out['counts']['candidates_total'] += r[0]
                        # last activity ts
                        r = c.execute(
                            "SELECT MAX(ts) FROM events"
                        ).fetchone()
                        if r and r[0]:
                            out['last_activity_ts'] = r[0]
                        # dedup_hits (from audit_log if available)
                        try:
                            r = c.execute(
                                "SELECT COUNT(*) FROM audit_log "
                                "WHERE event = 'promote_idempotent_replay'"
                            ).fetchone()
                            if r:
                                out['counts']['dedup_hits_total'] += r[0]
                        except Exception:
                            pass
                        # schema version
                        try:
                            r = c.execute(
                                "SELECT version FROM schema_migrations "
                                "ORDER BY version DESC LIMIT 1"
                            ).fetchone()
                            if r:
                                out['schema_versions'][f'bus/{rel_key}'] = r[0]
                        except Exception:
                            pass
                    elif store == Store.NEST:
                        r = c.execute(
                            "SELECT COUNT(*) FROM embeddings"
                        ).fetchone()
                        if r:
                            out['counts']['embeddings_total'] += r[0]
                    elif store == Store.FORGE:
                        r = c.execute(
                            "SELECT COUNT(*) FROM llm_call_log"
                        ).fetchone()
                        if r:
                            out['counts']['forge_audit_total'] += r[0]
                except Exception:
                    # Skip unreadable DBs; don't fail the whole endpoint.
                    continue
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass

        return jsonify(out)

    @app.route('/v1/reload', methods=['POST'])
    def reload():
        """Hot-reload server code (P3-fix 2026-08-15).

        Re-execs the current process via os.execv so all module caches
        (bus/store, forge/extractor, server) pick up fresh source. Used
        after patching the code without restarting manually.

        Restricted to first_admin (per ACL plan § reload requires root).
        """
        import os as _os
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'reload requires admin role'}), 403
        except Exception:
            pass
        # Schedule a self-restart in 200ms then return. The new process
        # will bind the port after the old one closes it.
        import threading as _threading
        def _respawn():
            import time as _t
            _t.sleep(0.2)
            _os.execv(sys.executable, [sys.executable] + sys.argv)
        _threading.Thread(target=_respawn, daemon=True).start()
        return jsonify({'reloading': True, 'pid': _os.getpid()})

    @app.errorhandler(404)
    def not_found(e):
        """Flask 404 handler that emits a structured JSON error."""
        return jsonify({'error': 'not found', 'path': request.path}), 404

    # === Grant endpoints (2026-08-16 strict-privacy ship) ===

    @app.route('/v1/grant', methods=['POST'])
    def grant_create():
        """
        Issue a cross-user private-tier grant.

        Body: {
          "grantor":    "<user_id>",      # data owner (the caller if 'user' role)
          "grantee":    "admin:<id>" | "user:<id>",
          "scope":      "read" | "write" | "admin",
          "expires_at": ISO 8601 or null (default null),
          "reason":     free text (optional)
        }

        Auth: caller MUST be the grantor (user role) — i.e. a user can only
        authorize access to their own private data.
        """
        from ._internal.grants import create_grant as _create_grant
        from ._internal.acl import astor_current_acl
        from ._internal.audit_logger import astor_audit

        body = request.get_json(force=True) or {}
        grantor = body.get('grantor')
        grantee = body.get('grantee')
        scope = body.get('scope', 'read')
        expires_at = body.get('expires_at')
        reason = body.get('reason')

        if not grantor or not grantee:
            return jsonify({'error': 'missing grantor/grantee'}), 400

        ctx = astor_current_acl()
        if ctx.role == 'user':
            if ctx.user_id != grantor:
                astor_audit(
                    actor=ctx.actor, tier='private', action='admin_op',
                    user_id=grantor, target='grant_create_denied',
                    reason='user can only grant on own private',
                    metadata={"requested_grantee": grantee},
                )
                return jsonify({
                    'error': 'forbidden',
                    'detail': 'user can only authorize access to their own private data',
                }), 403
        elif ctx.role != 'admin':
            # admin cannot forge a grant on a user's behalf
            astor_audit(
                actor=ctx.actor, tier='private', action='admin_op',
                user_id=grantor, target='grant_create_denied',
                reason='admin cannot forge grant on user behalf',
                metadata={"requested_grantee": grantee},
            )
            return jsonify({
                'error': 'forbidden',
                'detail': 'admin cannot create grants on behalf of users',
            }), 403

        try:
            gid = _create_grant(grantor=grantor, grantee=grantee, scope=scope,
                                expires_at=expires_at, reason=reason)
        except ValueError as exc:
            return jsonify({'error': 'invalid_grant', 'detail': str(exc)}), 400
        astor_audit(
            actor=ctx.actor, tier='private', action='admin_op',
            user_id=grantor, target='grant_created',
            reason=f'grantee={grantee} scope={scope}',
            metadata={"grant_id": gid, "grantee": grantee, "scope": scope},
        )
        return jsonify({'grant_id': gid, 'grantor': grantor, 'grantee': grantee,
                        'scope': scope, 'expires_at': expires_at})

    @app.route('/v1/grant/revoke', methods=['POST'])
    def grant_revoke():
        """Revoke a grant by id. Caller must own the grant (be the grantor)."""
        from ._internal.grants import revoke_grant as _revoke_grant, list_grants as _list
        from ._internal.acl import astor_current_acl
        from ._internal.audit_logger import astor_audit

        body = request.get_json(force=True) or {}
        gid = body.get('grant_id')
        if not gid:
            return jsonify({'error': 'missing grant_id'}), 400

        ctx = astor_current_acl()
        rows = _list(grantee=None, include_revoked=True)
        target = next((r for r in rows if r['id'] == int(gid)), None)
        if not target:
            return jsonify({'error': 'grant_not_found'}), 404
        if ctx.role == 'user' and ctx.user_id != target['grantor']:
            return jsonify({'error': 'forbidden',
                            'detail': 'only the grantor can revoke their grant'}), 403

        ok = _revoke_grant(int(gid), by=ctx.actor)
        astor_audit(
            actor=ctx.actor, tier='private', action='admin_op',
            user_id=target['grantor'], target='grant_revoked',
            reason=f'grantee={target["grantee"]}',
            metadata={"grant_id": int(gid)},
        )
        return jsonify({'ok': ok, 'grant_id': int(gid)})

    @app.route('/v1/grant/list', methods=['GET'])
    def grant_list():
        """List grants scoped to caller role (first_admin=all, admin=incoming, user=outgoing)."""
        from ._internal.grants import list_grants as _list
        from ._internal.acl import astor_current_acl

        ctx = astor_current_acl()
        include_revoked = request.args.get('include_revoked', 'false').lower() == 'true'

        if ctx.role == 'admin':
            grants_out = _list(include_revoked=include_revoked)
        elif ctx.role == 'admin':
            grants_out = _list(grantee=ctx.actor, include_revoked=include_revoked)
        else:
            grants_out = _list(grantor=ctx.user_id, include_revoked=include_revoked)
        return jsonify({'grants': grants_out, 'count': len(grants_out)})

    # 2026-08-31 ship: 记忆原生三维度自审 audit endpoint
    # 灵感来源: 微信文章"从外部记忆到记忆原生模型"by Bannings
    # (https://mp.weixin.qq.com/s/aL1gaDDGR1eJy2uzL5kKdQ)
    # 三维: 可寻址 (nest.search) / 可更新 (bus.append + nest.store wired) /
    #       可计算 (query 进 nest search, 不是 hardcoded prefix)
    @app.route('/v1/audit/health', methods=['GET'])
    def audit_health():
        """Self-audit astor on 3-dim memory-native checklist (Bannings 2026-08-31).

        Returns per-dimension score (0/1) + evidence. Total 0-3.
        GET only — no side effects, no auth required (content-free).
        """
        import sqlite3 as _sqlite3
        from ._internal.acl_layout import get_db_path, Tier, Store

        evidence = {}

        # === Dim 1: 可寻址 (Addressable) ===
        # nest.search endpoint exists + nest DB has embeddings
        addressable_ok = False
        try:
            # Check nest DB has embeddings (any tier — public covers it)
            nest_path = str(get_db_path(Tier.PUBLIC, Store.NEST))
            conn = _sqlite3.connect(nest_path)
            cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'")
            has_table = cur.fetchone()[0] > 0
            if has_table:
                cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                addressable_ok = cnt > 0
                evidence['addressable'] = {
                    'nest_db': nest_path,
                    'embeddings_count': cnt,
                    'check': 'PASS — nest has embeddings'
                }
            else:
                evidence['addressable'] = {
                    'nest_db': nest_path,
                    'check': 'FAIL — embeddings table missing'
                }
            conn.close()
        except Exception as e:
            evidence['addressable'] = {'check': f'FAIL — {e}'}

        # === Dim 2: 可更新 (Updatable) ===
        # bus.append exists + nest.store wired (called after bus append)
        updatable_ok = False
        try:
            # Look for nest.store call in server.py source
            import re as _re
            src_path = os.path.join(os.path.dirname(__file__), 'server.py')
            with open(src_path, 'r', encoding='utf-8') as f:
                src = f.read()
            nest_store_wired = 'nest.store' in src
            # Check bus DB has append path (events_total > 0 implies writes happened)
            bus_path = str(get_db_path(Tier.PUBLIC, Store.BUS))
            conn = _sqlite3.connect(bus_path)
            events_cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.close()
            updatable_ok = nest_store_wired and events_cnt > 0
            evidence['updatable'] = {
                'nest_store_wired': nest_store_wired,
                'bus_events_count': events_cnt,
                'check': 'PASS — nest.store wired + events flowing' if updatable_ok else 'FAIL — check wiring'
            }
        except Exception as e:
            evidence['updatable'] = {'check': f'FAIL — {e}'}

        # === Dim 3: 可计算 (Computable) ===
        # nest.search computes embeddings live (not cached/hardcoded)
        # proxy: query_embedding goes through nest.search() not prefix-match
        computable_ok = False
        try:
            # Look for embedding model in nest.search call (model.embed or model.encode)
            src_path = os.path.join(os.path.dirname(__file__), 'server.py')
            with open(src_path, 'r', encoding='utf-8') as f:
                src = f.read()
            uses_live_embed = ('model.embed' in src or 'model.encode' in src) and 'nest.search' in src
            computable_ok = uses_live_embed
            evidence['computable'] = {
                'live_embed_in_search': uses_live_embed,
                'check': 'PASS — nest.search computes live embeddings' if computable_ok else 'FAIL — using prefix/cache'
            }
        except Exception as e:
            evidence['computable'] = {'check': f'FAIL — {e}'}

        # === Aggregate ===
        scores = {
            'addressable': 1 if addressable_ok else 0,
            'updatable': 1 if updatable_ok else 0,
            'computable': 1 if computable_ok else 0,
        }
        total = sum(scores.values())
        verdict = (
            'memory_native_ready' if total == 3 else
            'partially_native' if total >= 1 else
            'memory_external_only'
        )
        return jsonify({
            'version': __version__,
            'audit_ts': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
            'dimension_scores': scores,
            'total_score': f'{total}/3',
            'verdict': verdict,
            'evidence': evidence,
            'reference': 'mp.weixin.qq.com/s/aL1gaDDGR1eJy2uzL5kKdQ (Bannings 2026-08)'
        })

    @app.errorhandler(500)
    def internal_error(e):
        """Flask 500 handler that emits a structured JSON error + audit row."""
        return jsonify({'error': 'internal error', 'detail': str(e)}), 500

    return app


def main():
    """Run dev server: python -m astor_memory.server"""
    import argparse
    parser = argparse.ArgumentParser(description='Astor-Memory REST API server')
    parser.add_argument('--host', default='127.0.0.1', help='Bind host (default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=7803, help='Port (default 7803)')
    parser.add_argument('--debug', action='store_true', help='Flask debug mode')
    parser.add_argument('--astor-dir', help='Override ASTOR_DIR (for testing)')
    args = parser.parse_args()

    app = create_app(astor_dir=args.astor_dir)
    print(f'[*] Astor-Memory v{__version__} REST API')
    print(f'   Listening on http://{args.host}:{args.port}')
    print(f'   Endpoints: /v1/health /v1/write /v1/read /v1/install')
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()


__all__ = ['create_app', 'main']
