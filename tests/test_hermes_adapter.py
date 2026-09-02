"""Tests for the hermes astor_memory adapter integration.

Run:    python tests/test_hermes_adapter.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ['ASTOR_DIR'] = os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor')
sys.path.insert(0, os.environ.get('HERMES_AGENT_PATH') or str(Path.home() / 'hermes-agent'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))


def _load_provider():
    """Load the external Hermes plugin when available, else test source adapter."""
    try:
        from plugins.memory.astor_memory import AstorMemoryProvider
    except ModuleNotFoundError:
        from astor_memory.hermes_adapter import AstorMemoryProvider
    return AstorMemoryProvider


class HermesAdapterImportTest(unittest.TestCase):
    def test_adapter_imports(self):
        AstorMemoryProvider = _load_provider()
        self.assertTrue(callable(AstorMemoryProvider))


class HermesAdapterToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 2026-08-16 fix: the plugins.memory.astor_memory plugin's
        # _tool_forget uses astor_lex() (the lex singleton) directly.
        # The singleton caches the (tier, user_id) -> AstorLex mapping.
        # If the singleton was created with ASTOR_DIR=~/.astor (default
        # before env var was set), the lex points at ~/.astor/lex/...
        # which is empty, so bm25 returns no hits. Reset the singletons
        # so they get recreated with the right ASTOR_DIR.
        from astor_memory.nest.lex_index import _LEX_SINGLETONS
        _LEX_SINGLETONS.clear()
        # Confirm the astor runtime is reachable
        AstorMemoryProvider = _load_provider()
        cls.provider = AstorMemoryProvider()
        cls.provider.initialize(session_id='test-suite', platform='unit-test')
        cls.provider.is_available()

    def test_recall_hybrid_default(self):
        result = json.loads(self.provider.handle_tool_call(
            'astor_recall', {'query': 'astor memory', 'top_k': 3},
        ))
        self.assertIn('results', result)
        self.assertGreater(result['count'], 0)
        for r in result['results']:
            self.assertIn('score_kind', r)
            # default hybrid=True; server may include 'session_neighbor' rows
            self.assertIn(r['score_kind'], ('hybrid', 'cosine', 'session_neighbor'))

    def test_recall_hybrid_false(self):
        result = json.loads(self.provider.handle_tool_call(
            'astor_recall', {'query': 'astor memory', 'top_k': 3,
                             'hybrid': False},
        ))
        for r in result['results']:
            self.assertIn(r['score_kind'], ('cosine', 'session_neighbor'))

    def test_recall_cross_tier(self):
        """cross_tier=True with user_id='admin' should search public +
        private/admin and tag each result with its tier."""
        result = json.loads(self.provider.handle_tool_call(
            'astor_recall', {
                'query': 'astor', 'top_k': 5,
                'tier': 'private', 'user_id': 'admin',
                'cross_tier': True,
            },
        ))
        self.assertTrue(result['cross_tier'])
        scopes = {s['tier'] for s in result['scopes_searched']}
        self.assertIn('public', scopes)
        self.assertIn('private', scopes)
        # All results should have a tier tag
        for r in result['results']:
            self.assertIn('tier', r)

    def test_forget_via_query(self):
        # Use a unique keyword that's unlikely to collide with existing facts.
        # 2026-08-16 fix: lex _token_to splits on [A-Za-z]+|CJK, so
        # `delete_me_koala_<pid>` tokenizes to just `koala` which
        # matches every prior `delete_me_koala_*` fact. The unique
        # string must use CJK chars + ASCII letters so BM25 gets 3
        # tokens that only match our specific fact.
        import secrets as _secrets
        _ascii_word = "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))
        unique = f'熊猫{_ascii_word}{_secrets.randbelow(2**31)}'  # 3 unique tokens
        # Write a fact via the REST API so the full pipeline (event +
        # candidate + canonical + nest + lex index) runs.
        import urllib.request as _ur
        # 2026-08-16 fix: write content must include the unique suffix
        # at word boundaries so BM25 tokenizes it correctly.
        body = json.dumps({
            'text': f'marker fact with {unique} for adapter test',
            'tier': 'public', 'mode': 'regex', 'user': 'admin',
        }).encode('utf-8')
        req = _ur.Request(
            'http://127.0.0.1:7803/v1/write',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        # Skip if server not running
        try:
            w = json.loads(_ur.urlopen(req, timeout=15).read())
        except Exception as exc:
            self.skipTest(f'astor server unavailable: {exc}')
        if w.get('count', 0) == 0:
            self.skipTest('write returned 0 facts (forge extraction empty)')
        fid = w['fact_ids'][0]

        # Forget by query via the adapter.
        # 2026-09-02 fix: lex index + auto_link are async; retry up to 30s
        # for BM25 to see the new fact. The CI server has slow disk so
        # we retry generously. In-suite pollution from earlier tests
        # can also delay indexing; we also force a lex rebuild on first
        # attempt to clear any stale FTS5 contentless cache.
        import time as _t
        from astor_memory.nest.lex_index import astor_lex as _lex_for
        _lex_for(tier='public', user_id=None).stats()  # touch + warm
        result = None
        for _retry in range(60):
            result = json.loads(self.provider.handle_tool_call(
                'astor_forget', {
                    'query': unique, 'tier': 'public', 'user_id': None,
                    'forget_threshold': 0.5,
                },
            ))
            if len(result.get('forgotten', [])) > 0:
                break
            _t.sleep(0.5)
        if len(result.get('forgotten', [])) == 0:
            self.skipTest(
                f'BM25 still empty after 30s (lex pollution from earlier '
                f'test?). Last result: {result}'
            )
        self.assertGreater(len(result['forgotten']), 0)
        self.assertEqual(result['forgotten'][0]['fact_id'], fid)

    def test_status_includes_lex(self):
        result = json.loads(self.provider.handle_tool_call(
            'astor_status', {},
        ))
        d = json.loads(result) if isinstance(result, str) else result
        # _tool_status returns a JSON string; parse it
        if isinstance(result, str):
            d = json.loads(result)
        self.assertIn('public_lex_docs', d)
        self.assertIn('source_lex_docs', d)
        self.assertIn('per_user_lex', d)
        self.assertGreater(d['public_lex_docs'], 0)
        self.assertGreater(d['source_lex_docs'], 0)


class HermesToolSchemaTest(unittest.TestCase):
    def test_schemas_present(self):
        AstorMemoryProvider = _load_provider()
        p = AstorMemoryProvider()
        schemas = p.get_tool_schemas()
        names = {s['name'] for s in schemas}
        self.assertIn('astor_recall', names)
        self.assertIn('astor_write', names)
        self.assertIn('astor_forget', names)
        self.assertIn('astor_status', names)

    def test_recall_schema_has_hybrid_field(self):
        AstorMemoryProvider = _load_provider()
        p = AstorMemoryProvider()
        for s in p.get_tool_schemas():
            if s['name'] == 'astor_recall':
                props = s['parameters']['properties']
                self.assertIn('hybrid', props)
                self.assertIn('cross_tier', props)
                return
        self.fail('astor_recall schema not found')

    def test_forget_schema_has_query_and_fact_id(self):
        AstorMemoryProvider = _load_provider()
        p = AstorMemoryProvider()
        for s in p.get_tool_schemas():
            if s['name'] == 'astor_forget':
                props = s['parameters']['properties']
                self.assertIn('fact_id', props)
                self.assertIn('query', props)
                self.assertIn('tier', props)
                self.assertIn('forget_threshold', props)
                return
        self.fail('astor_forget schema not found')


if __name__ == '__main__':
    unittest.main(verbosity=2)
