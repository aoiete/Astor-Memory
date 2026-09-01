src = r'<source_dir>astor_memory\server.py'
with open(src, encoding='utf-8') as f:
    text = f.read()

old = '''        # v1.10.9 (2026-08-27): multi-query expansion. If enabled, generate
        # 1-2 query paraphrases, run hybrid recall on each, and merge via
        # reciprocal rank fusion. This is the cheapest way to recover facts
        # whose long content contains a phrase the original query didn't
        # surface (e.g. "Caroline research adoption" -> "Caroline looked into
        # adoption agencies"). Cached for 10min.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '0') == '1':
            try:
                from .nest.query_expander import expand_query as _expq
                _query_variants = _expq(query)
            except Exception:
                pass'''

new = '''        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass'''

assert old in text, 'expansion block not found'
text = text.replace(old, new, 1)

import ast
ast.parse(text)
with open(src, 'w', encoding='utf-8') as f:
    f.write(text)
print('Synonym expansion default ON (no LLM cost)')
