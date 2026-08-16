"""Tests for the hermes astor_memory adapter integration.

Run:    python tests/test_hermes_adapter.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ['ASTOR_DIR'] = str(Path(r'<runtime_dir>'))
sys.path.insert(0, str(Path(r'<home_dir>AppData\Local\hermes\hermes-agent')))
sys.path.insert(0, str(Path(r'<source_dir>')))


class HermesAdapterImportTest(unittest.TestCase):
    def test_adapter_imports(self):
        from plugins.memory.astor_memory import (
            AstorMemoryProvider,
        )
        self.assertTrue(callable(AstorMemoryProvider))


class HermesAdapterToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Confirm the astor runtime is reachable
        from plugins.memory.astor_memory import AstorMemoryProvider
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
            # default hybrid=True
            self.assertEqual(r['score_kind'], 'hybrid')

    def test_recall_hybrid_false(self):
        result = json.loads(self.provider.handle_tool_call(
            'astor_recall', {'query': 'astor memory', 'top_k': 3,
                             'hybrid': False},
        ))
        for r in result['results']:
            self.assertEqual(r['score_kind'], 'cosine')

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
        unique = f'delete_me_koala_{os.getpid()}'
        # Write a fact via the REST API so the full pipeline (event +
        # candidate + canonical + nest + lex index) runs.
        import urllib.request as _ur
        body = json.dumps({
            'text': f'Forgettable marker fact {unique} for adapter test',
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

        # Forget by query via the adapter
        result = json.loads(self.provider.handle_tool_call(
            'astor_forget', {
                'query': unique, 'tier': 'public', 'user_id': None,
                'forget_threshold': 0.5,
            },
        ))
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
        from plugins.memory.astor_memory import AstorMemoryProvider
        p = AstorMemoryProvider()
        schemas = p.get_tool_schemas()
        names = {s['name'] for s in schemas}
        self.assertIn('astor_recall', names)
        self.assertIn('astor_write', names)
        self.assertIn('astor_forget', names)
        self.assertIn('astor_status', names)

    def test_recall_schema_has_hybrid_field(self):
        from plugins.memory.astor_memory import AstorMemoryProvider
        p = AstorMemoryProvider()
        for s in p.get_tool_schemas():
            if s['name'] == 'astor_recall':
                props = s['parameters']['properties']
                self.assertIn('hybrid', props)
                self.assertIn('cross_tier', props)
                return
        self.fail('astor_recall schema not found')

    def test_forget_schema_has_query_and_fact_id(self):
        from plugins.memory.astor_memory import AstorMemoryProvider
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
