"""
Bus: SQLite-based event log + canonical fact store.

Provides:
- Bus class with append_event / insert_candidate / promote_candidate
- Transaction context for atomic multi-insert (Plan § Crash recovery)
- Default connection with WAL + foreign keys + busy_timeout (Plan § Memory <-> concurrency)

v1.0 simple install: single file ~/.astor/astor.db
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import astor_init_schema, astor_verify_schema
from datetime import datetime, timezone

def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string with microseconds."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%fZ')



@dataclass
class AstorEvent:
    """Lightweight event representation."""
    id: int
    ts: str
    namespace: str
    agent_id: str
    source: str
    action: str
    content: str
    metadata: dict[str, Any]


class AstorBus:
    """SQLite-backed bus for events + canonical facts."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._open()

    def _open(self):
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 5000')
        self._conn = conn
        astor_init_schema(conn)

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the bus SQLite connection (lazy-init, schema applied)."""
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def transaction(self):
        """Atomic transaction context. All writes within commit/rollback together."""
        with self._lock:
            c = self.conn.cursor()
            c.execute('BEGIN IMMEDIATE')
            try:
                yield c
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise

    def append_event(
        self,
        namespace: str,
        agent_id: str,
        source: str,
        action: str,
        content: str,
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> int:
        """Append an event to the bus. Returns event_id."""
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO events
                   (namespace, agent_id, source, action, content, metadata, request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    namespace,
                    agent_id,
                    source,
                    action,
                    content,
                    json.dumps(metadata or {}),
                    request_id,
                ),
            )
            event_id = cur.lastrowid
            assert event_id is not None
            return event_id

    def insert_candidate(
        self,
        event_id: int,
        namespace: str,
        content: str,
        kind: str = 'fact',
        confidence: float = 0.7,
        importance: float = 0.5,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        scene: str = 'casual',
        keywords: list[str] | None = None,
        context: str = '',
        event_date: str | None = None,
        event_date_precision: str = 'none',
        abstract: str = '',
        overview: str = '',
        topic: str = '',
        session_id: str = '',
    ) -> int:
        """Insert a candidate fact. Returns candidate_id.

        v1.2.0: keywords + context are v2 schema columns. We store them in
        the candidate's metadata JSON blob (existing column) so the
        promotion path can read them back without needing a v5 candidate
        schema migration. promote_candidate re-reads metadata and pulls
        keywords/context out.

        v1.3.0 (2026-08-25): event_date + event_date_precision also stored
        in metadata JSON for temporal rerank during recall. Validated as
        ISO-8601 'YYYY-MM-DD' before insert.

        v1.6.0 (2026-08-25): abstract (L0) + overview (L1) stored in
        metadata JSON for OpenViking-style progressive loading. The
        system prompt loads only L0 abstracts (~80 tokens each); agent
        drills into L1/L2 only when needed. Cap applied to avoid runaway
        sizes if the extractor returns a 50KB blob.
        """
        # v1.2.0: merge keywords + context into metadata JSON for storage.
        # This avoids needing a separate column on memory_candidates.
        meta = dict(metadata or {})
        if keywords is not None:
            meta['__keywords__'] = list(keywords)
        if context:
            meta['__context__'] = str(context)[:500]
        # v1.6.0: L0 abstract + L1 overview — cap to keep storage bounded
        # even if a misbehaving extractor emits huge blobs.
        if abstract:
            meta['__abstract__'] = str(abstract)[:500]
        if overview:
            meta['__overview__'] = str(overview)[:1500]
        # v1.12.0 (2026-08-29): topic + session_id for hierarchical extraction
        # (Mem0 2026 pattern). Stored in metadata JSON to avoid schema change.
        if topic:
            meta['__topic__'] = str(topic)[:100]
        if session_id:
            meta['__session_id__'] = str(session_id)[:128]
        # v1.3.0: event_date for temporal recall rerank
        if event_date:
            # Validate ISO-8601 YYYY-MM-DD shape (lenient: accept YYYY-MM too)
            ed = str(event_date).strip()
            import re as _re_ed
            if _re_ed.match(r'^\d{4}-\d{2}(-\d{2})?$', ed):
                meta['__event_date__'] = ed
                meta['__event_date_precision__'] = str(event_date_precision or 'day')[:16]
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO memory_candidates
                   (event_id, namespace, content, kind, confidence, importance,
                    tags, metadata, scene)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    namespace,
                    content,
                    kind,
                    confidence,
                    importance,
                    json.dumps(tags or []),
                    json.dumps(meta),
                    scene,
                ),
            )
            candidate_id = cur.lastrowid
            assert candidate_id is not None
            return candidate_id

    def promote_candidate(
        self,
        candidate_id: int,
        promoted_by: str,
        user_id: str | None = None,
        tier: str = 'public',
        scope_type: str = 'long_term',
        verdict: str = 'settled',
        origin_session_id: str | None = None,
        stable_id: str | None = None,
    ) -> int:
        """Promote a candidate to canonical. Returns canonical_id.

        After the INSERT, computes embedding via nest and stores it on the
        canonical row so recall() works (Plan § Write-time dedup).
        """
        # P0-fix 2026-08-15: dedup check BEFORE INSERT.
        # P1-fix 2026-08-16: **content-aware** dedup. We only treat the
        # existing canonical row as idempotent if its content matches the
        # candidate's content. Otherwise the existing row is a STALE
        # ORPHAN from a previous failed promote + re-insert cycle — fall
        # through to INSERT which DELETES the stale row first.
        # Keep deduplication and canonical INSERT in one IMMEDIATE
        # transaction. Separate check/insert transactions allowed two workers
        # to both observe "no row" and race on UNIQUE(candidate_id).
        existing_canonical_id = None
        with self.transaction() as c:
            cand_row = c.execute(
                "SELECT content FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if cand_row is None:
                raise ValueError(f"Candidate {candidate_id} not found")
            _candidate_content = cand_row[0]
            existing = c.execute(
                "SELECT id, content FROM memory_canonical WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None and existing[1] == _candidate_content:
                existing_canonical_id = existing[0]
            else:
                row = c.execute(
                    "SELECT event_id, namespace, content, kind, confidence, importance, tags, metadata, scene FROM memory_candidates WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                assert row is not None
                event_id, namespace, content, kind, confidence, importance, tags, metadata, scene = row
                # v1.2.0: pull keywords/context out of metadata JSON. Older
                # candidates without these get safe defaults ('[]' / '').
                try:
                    meta_dict = json.loads(metadata) if metadata else {}
                except Exception:
                    meta_dict = {}
                kw_json = json.dumps(meta_dict.get('__keywords__') or [])
                ctx_text = str(meta_dict.get('__context__') or '')[:500]
                # v1.10.0: extract event_date from metadata JSON (legacy path).
                # Fresh facts come from /v1/write which already threads
                # event_date through insert_candidate params, so the column
                # gets set directly. This handles the upgrade case where
                # older facts only stored event_date in metadata.
                ev_date = meta_dict.get('__event_date__')
                ev_prec = meta_dict.get('__event_date_precision__') or 'none'
                # A stale orphan with different content can be replaced while
                # this same write lock is held; tombstoning cannot bypass the
                # UNIQUE(candidate_id) constraint.
                c.execute(
                    "DELETE FROM memory_canonical WHERE candidate_id = ? AND content != ?",
                    (candidate_id, content),
                )
                cur = c.execute(
                    """INSERT INTO memory_canonical
                       (candidate_id, event_id, namespace, content, kind, confidence, importance,
                        tags, metadata, keywords, context,
                        promoted_by, user_id, tier, scope_type, verdict,
                        origin_session_id, stable_id, embedding_version,
                        event_date, event_date_precision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_id, event_id, namespace, content, kind, confidence, importance,
                        tags, metadata, kw_json, ctx_text,
                        promoted_by, user_id, tier, scope_type, verdict,
                        origin_session_id, stable_id, 1,
                        ev_date, ev_prec,
                    ),
                )
                canonical_id = cur.lastrowid
                assert canonical_id is not None
        if existing_canonical_id is not None:
            # True idempotent retried write — already promoted with matching
            # content. Audit must happen OUTSIDE the now-closed transaction.
            # v1.10.9 (2026-08-26): also patch event_date on the existing row
            # when it was missing. The caller may have added metadata.event_date
            # in a later ingest (e.g. LoCoMo session timestamps were not
            # threaded through until v1.10.9). Without this patch, an OMB
            # re-ingest of stale conv-X data leaves event_date NULL even though
            # we'd extract it from metadata on a fresh write.
            if ev_date:
                self.conn.execute(
                    "UPDATE memory_canonical SET event_date = ?, "
                    "event_date_precision = COALESCE(NULLIF(?, 'none'), event_date_precision) "
                    "WHERE id = ? AND event_date IS NULL",
                    (ev_date, ev_prec or 'none', existing_canonical_id),
                )
            self.write_audit(
                event='promote_idempotent_replay',
                actor=promoted_by or 'system',
                target_type='candidate',
                target_id=candidate_id,
                metadata={'canonical_id': existing_canonical_id},
            )
            return existing_canonical_id

        # Compute + persist embedding (outside the bus transaction so a slow
        # embedding model doesn't hold the WAL lock).
        # P0-fix 2026-08-15: pass tier + user_id so embedding lands in the correct
        # per-tier nest DB. Previously called astor_nest() with no args which
        # raised ValueError (tier required) and silently swallowed embedding.
        # P1-fix 2026-08-16: on embed failure, enqueue to cascade_state queue
        # for replay (EverOS md_change_state pattern). Without this, fact
        # lives forever with no embedding and recall returns empty.
        try:
            from ..nest import astor_nest
            nest = astor_nest(tier=tier, user_id=user_id)
            nest.store(canonical_id, content)
        except Exception as e:
            # Embedding failure should not block fact storage; queue for
            # replay + write audit row.
            try:
                from . import cascade as _cascade
                _cascade.enqueue(
                    self,
                    fact_id=canonical_id,
                    operation='embed_insert',
                    tier=tier,
                    user_id=user_id,
                    payload={'content': content},
                    error=str(e),
                )
            except Exception:
                # If even enqueue fails (e.g. cascade_state missing),
                # fall back to bare audit row so we still see the failure.
                pass
            self.write_audit(
                event='embedding_failed',
                actor=promoted_by or 'system',
                target_type='canonical',
                target_id=canonical_id,
                metadata={'error': str(e), 'queued_for_replay': True},
            )

        with self.transaction() as c:
            c.execute(
                "UPDATE memory_candidates SET review_state='promoted', promoted_at=CURRENT_TIMESTAMP, promoted_to=? WHERE id=?",
                (canonical_id, candidate_id),
            )
        return canonical_id

    def write_audit(
        self,
        event: str,
        actor: str,
        target_type: str | None = None,
        target_id: str | None = None,
        old_state: dict | None = None,
        new_state: dict | None = None,
        reason: str | None = None,
        metadata: dict | None = None,
        severity: str = 'info',
    ) -> int:
        """Write an audit log entry. Returns audit_id.

        v1.10.8 (2026-08-26): added explicit `old_state` / `new_state` params.
        Previously callers had to stuff these into the `metadata` JSON, which
        meant versioning.py (which queries `audit_log.old_state IS NOT NULL`)
        could never see reflection-deprecate snapshots and restore_fact returned
        'no audit_log.old_state found' for legitimate cases.
        """
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO audit_log
                   (event, actor, target_type, target_id, old_state, new_state,
                    reason, metadata, severity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event, actor, target_type, target_id,
                 json.dumps(old_state or {}), json.dumps(new_state or {}),
                 reason, json.dumps(metadata or {}), severity),
            )
            audit_id = cur.lastrowid
            assert audit_id is not None
            return audit_id

    # ---- v1.6.0 memory_experience CRUD (OpenClaw Experience-inspired) ----

    def insert_experience(
        self,
        namespace: str,
        outcome: str,
        *,
        user_id: str | None = None,
        trigger_keywords: list[str] | None = None,
        trigger_fact_ids: list[int] | None = None,
        action_summary: str = '',
        context: str = '',
        reflection: str = '',
        next_step_hint: str = '',
        source_session_id: str | None = None,
        importance: float = 0.7,
        # v1.10.3: precomputed embedding. If the caller already computed
        # it (e.g. the hook batched several writes), pass it in to avoid
        # the embed call here. If None, we embed action_summary in this
        # transaction — adds ~300ms bge-base per write but eliminates the
        # 700-1100ms read-time cost.
        action_embedding: bytes | None = None,
    ) -> int:
        """Insert an experience row. Returns experience_id.

        OpenClaw Experience-inspired (per ClawBot 2026-08-25 14:40 digest).
        Distinct from memory_canonical: canonical facts are static
        knowledge ("user prefers coffee"); experiences are dynamic
        reflections ("last 3 times we ignored 'cancel'; next time, abort
        immediately"). The outcome tag on canonical is the TRIGGER; this
        row is the LEARNED REFLECTION.

        v1.10.3: precomputes action_summary embedding at write time so
        match_experiences at read time is fast (just matmul on a cached
        matrix). See _astor_upgrade_v7_to_v8.
        """
        if action_embedding is None and action_summary:
            # Embed at write time so reads are fast. bge-base cost
            # (~300ms) is amortized across all subsequent reads of this
            # experience; for experiences that get matched many times,
            # this is a clear win.
            try:
                from ..nest.embeddings import astor_get_embedding_model
                model = astor_get_embedding_model()
                import numpy as _np
                emb = list(model.embed([action_summary[:500]]))[0]
                action_embedding = _np.asarray(emb, dtype=_np.float32).tobytes()
            except Exception as _emb_exc:
                import sys as _s
                print(f"[astor.bus] experience embed failed (continuing): {_emb_exc}",
                      file=_s.stderr)
                action_embedding = None
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO memory_experience
                   (namespace, user_id, outcome, trigger_keywords, trigger_fact_ids,
                    action_summary, context, reflection, next_step_hint,
                    source_session_id, importance, action_embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    namespace,
                    user_id,
                    outcome,
                    json.dumps(trigger_keywords or []),
                    json.dumps(trigger_fact_ids or []),
                    action_summary,
                    context,
                    reflection,
                    next_step_hint,
                    source_session_id,
                    importance,
                    action_embedding,
                ),
            )
            exp_id = cur.lastrowid
            assert exp_id is not None
            return exp_id

    def list_experiences(
        self,
        namespace: str | None = None,
        user_id: str | None = None,
        outcome: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """List experience rows. Returns list of dicts newest-first."""
        clauses = []
        params: list = []
        if namespace is not None:
            clauses.append('namespace = ?')
            params.append(namespace)
        if user_id is not None:
            clauses.append('user_id = ?')
            params.append(user_id)
        if outcome is not None:
            clauses.append('outcome = ?')
            params.append(outcome)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT id, namespace, user_id, outcome, trigger_keywords, trigger_fact_ids, "
            f"action_summary, context, reflection, next_step_hint, invocation_count, "
            f"last_invoked_at, created_at, source_session_id, importance "
            f"FROM memory_experience {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        out = []
        for r in rows:
            try:
                tk = json.loads(r[4]) if r[4] else []
            except Exception:
                tk = []
            try:
                tf = json.loads(r[5]) if r[5] else []
            except Exception:
                tf = []
            out.append({
                'id': r[0],
                'namespace': r[1],
                'user_id': r[2],
                'outcome': r[3],
                'trigger_keywords': tk,
                'trigger_fact_ids': tf,
                'action_summary': r[6],
                'context': r[7],
                'reflection': r[8],
                'next_step_hint': r[9],
                'invocation_count': r[10],
                'last_invoked_at': r[11],
                'created_at': r[12],
                'source_session_id': r[13],
                'importance': r[14],
            })
        return out

    def match_experiences(
        self,
        query: str,
        *,
        namespace: str | None = None,
        user_id: str | None = None,
        outcome: str | None = None,
        top_k: int = 5,
        use_embedding: bool = True,
    ) -> list[dict]:
        """Find experiences matching query.

        v1.10.0: hybrid retrieval — keyword overlap (cheap) + embedding
        similarity (semantic). Keyword-only misses paraphrased experiences
        (e.g. 'hermes gateway restart' vs 'restart hermes gateway') which
        is exactly the failure mode of the 2026-08-25 audit when same
        concept was stored twice with different wording.

        Flow:
          1. Keyword match (always; cheap, fast)
          2. If use_embedding=True and bus has <100 experiences, embed
             query + score action_summary against it via O(n) cosine
          3. Combine via RRF-style score merge
        """
        import re as _re_match
        q_tokens = set()
        for ch in _re_match.findall(r'[\u4e00-\u9fff]', query):
            q_tokens.add(ch)
        for w in _re_match.findall(r'[A-Za-z0-9]+', query):
            q_tokens.add(w.lower())
        q_tokens_lower = q_tokens  # alias for readability
        clauses = []
        params: list = []
        if namespace is not None:
            clauses.append('namespace = ?')
            params.append(namespace)
        if user_id is not None:
            clauses.append('user_id = ?')
            params.append(user_id)
        if outcome is not None:
            clauses.append('outcome = ?')
            params.append(outcome)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = self.conn.execute(
            f"SELECT id, namespace, user_id, outcome, trigger_keywords, trigger_fact_ids, "
            f"action_summary, context, reflection, next_step_hint, invocation_count, "
            f"last_invoked_at, created_at, source_session_id, importance, action_embedding "
            f"FROM memory_experience {where}",
            params,
        ).fetchall()
        scored = []  # (score, row)
        # 1. Keyword scoring (cheap). Lowercase BOTH sides for case-insensitive
        # match (v1.10.1: query 'LSTM' should match trigger_keyword 'lstm').
        kw_scores: dict[int, float] = {}
        kw_hits: dict[int, int] = {}
        for r in rows:
            try:
                kws = json.loads(r[4]) if r[4] else []
            except Exception:
                kws = []
            if not kws:
                continue
            kws_lower = [str(k).lower() for k in kws if k]
            hits = sum(1 for kw_l in kws_lower
                       if kw_l and (kw_l in q_tokens_lower or any(t in kw_l for t in q_tokens_lower)))
            if hits > 0:
                kw_scores[int(r[0])] = hits * 0.5
                kw_hits[int(r[0])] = hits
        # 2. Embedding similarity. v1.10.1 perf fix: only embed if keyword
        # results are insufficient (less than top_k hits). The 2026-08-25
        # audit found semantic match cost +1.2s per recall (7 model.embed
        # calls when 6 experiences existed) — this kills throughput.
        # Strategy:
        #   - If keyword matched >= 1 row, skip embedding entirely (save 1+ sec).
        #   - Otherwise embed only action_summary of unmatched rows + query.
        #   - Cap: never embed more than 10 rows.
        # v1.10.1 perf: batch-embed all unmatched rows in a single model.embed() call
        # instead of N calls. Each call is ~300-700ms; batching cuts this to one
        # call (~400-800ms total). 6-8x speedup when kw=0 hits.
        emb_scores: dict[int, float] = {}
        need_embed = use_embedding and rows and len(rows) <= 100 and len(kw_hits) == 0
        if need_embed:
            try:
                # v1.10.3 fast path: if unmatched rows already have a precomputed
                # action_embedding column (written by insert_experience in v1.10.3+),
                # we skip the per-read embed entirely. Just embed the query, then
                # matmul against the cached column.
                import numpy as _np
                from astor_memory.nest.embeddings import astor_get_embedding_model, astor_get_model_name_for_ram
                model_name_for_query = astor_get_model_name_for_ram()  # bge-base to match canonical vector index dim
                model_for_query = astor_get_embedding_model(model_name=model_name_for_query)
                from astor_memory.nest.embeddings import astor_embed_query_cached
                query_emb = astor_embed_query_cached(model_for_query, model_name_for_query, query)
                # Collect unmatched rows
                unmatched = [r for r in rows if int(r[0]) not in kw_scores][:10]
                # Try precomputed path first
                precomputed = [(int(r[0]), r[15]) for r in unmatched if r[15]]
                if len(precomputed) == len(unmatched) and precomputed:
                    # All unmatched have precomputed embeddings — pure matmul.
                    emb_matrix = _np.frombuffer(
                        b"".join(blob for _, blob in precomputed),
                        dtype=_np.float32,
                    ).reshape(len(precomputed), -1)
                    qn = float(_np.linalg.norm(query_emb))
                    if qn > 0 and emb_matrix.shape[1] == query_emb.shape[0]:
                        e_norms = _np.linalg.norm(emb_matrix, axis=1)
                        valid = e_norms > 0
                        sims = _np.full(len(precomputed), -1e9, dtype=_np.float32)
                        sims[valid] = emb_matrix[valid] @ query_emb / (e_norms[valid] * qn)
                        for i, (fid, _) in enumerate(precomputed):
                            emb_scores[fid] = float(sims[i])
                else:
                    # Fallback: live embed unmatched text. Use bge-small (faster)
                    # since precomputed dim might not match query dim.
                    model_small = astor_get_embedding_model(model_name='BAAI/bge-small-en-v1.5')
                    unmatched_texts = [(int(r[0]), (r[6] or r[8] or '')[:500])
                                       for r in unmatched if (r[6] or r[8])]
                    if unmatched_texts:
                        all_texts = [query] + [t for _, t in unmatched_texts]
                        all_embs = list(model_small.embed(all_texts))
                        q_emb = all_embs[0]
                        qn = float(_np.linalg.norm(q_emb))
                        if qn > 0:
                            for (fid, _txt), emb in zip(unmatched_texts, all_embs[1:]):
                                en = float(_np.linalg.norm(emb))
                                if en > 0:
                                    emb_scores[fid] = float(
                                        _np.dot(q_emb, emb) / (qn * en)
                                    )
            except Exception as _emb_exc:
                import sys as _s_match
                print(f"[astor.bus] match_experiences embed failed (continuing): {_emb_exc}",
                      file=_s_match.stderr)
                pass
        # 3. Combine: 0.5*kw + 0.5*emb (if emb available), else kw-only.
        # v1.10.1: kw match is the PRIMARY signal. Embedding similarity is
        # secondary — it ranks among kw-matched rows (never introduces new
        # matches below sim 0.55). The bge-base-en-v1.5 model returns 0.45-0.55
        # for any short Chinese-mixed text, so without this guard semantic
        # match returns junk like 'LSTM training' → 'Hermes gateway restart'.
        for r in rows:
            fid = int(r[0])
            kw_s = kw_scores.get(fid, 0.0)
            emb_s = emb_scores.get(fid, 0.0) if emb_scores else 0.0
            if kw_s > 0:
                # Kw match always wins; emb is tiebreaker (30% weight)
                s = kw_s * 0.7 + emb_s * 0.3
            elif emb_s >= 0.55:
                # No kw hit but high emb similarity → real semantic match
                s = emb_s
            else:
                continue  # Filter noise
            if s > 0:
                scored.append((s, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:top_k]
        # Update invocation_count for matched experiences
        if results:
            ids = [r[0] for _, r in results]
            placeholders = ','.join('?' * len(ids))
            try:
                self.conn.execute(
                    f"UPDATE memory_experience SET invocation_count = invocation_count + 1, "
                    f"last_invoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
                self.conn.commit()
            except Exception:
                pass
        out = []
        for _, r in results:
            try:
                tk = json.loads(r[4]) if r[4] else []
                tf = json.loads(r[5]) if r[5] else []
            except Exception:
                tk = []; tf = []
            out.append({
                'id': r[0],
                'namespace': r[1],
                'user_id': r[2],
                'outcome': r[3],
                'trigger_keywords': tk,
                'trigger_fact_ids': tf,
                'action_summary': r[6],
                'context': r[7],
                'reflection': r[8],
                'next_step_hint': r[9],
                'invocation_count': r[10] + 1,  # reflects the +1 we just did
                'last_invoked_at': r[11],
                'created_at': r[12],
                'source_session_id': r[13],
                'importance': r[14],
            })
        return out

    def close(self) -> None:
        """Close the bus SQLite connection (used by CLI teardown)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# Module-level singleton (lazy init)
_astor_bus_singleton: AstorBus | None = None
_astor_bus_lock = threading.Lock()


def astor_bus(
    db_path: Path | None = None,
    tier: str | None = None,
    user_id: str | None = None,
) -> AstorBus:
    """
    Get or create a bus handle.

    2026-08-15 ship: backward-compat path REMOVED. The legacy single-file
    fallback (auto-creating ASTOR_DIR/astor_bus.db) was removed because it
    silently regenerated a root db that bypasses 3-tier × 3-store ACL.
    Callers MUST now explicitly pass `tier='public'|'source'|'private'`
    (and `user_id=<id>` for private).

    Args:
        db_path:  override the sqlite file path (testing only)
        tier:     'public' / 'source' / 'private' — REQUIRED
        user_id:  required when tier='private'

    Returns:
        AstorBus connected to the resolved db file.

    Raises:
        ValueError if tier is None.
    """
    from .._internal.acl_layout import get_db_path as _gdp
    from .._internal.acl import astor_check_read, astor_check_write, PermissionError_

    if tier is None:
        raise ValueError(
            "astor_bus() requires tier='public'|'source'|'private'. "
            "The legacy single-file fallback was removed 2026-08-15; "
            "see migrate_root_legacy_to_3tier.py for migration context."
        )
    # 2026-08-16 strict-privacy ship: opening bus requires READ access
    # (a read-grant covers read; a write-grant also covers read). Write
    # authorization is checked separately at write time, since read
    # endpoints (e.g. /v1/read) don't need write permission.
    astor_check_read(tier, user_id)
    if tier == 'private':
        try:
            astor_check_write(tier, user_id)
        except PermissionError_:
            # Read-grant holders pass through — caller may still get an
            # error at write time, but opening the bus for read is OK.
            pass
    if tier == "private" and user_id is None:
        raise ValueError("astor_bus(tier='private') requires user_id")
    target = db_path if db_path is not None else _gdp(tier, "bus", user_id)
    # v1.10.4: singleton per (tier, user_id, db_path). Previously every
    # /v1/read built a fresh AstorBus — each one opens a new sqlite3
    # connection (~5ms) and re-runs schema probes (~10ms cold) and
    # can't share any per-instance cache. Cache by key like astor_lex
    # does, so the same DB is reused across requests.
    if db_path is not None:
        return AstorBus(target)
    key = (tier, user_id, str(target))
    global _BUS_SINGLETONS
    if _BUS_SINGLETONS is None:
        _BUS_SINGLETONS = {}
    with _BUS_LOCK:
        inst = _BUS_SINGLETONS.get(key)
        if inst is None:
            inst = AstorBus(target)
            _BUS_SINGLETONS[key] = inst
        return inst


def astor_bus_for(tier: str, user_id: str | None = None) -> AstorBus:
    """
    Explicit 9-db layout accessor. Always uses the layout-derived path;
    raises PermissionError_ if ACL denies access.

    Examples:
        astor_bus_for('public')
        astor_bus_for('source')           # first_admin only
        astor_bus_for('private', 'alice') # alice's own db (or first_admin)
    """
    return astor_bus(db_path=None, tier=tier, user_id=user_id)


# v1.10.4: singleton cache for astor_bus (analogous to astor_nest fix).
import threading as _thr
_BUS_SINGLETONS: dict | None = None
_BUS_LOCK = _thr.Lock()


def _get_or_create_singleton(path: Path) -> AstorBus:
    """Backward-compat singleton for legacy astor_bus() callers."""
    global _astor_bus_singleton
    with _astor_bus_lock:
        if _astor_bus_singleton is None:
            _astor_bus_singleton = AstorBus(Path(path))
        return _astor_bus_singleton


def astor_reset_bus() -> None:
    """Reset the singleton (for testing)."""
    global _astor_bus_singleton
    with _astor_bus_lock:
        if _astor_bus_singleton is not None:
            _astor_bus_singleton.close()
        _astor_bus_singleton = None


__all__ = ["AstorBus", "AstorEvent", "astor_bus", "astor_bus_for", "astor_reset_bus"]
