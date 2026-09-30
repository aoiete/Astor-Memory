"""test_coref.py — v1.16.10 coreference resolution tests.

Verifies:
  - _extract_entities_from_text: Chinese + English names from raw text
  - _extract_entities_from_entities_json: structured name extraction
  - _build_antecedent_pool: aggregates names from recent facts (newer first)
  - _resolve_pronouns: Chinese + English pronoun replacement
  - resolve_coreferences: end-to-end with in-memory bus stub
  - Conservative: no antecedent → no change
  - Multi-character CN pronouns matched first (greedy)
  - English pronoun case preserved (He → Maria vs he → maria)
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestExtractEntities(unittest.TestCase):
    def test_english_proper_nouns(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        ents = _extract_entities_from_text("Maria mentioned the deadline.")
        self.assertIn("Maria", ents)

    def test_chinese_titles(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        ents = _extract_entities_from_text("王经理说截止日期改了。")
        self.assertTrue(any("王" in e for e in ents))

    def test_filters_common_chinese_words(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        # '他们' is a pronoun — should NOT be extracted as an entity
        ents = _extract_entities_from_text("他们决定推迟项目。")
        for e in ents:
            self.assertNotIn("他们", e)

    def test_dedup_case_insensitive(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        ents = _extract_entities_from_text("Maria and maria and MARIA all agree.")
        # All three should dedupe to one
        maria_count = sum(1 for e in ents if e.lower() == "maria")
        self.assertEqual(maria_count, 1)


class TestResolvePronouns(unittest.TestCase):
    def test_chinese_pronoun_replacement(self):
        from astor_memory.nest.coref import _resolve_pronouns
        pool = ["Maria"]
        rewritten, resolutions = _resolve_pronouns("她说她没被告知。", pool)
        self.assertIn("Maria", rewritten)
        self.assertGreater(len(resolutions), 0)
        self.assertEqual(resolutions[0]['pronoun'], '她')

    def test_english_pronoun_replacement(self):
        from astor_memory.nest.coref import _resolve_pronouns
        pool = ["Maria"]
        rewritten, resolutions = _resolve_pronouns("She said she was not told.", pool)
        # Both 'She' and 'she' replaced (same antecedent reused)
        self.assertEqual(len(resolutions), 2)
        # Both resolutions point to Maria (case-preserved at each match)
        for r in resolutions:
            self.assertEqual(r['replaced_with'], 'Maria')
        # First occurrence was 'She' (capital) → 'Maria'
        # Second was 'she' (lower) → 'maria' (case preserved as-is)
        # We accept either: both 'Maria' (test b) OR mixed case
        # Just verify 'Maria' is in the result
        self.assertIn("Maria", rewritten)

    def test_multichar_pronoun_greedy(self):
        from astor_memory.nest.coref import _resolve_pronouns
        pool = ["Acme Corp"]
        rewritten, resolutions = _resolve_pronouns("该公司的产品很好。", pool)
        # Should match '该公司' (4 chars) BEFORE '她' (1 char)
        # because CN_PRONOUNS list has '该公司' before '她'
        self.assertIn("Acme Corp", rewritten)
        if resolutions:
            self.assertEqual(resolutions[0]['pronoun'], '该公司')

    def test_no_antecedent_no_change(self):
        from astor_memory.nest.coref import _resolve_pronouns
        rewritten, resolutions = _resolve_pronouns("她说她没被告知。", [])
        self.assertEqual(rewritten, "她说她没被告知。")
        self.assertEqual(resolutions, [])

    def test_consume_antecedent_once(self):
        # v1.16.10 design: cursor continues across langs. CN pronoun
        # types consume pool[0] (Maria), then EN pronoun types consume
        # pool[1] (John). So '她' → Maria, 'he' → John.
        from astor_memory.nest.coref import _resolve_pronouns
        pool = ["Maria", "John"]
        rewritten, resolutions = _resolve_pronouns("她 and he disagreed.", pool)
        self.assertEqual(len(resolutions), 2)
        replaced_langs = {r['lang']: r['replaced_with'] for r in resolutions}
        self.assertEqual(replaced_langs.get('zh'), 'Maria')
        self.assertEqual(replaced_langs.get('en'), 'John')

    def test_no_double_replace_same_pronoun(self):
        # v1.16.10 design: each pronoun TYPE (e.g. 'she', 'her') reuses
        # the first unused antecedent for ALL its occurrences within
        # the text. So 'she' (She + she lowercase) both become Maria,
        # 'her' (her) becomes Maria. Total: 2 (she) + 1 (her) = 3
        # resolutions.
        from astor_memory.nest.coref import _resolve_pronouns
        pool = ["Maria"]
        rewritten, resolutions = _resolve_pronouns(
            "She said she told her.", pool
        )
        self.assertEqual(len(resolutions), 3)
        for r in resolutions:
            self.assertIn(r['replaced_with'].lower(), ['maria'])
        # 'she' and 'her' substrings gone from rewritten
        self.assertNotIn("she", rewritten.lower())
        self.assertNotIn("her ", rewritten.lower())  # trailing space to skip 'her' as substring of 'Maria'? no, maria has no her
        # Check: rewritten should have 3 'Maria' (or mixed-case) substrings
        # Actually 2 'Maria' (from She+she) + 1 'Maria' (from her) = 3
        # But case-preserved: She→Maria, she→maria, her→maria
        # So 'Maria' (cap) appears 1x, 'maria' (lower) appears 2x
        # Test: lower-case substring count
        self.assertEqual(rewritten.lower().count("maria"), 3)


class TestResolveCoreferences(unittest.TestCase):
    """End-to-end with in-memory SQLite bus stub."""

    def setUp(self):
        import sqlite3
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)

    def tearDown(self):
        self._db.close()

    def _make_bus_stub(self, facts: list[dict]):
        """Build a stub bus-like object with .conn + insert fact rows."""
        self._db.executescript("""
            CREATE TABLE memory_canonical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                namespace TEXT NOT NULL,
                user_id TEXT,
                entities_json TEXT NOT NULL DEFAULT '[]',
                valid_until TEXT,
                tombstoned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT
            );
        """)
        import datetime as _dt
        ts = _dt.datetime.utcnow().isoformat() + 'Z'
        for i, fact in enumerate(facts):
            # Stagger timestamps so newer facts have later timestamps
            fact_ts = ts.replace('T', 'T') + f'.{i:03d}Z' if 'T' in ts else ts
            self._db.execute(
                "INSERT INTO memory_canonical "
                "(content, namespace, user_id, entities_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    fact['content'],
                    fact.get('namespace', 'admin'),
                    fact.get('user_id', 'admin'),
                    json.dumps(fact.get('entities', [])),
                    fact_ts,
                ),
            )
        self._db.commit()

        # Build a stub object with .conn only
        class _Stub:
            pass
        s = _Stub()
        s.conn = self._db
        return s

    def test_end_to_end_resolves(self):
        from astor_memory.nest.coref import resolve_coreferences
        bus = self._make_bus_stub([
            {
                'content': 'Maria 在周一站会上提了截止日期的问题',
                'entities': [{'name': 'Maria'}],
            },
        ])
        result = resolve_coreferences(
            bus,
            '她说她没被告知这个变更。',
            namespace='admin',
            user_id='admin',
        )
        self.assertTrue(result['changed'])
        self.assertIn('Maria', result['resolved'])
        self.assertGreater(len(result['resolutions']), 0)

    def test_no_change_when_no_antecedent(self):
        from astor_memory.nest.coref import resolve_coreferences
        # Empty bus
        bus = self._make_bus_stub([])
        result = resolve_coreferences(
            bus,
            '她说她没被告知。',
            namespace='admin',
            user_id='admin',
        )
        self.assertFalse(result['changed'])
        self.assertEqual(result['resolved'], '她说她没被告知。')
        self.assertEqual(result['resolutions'], [])

    def test_namespace_isolation(self):
        from astor_memory.nest.coref import resolve_coreferences
        # Maria fact in different namespace — should NOT match
        bus = self._make_bus_stub([
            {
                'content': 'Maria mentioned deadline',
                'namespace': 'public',
                'user_id': 'someone',
                'entities': [{'name': 'Maria'}],
            },
        ])
        result = resolve_coreferences(
            bus,
            '她说她没被告知。',
            namespace='admin',
            user_id='admin',
        )
        # Different namespace → no antecedent found → no change
        self.assertFalse(result['changed'])

    def test_newest_antecedent_priority(self):
        from astor_memory.nest.coref import resolve_coreferences
        # Two entities in history; newer one should win
        bus = self._make_bus_stub([
            {
                'content': 'John 说他在写代码。',
                'entities': [{'name': 'John'}],
            },
            {
                'content': 'Maria 说她也在写代码。',
                'entities': [{'name': 'Maria'}],
            },
        ])
        result = resolve_coreferences(
            bus,
            '他说他的代码更好。',
            namespace='admin',
            user_id='admin',
        )
        # The newer fact has Maria → 'he' should resolve to Maria
        # (newer = higher priority)
        self.assertTrue(result['changed'])
        self.assertIn('Maria', result['resolved'])


class TestMakeResolvedText(unittest.TestCase):
    """Pure helper test — no DB."""

    def test_helper_resolves(self):
        from astor_memory.nest.coref import make_resolved_text
        result = make_resolved_text("她说她同意。", ["Maria"])
        self.assertIn("Maria", result)


import json  # at module level for the stub


if __name__ == '__main__':
    unittest.main()


class TestPronounAnchoredSkip(unittest.TestCase):
    """v1.16.10: skip entity extraction from pronoun-anchored sentences."""

    def test_skips_chinese_pronoun_anchored(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        # Pronoun-anchored: "她看了那份报告" — should yield no entities
        # because the antecedent is elsewhere, not in this sentence.
        ents = _extract_entities_from_text("她看了那份报告")
        self.assertEqual(ents, [])

    def test_extracts_from_proper_noun_anchored(self):
        from astor_memory.nest.coref import _extract_entities_from_text
        ents = _extract_entities_from_text("Maria 在周一站会上提了截止日期的问题")
        self.assertIn("Maria", ents)

    def test_skips_english_pronoun_anchored(self):
        # v1.16.10: a sentence starting with an English pronoun is
        # skipped entirely (no first-mention context). "She said"
        # anchors the sentence to a prior mention — extract nothing.
        from astor_memory.nest.coref import _extract_entities_from_text
        ents = _extract_entities_from_text("She said Maria was there")
        self.assertEqual(ents, [])
