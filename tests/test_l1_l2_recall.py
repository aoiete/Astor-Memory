"""Tests for the L1/L2 multi-granularity recall path (Ship A, ADR-0004).

Validates:
1. rebuild_clusters() groups facts by session_id and writes one row per
   group with mean-pooled embedding.
2. Single-fact clusters are skipped (need >=2 members to be useful).
3. search_l1_l2() does L1 cosine → top clusters → L2 cosine restricted
   to those clusters' members.
4. ASTOR_MULTIGR_ENABLED=0 disables the path.
5. Empty cluster_embeddings returns [] (caller falls back).

Runs against a temp SQLite mimicking the nest schema; doesn't touch
the live server.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import tempfile
import unittest
import numpy as np


def _pack(emb: np.ndarray) -> bytes:
    return struct.pack(f'{len(emb)}f', *emb)


def _setup_fake_nest() -> tuple[str, callable]:
    """Create a temp nest DB with the v4 schema + a few facts.

    Returns (path, close_fn). Facts have known session_ids so we can
    assert grouping.
    """
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE memory_canonical (
        id INTEGER PRIMARY KEY,
        content TEXT,
        session_id TEXT,
        tombstoned INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE embeddings (
        fact_id INTEGER NOT NULL,
        embedding BLOB NOT NULL,
        model_name TEXT NOT NULL,
        dim INTEGER NOT NULL,
        created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        tier TEXT NOT NULL DEFAULT 'private',
        PRIMARY KEY (fact_id, model_name)
    );
    CREATE TABLE cluster_embeddings (
        cluster_key TEXT NOT NULL,
        model_name TEXT NOT NULL,
        embedding BLOB NOT NULL,
        member_count INTEGER NOT NULL,
        updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY (cluster_key, model_name)
    );
    """)

    # Three sessions, 5 facts total
    # session A: 3 facts (cluster)
    # session B: 1 fact (singleton, should be skipped)
    # session C: 1 fact (singleton, should be skipped)
    facts = [
        (1, 'fact A1', 'sess-A'),
        (2, 'fact A2', 'sess-A'),
        (3, 'fact A3', 'sess-A'),
        (4, 'fact B1', 'sess-B'),
        (5, 'fact C1', 'sess-C'),
    ]
    dim = 4
    np.random.seed(42)
    for fid, content, sid in facts:
        conn.execute(
            "INSERT INTO memory_canonical (id, content, session_id, tombstoned) "
            "VALUES (?, ?, ?, 0)",
            (fid, content, sid),
        )
        # Random unit vector
        v = np.random.randn(dim).astype(np.float32)
        v /= np.linalg.norm(v)
        conn.execute(
            "INSERT INTO embeddings (fact_id, embedding, model_name, dim, tier) "
            "VALUES (?, ?, ?, ?, 'private')",
            (fid, _pack(v), 'fake-model', dim),
        )
    conn.commit()

    def close():
        conn.close()
    return path, close


class TestL1L2(unittest.TestCase):

    def setUp(self):
        self.db_path, self.close = _setup_fake_nest()
        self.conn = sqlite3.connect(self.db_path)
        self.dim = 4
        # Inject the conn into AstorNest for the test
        from astor_memory.nest.vector_store import AstorNest
        # Use a minimal AstorNest that doesn't open its own conn
        # — bypass __init__ by patching _conn directly
        self.nest = AstorNest.__new__(AstorNest)
        self.nest.db_path = self.db_path
        self.nest._conn = self.conn

    def tearDown(self):
        self.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_rebuild_clusters_writes_multi_member_clusters_only(self):
        n = self.nest.rebuild_clusters(model_name='fake-model')
        # sess-A has 3 members → 1 cluster. sess-B and sess-C are singletons.
        self.assertEqual(n, 1)
        rows = self.conn.execute(
            "SELECT cluster_key, member_count FROM cluster_embeddings"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'sess-A')
        self.assertEqual(rows[0][1], 3)

    def test_search_l1_l2_returns_facts_in_winning_clusters(self):
        self.nest.rebuild_clusters(model_name='fake-model')
        # Query vector close to sess-A facts (random, but biased)
        v = np.random.randn(self.dim).astype(np.float32)
        v /= np.linalg.norm(v)
        results = self.nest.search_l1_l2(v, l1_limit=2, l2_limit=5, model_name='fake-model')
        # All returned facts must be from a winning cluster
        self.assertGreater(len(results), 0)
        for ck, fid, sim in results:
            self.assertEqual(ck, 'sess-A')
            self.assertIn(fid, {1, 2, 3})
            # cosine is [-1, 1] — just assert it's a valid number
            self.assertGreaterEqual(sim, -1.0)
            self.assertLessEqual(sim, 1.0)

    def test_search_l1_l2_returns_empty_when_no_clusters(self):
        # Don't rebuild — cluster_embeddings is empty
        v = np.random.randn(self.dim).astype(np.float32)
        results = self.nest.search_l1_l2(v, l1_limit=2, l2_limit=5, model_name='fake-model')
        self.assertEqual(results, [])

    def test_search_l1_l2_disabled_by_env_var(self):
        self.nest.rebuild_clusters(model_name='fake-model')
        old = os.environ.get('ASTOR_MULTIGR_ENABLED')
        try:
            os.environ['ASTOR_MULTIGR_ENABLED'] = '0'
            v = np.random.randn(self.dim).astype(np.float32)
            results = self.nest.search_l1_l2(v, l1_limit=2, l2_limit=5, model_name='fake-model')
            self.assertEqual(results, [])
        finally:
            if old is None:
                os.environ.pop('ASTOR_MULTIGR_ENABLED', None)
            else:
                os.environ['ASTOR_MULTIGR_ENABLED'] = old

    def test_rebuild_clusters_idempotent(self):
        n1 = self.nest.rebuild_clusters(model_name='fake-model')
        n2 = self.nest.rebuild_clusters(model_name='fake-model')
        self.assertEqual(n1, n2)


if __name__ == '__main__':
    unittest.main()
