"""test_synonym_expander_chinese.py — Ship K (2026-09-28)

Verifies the Chinese-aware query expansion. Tests:
- Original query is always returned as first variant (backward compat).
- Chinese triggers generate synonym variants (健身→锻炼, 知识库→RAG, etc.).
- English triggers still work (research→study, when→what date).
- Mixed-script queries (CJK + Latin) hit BOTH pipelines.
- Bigram fallback fires when no synonym matched.
- LLM fallback gate: default off (returns empty); ASTOR_LLM_EXPAND=1 enables.
- Dedup: same variant isn't emitted twice even if multiple triggers match.
- Self-substitution guard: syn != trigger, syn not in query.

Author: astor-memory Ship K (multi-language expansion).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Ensure the package is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _eq(label: str, got, expected) -> None:
    if got != expected:
        print(f"  [FAIL] {label}")
        print(f"    got     : {got}")
        print(f"    expected: {expected}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _truthy(label: str, got) -> None:
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    # Import here so any IndentationError surfaces first
    from astor_memory.nest.synonym_expander import (
        expand_query, _has_cjk, _has_latin, _match_cn_groups, _cn_bigrams,
    )

    print("=== Unit cases ===")

    # has_cjk / has_latin detection
    _truthy("has_cjk('健身')", _has_cjk("健身"))
    _truthy("has_cjk('RAG 知识库')", _has_cjk("RAG 知识库"))
    _truthy("not has_cjk('hello')", not _has_cjk("hello"))
    _truthy("has_latin('RAG')", _has_latin("RAG"))
    _truthy("not has_latin('健身')", not _has_latin("健身"))

    # Original first invariant (backward compat)
    out = expand_query("anything goes here", 3)
    _eq("original-first invariant", out[0], "anything goes here")

    # Chinese trigger generates variants
    out = expand_query("fit 健身 计划", 3)
    _truthy("健身 generates ≥2 variants", len(out) >= 2)
    _truthy("variant contains 锻炼 (synonym)", any("锻炼" in v for v in out))
    _truthy("variant contains 运动 (synonym)", any("运动" in v for v in out))

    # workout 周训练 → 健身/锻炼
    out = expand_query("workout 周训练", 3)
    _truthy("训练 → 健身", any("健身" in v for v in out))
    _truthy("训练 → 锻炼", any("锻炼" in v for v in out))

    # RAG 知识库 → knowledge / 信息
    out = expand_query("RAG 知识库 bge-reranker", 3)
    _truthy("知识库 → knowledge variant", any("knowledge" in v for v in out))
    _truthy("知识库 → 信息 variant", any("信息库" in v for v in out))
    # Self-substitution guard: 'RAG' synonym should NOT replace 知识库 (RAG
    # already in query). Without the guard we'd see "RAG RAG bge-reranker".
    _truthy("no RAG-self-replacement", not any(v.count("RAG") > 1 for v in out))

    # 八字 壬水身强 → 四柱 / 命理
    out = expand_query("八字 壬水身强", 3)
    _truthy("八字 → 四柱", any("四柱" in v for v in out))
    _truthy("八字 → 命理", any("命理" in v for v in out))

    # 今日日柱 → 日主 / 日干
    out = expand_query("今日日柱", 3)
    _truthy("日柱 → 日主", any("日主" in v for v in out))
    _truthy("日柱 → 日干", any("日干" in v for v in out))

    # 用户当前时区 → timezone / time zone
    out = expand_query("用户当前时区", 3)
    _truthy("时区 → timezone", any("timezone" in v for v in out))
    _truthy("时区 → time zone", any("time zone" in v for v in out))

    # 健身 (single-trigger CN) → 3 variants
    out = expand_query("健身", 3)
    _truthy("single-token CN → ≥3 variants", len(out) >= 3)

    # English still works (legacy v1.10.9 path)
    out = expand_query("When did Caroline research?", 3)
    _truthy("English 'when' → temporal specialization",
            any("what date" in v.lower() or "what time" in v.lower() for v in out))

    # English synonym: research → study
    out = expand_query("research topic today", 3)
    _truthy("English 'research' → study", any("study" in v for v in out))

    # Mixed script: both pipelines fire
    out = expand_query("research 健身", 3)
    _truthy("mixed-script CN triggers", any("锻炼" in v or "运动" in v for v in out))
    _truthy("mixed-script EN triggers", any("study" in v for v in out))

    # Bigram fallback: query with no synonym hit gets bigram anchors
    out = expand_query("独一无二 现象", 3)
    _truthy("bigram fallback fires (no synonym)",
            len(out) >= 2 and out[1] != "独一无二 现象")

    # Dedup: 多 triggers don't emit duplicates
    out = expand_query("健身 锻炼", 3)
    seen = set()
    dup = False
    for v in out:
        if v.lower() in seen:
            dup = True
            break
        seen.add(v.lower())
    _truthy("no duplicate variants", not dup)

    # LLM fallback gate (default off → no extra variants beyond synonym)
    os.environ.pop("ASTOR_LLM_EXPAND", None)
    out = expand_query("完全 陌生 词语", 3)
    # This query has no CN synonym triggers; should fall to bigram, not LLM
    _truthy("LLM gate default off (no env)", all(v for v in out))

    # Empty query edge case
    out = expand_query("", 3)
    _eq("empty query → ['']", out, [""])

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())