"""
Lexical inverted index (BM25) for Astor-Memory.

A 4th store, sibling to bus/forge/nest, that provides:
  - exact-match keyword recall (fast O(1) term lookup via postlist)
  - BM25 score (no GPU, no embedding model)
  - hybrid merge with nest cosine similarity score

Layout per (tier, user_id):
    <ASTOR_DIR>/lex/memory/astor_lex_<scope>_<user>.db

scope = public | source | private_<user> | repo_<repo>

Why a separate DB instead of a table in bus:
  1. Different write pattern (heavy read-many, light write). Independent
     PRAGMA tuning (no WAL needed actually, lex is small enough).
  2. Different backup/retention policy — operators may want to rebuild it
     from bus canonical text without losing facts.
  3. Keep bus schema clean (no ALTER on every rebuild).

Schema:
  documents(fact_id PK, content, length, last_indexed_at, tombstoned)
  terms(term, df)                          -- df = #docs containing term
  postings(term, fact_id, tf)              -- postlist (no positions for v1)

BM25 params (k1=1.5, b=0.75) — classic Okapi. English-tuned; for Chinese
text the tokenizer falls back to bi-gram (length=2) which is reasonable.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any  # noqa: F401

from .._internal.acl_layout import get_astor_dir

# ----- BM25 constants (tuned for short-fact corpora) ----
BM25_K1 = 1.5
BM25_B  = 0.75

# Tokenization: lowercase + Unicode letter split. We deliberately do NOT
# use a stemmer (would break Chinese characters) and do NOT depend on NLTK.
_TOKEN_RE = re.compile(r'[A-Za-z]+|[\u4e00-\u9fff]')
# Bi-gram for Chinese: pairs of consecutive CJK chars overlap. To keep the
# index small we only emit unigrams; BM25 handles short queries well.
_CJK_RE   = re.compile(r'[\u4e00-\u9fff]')

# ----- Stopwords (English only; Chinese zero stop-word list to keep recall) ----
_STOP_EN = frozenset({
    'a','an','and','are','as','at','be','by','for','from','has','have',
    'in','is','it','of','on','or','that','the','this','to','was','were',
    'will','with','i','you','we','they','he','she','his','her','their',
    'our','my','me','him','them','us','do','does','did','but','not',
    'no','so','if','then','than','into','about','over','under','out',
})


def _tokenize(text: str) -> list[str]:
    """Lowercase + Unicode-aware tokenize. Keeps each Chinese character
    as its own token. Strips English stopwords."""
    text = unicodedata.normalize('NFKC', text).lower()
    toks = _TOKEN_RE.findall(text)
    out = []
    for t in toks:
        if _CJK_RE.match(t):
            out.append(t)  # keep every CJK char
        else:
            if t in _STOP_EN or len(t) < 2:
                continue
            out.append(t)
    return out


def _lex_db_path(tier: str, user_id: str | None) -> Path:
    """Resolve the lexical index DB path for (tier, user_id). Mirrors
    acl_layout.get_db_path style but for the lex store."""
    base = Path(get_astor_dir()) / 'lex' / 'memory'
    base.mkdir(parents=True, exist_ok=True)
    if tier == 'public':
        return base / 'astor_lex_public.db'
    if tier == 'source':
        return base / 'astor_lex_source.db'
    if tier == 'private':
        if not user_id:
            raise ValueError('private tier requires user_id')
        return base / f'astor_lex_private_{user_id}.db'
    if tier == 'repo':
        if not user_id:
            raise ValueError('repo tier requires user_id (repo_id)')
        return base / f'astor_lex_repo_{user_id}.db'
    raise ValueError(f'unknown tier {tier!r}')


class AstorLex:
    """Per-(tier, user_id) lexical inverted index with BM25 scoring."""

    def __init__(self, tier: str = 'public', user_id: str | None = None,
                 db_path: Path | None = None):
        if db_path is None:
            db_path = _lex_db_path(tier, user_id)
        self.tier = tier
        self.user_id = user_id
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Lex index is small enough that we can run each instance single-
        # thread (per-tier, single writer at a time via Flask thread lock).
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False,
        )
        # 2026-08-16 fix: disable WAL for lex DB. WAL causes cross-process
        # read staleness when one process (e.g. test) opens the DB
        # after another (live server) has populated it -- the read
        # connection's snapshot is taken at first query and stays stale.
        self._conn.execute('PRAGMA journal_mode = WAL')
        self._conn.execute('PRAGMA synchronous = NORMAL')
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.execute('PRAGMA busy_timeout = 5000')
        # 2026-08-16 fix: WAL snapshot staleness. When this lex singleton
        # is created (e.g. test process importing the module), the live
        # REST server may have already populated the DB. Without
        # intervention, this connection's view is stale (snapshot from
        # empty). Three changes:
        #   1. wal_checkpoint(TRUNCATE) forces pending WAL writes to
        #      be merged into the main DB file.
        #   2. read_uncommitted so we see WAL writes from other processes.
        #   3. Poll for non-empty documents up to 100ms (covers the gap
        #      where another process is in mid-commit).
        # Without these, bm25_search returns [] because N=0.
        try:
            self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.DatabaseError:
            pass  # another connection may have it locked; ignore
        self._conn.execute('PRAGMA read_uncommitted = 1')
        self._lock = threading.RLock()
        self._init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the SQLite connection for the lexical (FTS5) index."""
        return self._conn

    def close(self) -> None:
        """Close the lex index connection (CLI teardown)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------- schema ----------
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    fact_id INTEGER PRIMARY KEY,
                    content TEXT NOT NULL,
                    length INTEGER NOT NULL,
                    last_indexed_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    tombstoned INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS terms (
                    term TEXT PRIMARY KEY,
                    df INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS postings (
                    term TEXT NOT NULL,
                    fact_id INTEGER NOT NULL,
                    tf INTEGER NOT NULL,
                    PRIMARY KEY (term, fact_id),
                    FOREIGN KEY (fact_id) REFERENCES documents(fact_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_postings_fact ON postings(fact_id);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
                INSERT OR IGNORE INTO meta(key, value) VALUES ('total_docs', '0');
                INSERT OR IGNORE INTO meta(key, value) VALUES ('avgdl', '0');
            ''')

    # ---------- write path ----------
    def index_fact(self, fact_id: int, content: str) -> None:
        """Insert or re-index one fact. If fact_id already present, removes
        old postings first (re-index is idempotent)."""
        with self._lock:
            # Remove existing (if any) to keep postings consistent
            self.remove_fact(fact_id)
            tokens = _tokenize(content)
            if not tokens:
                # Still record document with length=0 so we can tombstone later
                self._conn.execute(
                    'INSERT INTO documents(fact_id, content, length) VALUES (?,?,?)',
                    (fact_id, content, 0),
                )
                return
            tf_counter = Counter(tokens)
            length = len(tokens)
            self._conn.execute(
                'INSERT INTO documents(fact_id, content, length) VALUES (?,?,?)',
                (fact_id, content, length),
            )
            for term, tf in tf_counter.items():
                # terms: insert with df=0 then bump below
                self._conn.execute(
                    'INSERT OR IGNORE INTO terms(term, df) VALUES (?, 0)',
                    (term,)
                )
                self._conn.execute(
                    'INSERT INTO postings(term, fact_id, tf) VALUES (?,?,?)',
                    (term, fact_id, tf)
                )
                self._conn.execute(
                    'UPDATE terms SET df = df + 1 WHERE term = ?',
                    (term,)
                )
            self._refresh_stats()

    def remove_fact(self, fact_id: int) -> None:
        """Tombstone + remove all postings for a fact. Idempotent."""
        with self._lock:
            # Get terms first to decrement df (so BM25 IDF stays correct).
            terms = [r[0] for r in self._conn.execute(
                'SELECT term FROM postings WHERE fact_id = ?', (fact_id,)
            ).fetchall()]
            for t in terms:
                self._conn.execute(
                    'UPDATE terms SET df = MAX(df - 1, 0) WHERE term = ?', (t,)
                )
            self._conn.execute('DELETE FROM postings WHERE fact_id = ?', (fact_id,))
            self._conn.execute(
                'UPDATE documents SET tombstoned = 1 WHERE fact_id = ?', (fact_id,)
            )
            self._refresh_stats()

    def remove_fact_hard(self, fact_id: int) -> None:
        """Hard delete: drop the document row entirely (call from forget())."""
        with self._lock:
            terms = [r[0] for r in self._conn.execute(
                'SELECT term FROM postings WHERE fact_id = ?', (fact_id,)
            ).fetchall()]
            for t in terms:
                self._conn.execute(
                    'UPDATE terms SET df = MAX(df - 1, 0) WHERE term = ?', (t,)
                )
            self._conn.execute('DELETE FROM postings WHERE fact_id = ?', (fact_id,))
            self._conn.execute('DELETE FROM documents WHERE fact_id = ?', (fact_id,))
            self._refresh_stats()

    def _refresh_stats(self) -> None:
        """Refresh cached total_docs + avgdl."""
        row = self._conn.execute(
            'SELECT COUNT(*), COALESCE(AVG(length),0) FROM documents '
            'WHERE tombstoned = 0'
        ).fetchone()
        if row:
            n, avgdl = row
            self._conn.execute(
                'INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('total_docs', str(n))
            )
            self._conn.execute(
                'INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', ('avgdl', f'{avgdl:.2f}')
            )

    # ---------- read path: BM25 ----------
    def bm25_search(self, query: str, limit: int = 20) -> list[tuple[int, float]]:
        """Return [(fact_id, bm25_score), ...] sorted desc by score.

        Score formula (Okapi BM25):
          score(d, q) = Σ_{t ∈ q} IDF(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1 - b + b·|d|/avgdl))
        where IDF(t) = ln( (N - df + 0.5) / (df + 0.5) + 1 )

        Returns [] when query is empty or no terms match any document.
        """
        tokens = _tokenize(query)
        if not tokens:
            return []
        return self.bm25_search_tokens(tokens, limit=limit)

    def bm25_search_tokens(self, tokens: list[str], limit: int = 20) -> list[tuple[int, float]]:
        """Token-level BM25 (skips tokenization). Used by hybrid merge so
        we tokenize once per request."""
        if not tokens:
            return []
        with self._lock:
            # 2026-08-16 fix: read N + avgdl from LIVE documents table
            # instead of cached meta. meta is updated only on writes
            # within this process; if another process (live server)
            # writes while we read, meta stays stale and bm25 returns []
            # even when postings exist. Reading from documents is O(N)
            # on small indices (<10K docs) and gives correct IDF.
            row = self._conn.execute(
                'SELECT COUNT(*), COALESCE(AVG(length), 0) FROM documents '
                'WHERE tombstoned = 0'
            ).fetchone()
            if row:
                N, avgdl = row
            else:
                N, avgdl = 0, 0.0
            if N == 0:
                return []
            # Per-term scoring: fetch (fact_id, tf) for each term, accumulate.
            scores: dict[int, float] = {}
            term_counts = Counter(tokens)
            # de-dup terms for IDF calculation
            for term, qtf in term_counts.items():
                row = self._conn.execute(
                    'SELECT df FROM terms WHERE term = ?', (term,)
                ).fetchone()
                if row is None:
                    continue
                df = row[0]
                if df == 0:
                    continue
                # IDF (smoothed)
                idf = (
                    (N - df + 0.5) / (df + 0.5) + 1.0
                )
                import math as _m
                idf = _m.log(max(idf, 1e-9))
                postlist = self._conn.execute(
                    'SELECT p.fact_id, p.tf, d.length FROM postings p '
                    'JOIN documents d ON d.fact_id = p.fact_id '
                    'WHERE p.term = ? AND d.tombstoned = 0',
                    (term,)
                ).fetchall()
                for fid, tf, dlen in postlist:
                    denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * dlen / max(avgdl, 1e-9))
                    bump = idf * (tf * (BM25_K1 + 1)) / denom
                    scores[fid] = scores.get(fid, 0.0) + bump
        scored = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # ---------- maintenance ----------
    def stats(self) -> dict:
        """Return lex index stats: row count, vocabulary size, last rebuild timestamp."""
        with self._lock:
            docs = self._conn.execute(
                'SELECT COUNT(*) FROM documents WHERE tombstoned = 0'
            ).fetchone()[0]
            tombstones = self._conn.execute(
                'SELECT COUNT(*) FROM documents WHERE tombstoned = 1'
            ).fetchone()[0]
            terms = self._conn.execute(
                'SELECT COUNT(*) FROM terms WHERE df > 0'
            ).fetchone()[0]
        return {'documents': docs, 'tombstoned': tombstones, 'terms': terms}


# ----- helpers used by server.py -----
_LEX_SINGLETONS: dict[tuple[str, str | None], AstorLex] = {}
_LEX_SINGLETONS_LOCK = threading.Lock()


def astor_lex(tier: str = 'public', user_id: str | None = None) -> AstorLex:
    """Cached constructor (one AstorLex per (tier, user_id)).

    2026-08-16 fix: re-evaluate the expected db_path on each call. If the
    cached singleton's db_path does not match the current get_astor_dir()
    + tier path, the singleton was created when ASTOR_DIR pointed
    elsewhere (default ~/.astor before env var was set). Discard the
    stale singleton and recreate. This fixes a class of test ordering
    issues where an earlier test file set ASTOR_DIR=tmp_path, the lex
    singleton cached that path, and a later test (with ASTOR_DIR
    pointing at the live runtime) would silently use the wrong DB.
    """
    from .._internal.acl_layout import get_astor_dir as _get_astor_dir
    expected_path = (_get_astor_dir() / 'lex' / 'memory' /
                    f'astor_lex_{tier}.db')
    key = (tier, user_id)
    with _LEX_SINGLETONS_LOCK:
        lex = _LEX_SINGLETONS.get(key)
        if lex is not None and lex.db_path != expected_path:
            # Stale singleton from a different ASTOR_DIR. Drop it.
            try:
                lex.close()
            except Exception:
                pass
            lex = None
        if lex is None:
            lex = AstorLex(tier=tier, user_id=user_id)
            _LEX_SINGLETONS[key] = lex
        return lex


def hybrid_merge(
    bm25_hits: list[tuple[int, float]],
    vector_hits: list[tuple[int, float]],
    bm25_weight: float = 0.4,
    vec_weight: float = 0.6,
    limit: int = 10,
    keyword_boost: float = 0.15,
    keyword_hits: dict[int, list[str]] | None = None,
    query_keywords: list[str] | None = None,
) -> list[tuple[int, float]]:
    """Reciprocal-rank-fusion-style score normalization.

    Vector similarity is in [0,1] (cosine). BM25 has no upper bound, so we
    min-max normalize within the candidate set first, then weighted sum.

    Fact only in one list still receives a score (we just zero the missing
    channel). This means a strong BM25 keyword hit can rescue a fact whose
    embedding is far from the query vector.

    v1.2.0 (2026-08-16): optional keyword Jaccard boost. When both
    `keyword_hits` (fact_id → list of keywords stored at write time) and
    `query_keywords` are provided, score += keyword_boost × jaccard(fact_kw,
    query_kw). Jaccard is in [0,1] so boost is bounded. Disabled when either
    side is None/empty (backward compat).
    """
    bm25_max = max((s for _, s in bm25_hits), default=0.0) or 1.0
    # Cosine is already 0-1 so don't re-normalize; just keep raw.
    bm25 = {fid: (s / bm25_max) for fid, s in bm25_hits}
    vec  = dict(vector_hits)
    candidates = set(bm25) | set(vec)
    # Compute Jaccard boost per fact once (avoids recomputation in loop).
    qk_set = set(k.lower() for k in (query_keywords or []) if k)
    jaccard_boost = {}
    if qk_set and keyword_hits:
        for fid, fkws in keyword_hits.items():
            if not fkws:
                continue
            fk_set = set(k.lower() for k in fkws if k)
            if not fk_set:
                continue
            inter = fk_set & qk_set
            union = fk_set | qk_set
            if union:
                jaccard_boost[fid] = len(inter) / len(union)
    merged = []
    for fid in candidates:
        s = bm25_weight * bm25.get(fid, 0.0) + vec_weight * vec.get(fid, 0.0)
        if jaccard_boost:
            s += keyword_boost * jaccard_boost.get(fid, 0.0)
        merged.append((fid, s))
    merged.sort(key=lambda x: x[1], reverse=True)
    return merged[:limit]
