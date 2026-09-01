"""v1.10.9 (2026-08-27): pass `doc_timestamp` through astor_extract_facts
and its callers so the LLM extractor can resolve relative time phrases
('yesterday', 'last week') against the conversation's anchor date.

The previous path lost temporal context: extractor saw the raw text
'Caroline went to the LGBTQ support group yesterday' but had no way to
know which calendar date 'yesterday' maps to. By passing `event.ts`
through to the LLM prompt and the relative-date post-processor, we get
accurate absolute event_date (e.g. '2023-05-07') without changing any
DB schema or re-ingesting existing data.
"""
import re
import sys
from pathlib import Path

ROOT = Path(r"<source_dir>astor_memory")
sys.path.insert(0, str(ROOT.parent))

# 1. Patch forge/extractor.py: add doc_timestamp kwarg to astor_extract_facts
ext = (ROOT / "forge" / "extractor.py").read_text(encoding="utf-8")

old_sig = '''def astor_extract_facts(
    text: str,
    mode: AstorExtractMode = 'auto',
    *,
    tier: str = 'public',
    user_id: str | None = None,
    actor: str = 'system',
    why: str | None = None,
    outcome: Literal['success', 'error', 'neutral'] = 'neutral',
) -> list[AstorFact]:'''
new_sig = '''def astor_extract_facts(
    text: str,
    mode: AstorExtractMode = 'auto',
    *,
    tier: str = 'public',
    user_id: str | None = None,
    actor: str = 'system',
    why: str | None = None,
    outcome: Literal['success', 'error', 'neutral'] = 'neutral',
    doc_timestamp: str | None = None,
) -> list[AstorFact]:'''
assert old_sig in ext, "astor_extract_facts signature not found"
ext = ext.replace(old_sig, new_sig, 1)

# Insert relative-date post-process step right after the `facts` list is
# populated (just before the capture_intent block). We use the relative_date
# module we added previously.
old_post = '''    if facts and (why or outcome != 'neutral'):
        for f in facts:
            if f.tags is None:
                f.tags = []'''
new_post = '''    # v1.10.9: anchor-based relative-date resolution. If the caller passed
    # a document timestamp (e.g. event.ts for /v1/write), normalize all
    # 'yesterday/last week' references into absolute YYYY-MM-DD so temporal
    # queries can match them later.
    if facts and doc_timestamp:
        try:
            from .relative_date import resolve_relative_dates_batch
            dict_facts = [f.__dict__ for f in facts]
            resolved = resolve_relative_dates_batch(dict_facts, doc_timestamp[:10])
            for f, src in zip(facts, resolved):
                if src.get("event_date") and not f.event_date:
                    f.event_date = src.get("event_date")
                if src.get("event_date_precision") and f.event_date_precision == "none":
                    f.event_date_precision = src.get("event_date_precision")
        except Exception:
            pass
    if facts and (why or outcome != 'neutral'):
        for f in facts:
            if f.tags is None:
                f.tags = []'''
assert old_post in ext, "post block not found"
ext = ext.replace(old_post, new_post, 1)

# 2. Also pass anchor to the LLM branch via a system note when available.
old_llm = '''        elif mode == 'llm':
            # LLM extract (lazy import to avoid loading requests when needed)
            from .llm_extract import astor_llm_extract
            # v1.10.8: astor_llm_extract may internally fall back to regex if
            # all providers fail. We need to record the REAL provider used, not
            # the requested one. astor_llm_extract now returns a tuple-like
            # (facts, actual_provider) to make this honest.
            from .llm_extract import astor_llm_extract_with_provider
            facts, actual_provider = astor_llm_extract_with_provider(text)'''
new_llm = '''        elif mode == 'llm':
            # LLM extract (lazy import to avoid loading requests when needed)
            from .llm_extract import astor_llm_extract
            # v1.10.8: astor_llm_extract may internally fall back to regex if
            # all providers fail. We need to record the REAL provider used, not
            # the requested one. astor_llm_extract now returns a tuple-like
            # (facts, actual_provider) to make this honest.
            from .llm_extract import astor_llm_extract_with_provider
            # v1.10.9: when caller passes doc_timestamp, prefix the text with
            # an explicit anchor marker so the LLM resolves 'yesterday/last
            # week' against the correct calendar date.
            _text_for_llm = text
            if doc_timestamp:
                _text_for_llm = (
                    f"[Doc timestamp: {doc_timestamp[:10]}] Treat this as 'today' "
                    f"when resolving 'yesterday/last week/3 days ago' etc.\n\n"
                    f"{text}"
                )
            facts, actual_provider = astor_llm_extract_with_provider(_text_for_llm)'''
assert old_llm in ext, "llm branch not found"
ext = ext.replace(old_llm, new_llm, 1)

(ROOT / "forge" / "extractor.py").write_text(ext, encoding="utf-8")
print("extractor.py patched: doc_timestamp parameter + LLM anchor + relative-date post-process")

# 3. Patch server.py: pass event_ts into astor_extract_facts calls
srv = (ROOT / "server.py").read_text(encoding="utf-8")

# Call site 1: /v1/write
old_w = '''        facts = forge.astor_extract_facts(
            text, mode=mode, tier=tier,
            user_id=bus_user_id if tier == 'private' else None,
            actor='rest_api',
        )'''
new_w = '''        facts = forge.astor_extract_facts(
            text, mode=mode, tier=tier,
            user_id=bus_user_id if tier == 'private' else None,
            actor='rest_api',
            # v1.10.9: pass event ts as doc anchor so 'yesterday' etc. resolves
            doc_timestamp=str(event.ts) if hasattr(event, 'ts') else None,
        )'''
assert old_w in srv, "/v1/write call site not found"
srv = srv.replace(old_w, new_w, 1)

# Call site 2: source mirror
old_s = '''                src_facts = astor_forge().astor_extract_facts(
                    text, mode=mode, tier='source',
                    user_id=None, actor='rest_api.mirror',
                )'''
new_s = '''                src_facts = astor_forge().astor_extract_facts(
                    text, mode=mode, tier='source',
                    user_id=None, actor='rest_api.mirror',
                    doc_timestamp=str(src_event.ts) if hasattr(src_event, 'ts') else None,
                )'''
assert old_s in srv, "source mirror call site not found"
srv = srv.replace(old_s, new_s, 1)

(ROOT / "server.py").write_text(srv, encoding="utf-8")
print("server.py patched: pass event.ts into both /v1/write and source mirror")

# 4. Validate syntax
import ast
for p in [ROOT / "forge" / "extractor.py", ROOT / "server.py"]:
    ast.parse(p.read_text(encoding="utf-8"))
    print(f"OK: {p.relative_to(ROOT.parent)}")
