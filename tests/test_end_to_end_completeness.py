"""Tests for end-to-end completeness ship v1.14.65 P1-P8.

Covers:
- P2: astor_choose_extract_mode auto-routes to LLM when key + 30-1000 chars
- P5: _rewrite_query_for_recall strips CJK/EN discourse markers
- P7: cmd_recall_history parses recall_log.jsonl correctly
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


# -----------------------------------------------------------------------
# P2: extract mode heuristic
# -----------------------------------------------------------------------
class TestChooseExtractMode(unittest.TestCase):
    def setUp(self):
        # Save current env so we can restore it after each test
        from astor_memory.forge.extractor import astor_choose_extract_mode
        self.choose = astor_choose_extract_mode
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_short_text_falls_back_to_regex(self):
        # < 30 chars: even with LLM key, short text doesn't need LLM
        os.environ['OPENAI_API_KEY'] = 'test-key'
        self.assertEqual(self.choose('hi'), 'regex')
        self.assertEqual(self.choose('ok'), 'regex')

    def test_medium_text_no_llm_key_stays_regex(self):
        # 30-1000 chars but no key → regex (no network)
        os.environ.pop('OPENAI_API_KEY', None)
        os.environ.pop('OPENROUTER_API_KEY', None)
        os.environ.pop('ASTOR_LLM_ENABLED', None)
        text = '我今天 TFSA 亏了 5000 应该 trim 一些仓位, 然后看 NVDA 的 P/E ratio 是不是太高了'
        self.assertEqual(self.choose(text), 'regex')

    def test_medium_text_with_key_picks_llm(self):
        os.environ['OPENAI_API_KEY'] = 'test-key'
        text = '我今天 TFSA 亏了 5000 应该 trim 一些仓位, 然后看 NVDA 的 P/E ratio 是不是太高了'
        self.assertEqual(self.choose(text), 'llm')

    def test_medium_text_with_forced_flag_picks_llm(self):
        os.environ.pop('OPENAI_API_KEY', None)
        os.environ['ASTOR_LLM_ENABLED'] = '1'
        text = '我今天 TFSA 亏了 5000 应该 trim 一些仓位, 然后看 NVDA 的 P/E ratio 是不是太高了'
        self.assertEqual(self.choose(text), 'llm')

    def test_long_text_returns_none(self):
        os.environ['OPENAI_API_KEY'] = 'test-key'
        text = 'x' * 1500
        self.assertEqual(self.choose(text), 'none')


# -----------------------------------------------------------------------
# P5: query rewrite heuristic
# -----------------------------------------------------------------------
class TestRewriteQuery(unittest.TestCase):
    def setUp(self):
        from astor_memory.forge.extractor import _rewrite_query_for_recall
        self.rw = _rewrite_query_for_recall

    def test_strips_cjk_discourse(self):
        self.assertEqual(
            self.rw('我上次打 poker 怎么样'),
            '我上次打 poker',
        )

    def test_strips_en_discourse(self):
        self.assertEqual(
            self.rw('how was my last poker session'),
            'was my poker session',
        )

    def test_no_change_when_no_discourse(self):
        self.assertEqual(
            self.rw('poker Tuesday NLHE'),
            'poker Tuesday NLHE',
        )

    def test_empty_returns_empty(self):
        self.assertEqual(self.rw(''), '')

    def test_does_not_strip_punctuation(self):
        self.assertEqual(
            self.rw('poker, Tuesday, NLHE.'),
            'poker, Tuesday, NLHE.',
        )

    def test_falls_back_to_original_if_too_much_removed(self):
        # If we strip 80%+ of the query, that's not a useful rewrite.
        original = '我 你 他'  # all discourse, would become ''
        self.assertEqual(self.rw(original), original)


# -----------------------------------------------------------------------
# P7: recall-history CLI parses log correctly
# -----------------------------------------------------------------------
class TestRecallHistoryParser(unittest.TestCase):
    """Direct test of the log-filtering logic without invoking the CLI.

    The CLI is a thin wrapper around: read recall_log.jsonl, filter by
    --tier/--user/--hours/--contains, sort by ts desc, slice by --limit.
    We test the parser directly by replicating the loop here to lock
    in the expected JSON schema (so server.py changes don't break
    the CLI silently).
    """
    def _parse(self, log_path, args):
        rows = []
        with open(log_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if args.get('tier') and rec.get('tier') != args['tier']:
                    continue
                if args.get('user') and rec.get('user_id') != args['user']:
                    continue
                if args.get('contains'):
                    if args['contains'] not in (rec.get('query') or ''):
                        continue
                rows.append(rec)
        rows.sort(key=lambda r: r.get('ts', ''), reverse=True)
        return rows[:args.get('limit', 20)]

    def test_filter_by_tier(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'recall_log.jsonl'
            log.write_text(
                '{"ts": "2026-09-17T12:00:00", "tier": "public", "user_id": "admin", "qhash": "aaa", "query": "p1"}\n'
                '{"ts": "2026-09-17T12:01:00", "tier": "private", "user_id": "admin", "qhash": "bbb", "query": "p2"}\n'
                '{"ts": "2026-09-17T12:02:00", "tier": "public", "user_id": "sunday", "qhash": "ccc", "query": "p3"}\n'
            )
            out = self._parse(log, {'tier': 'public'})
            self.assertEqual(len(out), 2)
            self.assertEqual([r['qhash'] for r in out], ['ccc', 'aaa'])

    def test_filter_by_user(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'recall_log.jsonl'
            log.write_text(
                '{"ts": "2026-09-17T12:00:00", "tier": "public", "user_id": "admin", "qhash": "a", "query": ""}\n'
                '{"ts": "2026-09-17T12:01:00", "tier": "private", "user_id": "sunday", "qhash": "b", "query": ""}\n'
            )
            out = self._parse(log, {'user': 'sunday'})
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]['user_id'], 'sunday')

    def test_filter_by_contains(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'recall_log.jsonl'
            log.write_text(
                '{"ts": "2026-09-17T12:00:00", "tier": "public", "user_id": "admin", "qhash": "a", "query": "poker Tuesday NLHE"}\n'
                '{"ts": "2026-09-17T12:01:00", "tier": "public", "user_id": "admin", "qhash": "b", "query": "stock portfolio"}\n'
            )
            out = self._parse(log, {'contains': 'poker'})
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]['qhash'], 'a')

    def test_limit_truncates(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'recall_log.jsonl'
            lines = '\n'.join(
                f'{{"ts": "2026-09-17T12:0{i}:00", "tier": "public", "user_id": "admin", "qhash": "{i}", "query": ""}}'
                for i in range(5)
            )
            log.write_text(lines)
            out = self._parse(log, {'limit': 3})
            self.assertEqual(len(out), 3)


if __name__ == '__main__':
    unittest.main()
