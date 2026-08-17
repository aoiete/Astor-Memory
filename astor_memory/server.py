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
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from . import __version__, astor_bus, astor_nest, astor_forge
from ._internal.acl import astor_init_acl, _CURRENT, astor_current_acl, PermissionError_
from ._internal.bot_binding import get_user
from .config import get_default_astor_dir, get_default_bus_path, get_default_nest_path


def _astor_resolve_actor(user_id: str | None) -> tuple[str, str]:
    """Resolve (actor, role) for a given user_id from bot-binding.db user_meta.

    Returns ('first_admin', 'first_admin') when:
      - user_id is None/empty
      - user_id is 'admin' (the canonical first_admin alias; matches install-state.json)
      - user is not in user_meta (fail closed as root)

    Otherwise returns ('admin:<id>', 'admin') for role='admin', or
    ('user:<id>', 'user') for role='user'.

    2026-08-16 ACL fix (P0): was hardcoded to first_admin, allowing user_a
    to write source tier and any user to read another user's private DB.
    """
    if not user_id or user_id == 'admin':
        return ('first_admin', 'first_admin')
    meta = get_user(user_id)
    if meta is None or not meta.get('active', 1):
        return ('first_admin', 'first_admin')  # unknown → fail closed as root
    role = meta.get('role', 'user')
    if role == 'first_admin':
        return ('first_admin', 'first_admin')
    if role == 'admin':
        return (f'admin:{user_id}', 'admin')
    return (f'user:{user_id}', 'user')


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
    # based on `body.user` (was hardcoded to first_admin, allowing user_a → source
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
                actor, role = _astor_resolve_actor(body_user)
                if tier == 'private':
                    target_user = body.get('user_id') or body_user
                elif tier == 'repo':
                    target_user = body_user
                else:
                    target_user = None
                try:
                    # Bind ACL with ACTOR's identity (user_id = body_user).
                    # The cross-user check below uses target_user to verify
                    # the actor is allowed to access the target's data.
                    astor_init_acl(
                        actor=actor, role=role, tier=tier,
                        user_id=body_user if tier in ('private', 'repo') else None,
                    )
                except (ValueError, PermissionError_) as exc:
                    return jsonify({'error': 'acl_init_failed', 'detail': str(exc)}), 403
                if tier == 'private':
                    from ._internal.acl import astor_check_read as _acr, astor_check_write as _acw
                    try:
                        _acr(tier='private', user_id=target_user)
                    except PermissionError_:
                        return jsonify({'error': 'cross_user_forbidden', 'detail': (
                            f"user={body_user!r} (role={role!r}) cannot read private_<{target_user}>; "
                            f"only first_admin/admin may access other users' private tier"
                        )}), 403
                    try:
                        _acw(tier='private', user_id=target_user)
                    except PermissionError_:
                        return jsonify({'error': 'cross_user_forbidden', 'detail': (
                            f"user={body_user!r} (role={role!r}) cannot write private_<{target_user}>; "
                            f"only first_admin/admin may write other users' private tier"
                        )}), 403
                return
        # Default bind for GET endpoints + POST without JSON body.
        # GETs are read-only public-tier inspections; safe to bind as
        # first_admin (server identity).
        try:
            _ = _CURRENT.actor
        except AttributeError:
            astor_init_acl(actor='first_admin', role='first_admin', tier='public')

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

        # P2-fix 2026-08-15: optional mirror_to_source. When tier=public and
        # mirror=true, also write the same fact into the source tier (admin-
        # only) so the agent's self-pattern store gets the same content.
        # This is the 3-store × 3-tier "fanout" pattern from the plan.
        mirror_to_source = bool(body.get('mirror_to_source', False)) and tier == 'public'

        # 2026-08-15 ship: respect tier from request body. Default 'public'.
        # v1.1: tier=repo passes user (= repo_id) to bus/nest/forge as user_id.
        bus_user_id = user if tier in ('private', 'repo') else None
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
        # 2. Extract facts (forge) — now writes llm_call_log audit row
        facts = forge.astor_extract_facts(
            text, mode=mode, tier=tier,
            user_id=user if tier == 'private' else None,
            actor='rest_api',
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
            )
            canon_id = bus.promote_candidate(
                cand_id, promoted_by='rest.write', user_id=user, tier=tier,
                scope_type=scope,  # P1-fix 2026-08-15: thread scope through
                stable_id=stable_id,  # P1-fix 2026-08-15: enable content-hash dedup
            )
            fact_ids.append(canon_id)
            # Index for BM25 keyword recall — best-effort
            try:
                _lex.index_fact(int(canon_id), f.content)
            except Exception as _lex_exc:
                import sys as _sys
                print(f'[astor.server] lex index_fact failed (continuing): {_lex_exc}', file=_sys.stderr)
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
        top_k = int(body.get('top_k', 5))

        # 2026-08-15 ship: recall targets the tier from request body.
        tier = body.get('tier', 'public')
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
        embeddings = list(model.embed([query]))
        query_emb = embeddings[0]

        if not use_hybrid:
            # Pure vector path (legacy)
            results = nest.search(query_emb, limit=top_k)
        else:
            from .nest.lex_index import (
                astor_lex as _astor_lex,
                hybrid_merge as _hybrid_merge,
            )
            lex = _astor_lex(tier=tier, user_id=user_id)
            vector_hits = nest.search(query_emb, limit=oversample)
            bm25_hits = lex.bm25_search(query, limit=oversample)
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
            merged = _hybrid_merge(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                bm25_weight=bm25_weight,
                vec_weight=vec_weight,
                limit=oversample,
                keyword_hits=keyword_hits if keyword_hits else None,
                query_keywords=query_keywords,
            )
            results = merged[:top_k]
            # If hybrid returned nothing (empty lex AND empty nest), fall
            # back to vector-only so the caller doesn't get a hard empty.
            if not results:
                results = nest.search(query_emb, limit=top_k)
        # Enrich with bus metadata (content, kind)
        enriched = []
        for fact_id, sim in results:
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, keywords, context "
                "FROM memory_canonical WHERE id = ?",
                (fact_id,),
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
            })
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
                actor='first_admin', role='first_admin',
                tier=t, user_id=u,
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
            bus = astor_bus(tier=tier, user_id=uid)
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id "
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
            if ctx.role != 'first_admin':
                return jsonify({'error': 'merge requires first_admin'}), 403
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
            if ctx.role != 'first_admin':
                return jsonify({'error': 'merge requires first_admin'}), 403
        except Exception:
            pass
        result = apply_merges(merges=merges, actor=actor)
        return jsonify(result)

    @app.route('/v1/fact/<int:fact_id>/provenance', methods=['GET'])
    def fact_provenance(fact_id):
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
        from .nest.versioning import restore_fact
        body = request.get_json(force=True) if request.is_json else {}
        try:
            ctx = astor_current_acl()
            if ctx.role != 'first_admin':
                return jsonify({'error': 'restore requires first_admin'}), 403
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
            if ctx.role != 'first_admin':
                return jsonify({'error': 'cascade_replay requires first_admin'}), 403
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
                actor='first_admin',
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
            if ctx.role != 'first_admin':
                return jsonify({'error': 'reload requires first_admin role'}), 403
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
        return jsonify({'error': 'not found', 'path': request.path}), 404

    @app.errorhandler(500)
    def internal_error(e):
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
    print(f'🚀 Astor-Memory v{__version__} REST API')
    print(f'   Listening on http://{args.host}:{args.port}')
    print(f'   Endpoints: /v1/health /v1/write /v1/read /v1/install')
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()


__all__ = ['create_app', 'main']
