"""
Forge: high-level fact extraction orchestrator.

Combines regex + LLM extraction with write-time dedup.
"""

from __future__ import annotations

from .extractor import extract_facts, FactCandidate, ExtractMode
from .llm_extract import llm_extract

__all__ = ["extract_facts", "FactCandidate", "ExtractMode", "llm_extract"]
