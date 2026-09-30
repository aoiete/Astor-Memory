"""skills.py — v1.16.13 (2026-09-30)

MemSkill-inspired skill bank abstraction for astor memory operations.

Source: wechat article "MemSkill：Agent 记忆不再手工写死！
让记忆管理技能自己进化" (mp.weixin.qq.com/s/RmbEJ28DNQ4bI-olYTX5mA,
NTU 2026, 4 SOTA benchmarks).

MemSkill's core insight: agent memory operations (extract/inject/
update/forget) should be ABSTRACTED as callable SKILLS in a global
Skill Bank, not hardcoded in the server flow. Skills are written in
natural language templates + small Python implementations. Controller
selects skills per-context; Executor runs them; Designer evolves
the bank over time (out of scope for v1.16.13).

For v1.16.13 we ship:
1. A Skill dataclass (name + description + run callable + tags)
2. A SkillBank registry with auto-load of built-in skills
3. A simple Controller (selects skill by tag match)
4. Four built-in skills wrapping the existing operations:
   - coref_resolve_skill  (wraps v1.16.10 coref)
   - path_score_skill     (wraps v1.16.11 path_score)
   - bitemporal_invalidate_skill (wraps v1.16.8 invalidation)
   - episode_link_skill   (wraps v1.16.12 episode linking)
5. /v1/skill endpoint: list, invoke, inspect

Why ship skill abstraction (vs running ops directly):
- Discoverability: caller asks "what skills exist?" instead of
  reading source.
- Composable: caller can chain skills via the Controller.
- Future Designer (v1.17.x): the bank IS the thing that evolves.
- Tests: each skill isolated, can mock + unit-test.

Why not full MemSkill (no Designer):
- Designer requires a training loop + reward signal + offline
  eval harness. We have astor-eval-harness-patterns but a real
  Designer is a multi-week project. Ship the abstraction first.
- Skill Bank is the foundational artifact; Designer/Evolver is
  the upper layer.

Cost: <1ms per skill invocation (just function call). Skill
selection by tag: O(N) over registered skills (~10 for v1.16.13).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Any


@dataclass
class Skill:
    """A callable memory skill. The unit of the Skill Bank.

    Attributes:
        name: short snake_case id (e.g. 'coref_resolve').
        description: human-readable description (1-2 sentences).
        tags: list of tag strings for Controller matching. E.g.
            ['memory', 'coref', 'preprocess', 'write_hook'].
        run: callable that takes a context dict and returns a result
            dict. Must NOT raise on normal errors — return
            {'ok': False, 'error': 'msg'} instead.
        version: skill schema version, default 1.
    """
    name: str
    description: str
    tags: list[str]
    run: Callable[[dict], dict]
    version: int = 1

    def invoke(self, context: dict) -> dict:
        """Run this skill with a context dict.

        Wraps the callable in a try/except so a buggy skill can't
        break the calling flow (e.g. /v1/write hook).
        """
        t0 = time.monotonic()
        try:
            result = self.run(context)
            if not isinstance(result, dict):
                result = {'ok': True, 'value': result}
            result.setdefault('ok', True)
            result.setdefault('skill', self.name)
            result['elapsed_ms'] = round((time.monotonic() - t0) * 1000, 2)
            return result
        except Exception as e:
            return {
                'ok': False,
                'skill': self.name,
                'error': f'{type(e).__name__}: {e}',
                'elapsed_ms': round((time.monotonic() - t0) * 1000, 2),
            }


class SkillBank:
    """Registry of Skills. Singleton — one bank per server process."""

    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add or replace a skill by name."""
        if not skill.name:
            raise ValueError("skill.name required")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> bool:
        return self._skills.pop(name, None) is not None

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self, tag: str | None = None) -> list[dict]:
        """List skill summaries (name + description + tags)."""
        out = []
        for s in self._skills.values():
            if tag is None or tag in s.tags:
                out.append({
                    'name': s.name,
                    'description': s.description,
                    'tags': list(s.tags),
                    'version': s.version,
                })
        return out

    def select(self, tags: list[str], require_all: bool = False) -> list[Skill]:
        """Controller-style skill selection.

        Args:
            tags: tag list to match.
            require_all: if True, ALL tags must match. If False
                (default), ANY tag matches.
        Returns:
            list of matching Skills (may be empty).
        """
        out = []
        for s in self._skills.values():
            if require_all:
                if all(t in s.tags for t in tags):
                    out.append(s)
            else:
                if any(t in s.tags for t in tags):
                    out.append(s)
        return out

    def invoke(self, name: str, context: dict) -> dict:
        """Invoke a skill by name with a context dict."""
        s = self.get(name)
        if s is None:
            return {'ok': False, 'error': f'skill not found: {name}'}
        return s.invoke(context)

    def size(self) -> int:
        return len(self._skills)


# Singleton bank + auto-load of built-in skills.
_BANK: SkillBank | None = None


def get_bank() -> SkillBank:
    """Get the singleton SkillBank, lazy-initializing with built-in skills."""
    global _BANK
    if _BANK is None:
        _BANK = SkillBank()
        _load_builtin_skills(_BANK)
    return _BANK


def reset_bank() -> None:
    """Reset the singleton (for tests)."""
    global _BANK
    _BANK = None


# ---------------------------------------------------------------------------
# Built-in skills — wrap existing astor memory operations
# ---------------------------------------------------------------------------

def register_skill(
    name: str | None = None,
    description: str = '',
    tags: list[str] | None = None,
    version: int = 1,
):
    """Decorator: auto-register a function as a Skill.

    Usage:
        @register_skill(tags=['memory', 'write_hook'])
        def my_skill(ctx: dict) -> dict:
            ...

    The function name becomes the skill name (unless `name` is
    explicitly given). The bank is the singleton — registration
    happens on import. Tests should call reset_bank() before
    re-importing modules.

    This is the v1.16.14 replacement for v1.16.13's manual
    `_load_builtin_skills(bank)` pattern. Decorator-based registration
    is the standard idiom in skill systems (LangChain tools,
    FastAPI endpoints, etc.) — discoverable, low-boilerplate.
    """
    tags = tags or []

    def _decorator(func):
        skill_name = name or func.__name__
        skill_desc = description or (func.__doc__ or '').strip().split('\n')[0]
        s = Skill(
            name=skill_name,
            description=skill_desc,
            tags=list(tags),
            run=func,
            version=version,
        )
        # Lazy: avoid circular ref (get_bank → _load_builtin_skills → decorator
        # → get_bank). Direct singleton access.
        global _BANK
        if _BANK is None:
            _BANK = SkillBank()
        _BANK.register(s)
        return func

    return _decorator


def _skill_coref_resolve(ctx: dict) -> dict:
    """Resolve pronouns in `text` against recent facts.

    Wraps v1.16.10 nest/coref.py. Opt-in via Skill Bank because
    it's an extra DB read per write.
    """
    text = ctx.get('text') or ''
    if not text:
        return {'changed': False, 'resolutions': []}
    bus = ctx.get('bus')
    namespace = ctx.get('namespace') or 'public'
    user_id = ctx.get('user_id') or ''
    window = int(ctx.get('window') or 10)
    if bus is None:
        return {'changed': False, 'resolutions': [], 'note': 'no bus in context'}
    # Lazy import to avoid circular at module load
    from .coref import resolve_coreferences
    res = resolve_coreferences(bus, text, namespace=namespace,
                                user_id=user_id, antecedent_window=window)
    return res


@register_skill(
    description='Resolve pronouns in text against recent facts (v1.16.10 heuristic coref).',
    tags=['memory', 'coref', 'preprocess', 'write_hook', 'nlp'],
)
def coref_resolve(ctx: dict) -> dict:
    return _skill_coref_resolve(ctx)


def _skill_path_score(ctx: dict) -> dict:
    """Compute path-based graph score for a candidate.

    Wraps v1.16.11 nest/path_score.py.
    """
    query = ctx.get('query') or ''
    anchor = ctx.get('anchor') or {}
    neighbors = ctx.get('neighbors') or []
    if not query or not anchor:
        return {'direct': 0.0, 'path': 0.0, 'boost': 0.0,
                'chain': [], 'depth_used': 0, 'note': 'empty query or anchor'}
    from .path_score import path_score_for_fact
    return path_score_for_fact(
        query=query,
        anchor=anchor,
        neighbor_facts=neighbors,
        max_depth=int(ctx.get('max_depth') or 2),
        decay=float(ctx.get('decay') or 0.6),
        cap=float(ctx.get('cap') or 0.30),
    )


@register_skill(
    description='Compute path-based graph score for a candidate fact (v1.16.11 M-flow).',
    tags=['memory', 'graph', 'scoring', 'read_hook', 'rank'],
)
def path_score(ctx: dict) -> dict:
    return _skill_path_score(ctx)


def _skill_bitemporal_invalidate(ctx: dict) -> dict:
    """Invalidate a fact by id (bi-temporal invalidation).

    Wraps v1.16.8 bus/bitemporal.py.
    """
    fact_id = ctx.get('fact_id')
    if fact_id is None:
        return {'ok': False, 'error': 'fact_id required'}
    bus = ctx.get('bus')
    if bus is None:
        return {'ok': False, 'error': 'no bus in context'}
    reason = ctx.get('reason') or 'manual'
    from ..bus.bitemporal import invalidate_fact
    return invalidate_fact(bus, int(fact_id), reason=reason)


@register_skill(
    description='Invalidate a fact by id (v1.16.8 bi-temporal lifecycle).',
    tags=['memory', 'bitemporal', 'invalidate', 'write_hook', 'lifecycle'],
)
def bitemporal_invalidate(ctx: dict) -> dict:
    return _skill_bitemporal_invalidate(ctx)


def _skill_episode_link(ctx: dict) -> dict:
    """Link a derived fact_id to an episode (L0 evidence layer).

    Wraps v1.16.12 nest/episodes.py.
    """
    episode_id = ctx.get('episode_id')
    fact_id = ctx.get('fact_id')
    if episode_id is None or fact_id is None:
        return {'ok': False, 'error': 'episode_id and fact_id required'}
    conn = ctx.get('conn')
    if conn is None:
        return {'ok': False, 'error': 'no conn in context'}
    from .episodes import link_fact_to_episode
    link_fact_to_episode(conn, int(episode_id), int(fact_id))
    return {'ok': True, 'episode_id': int(episode_id), 'fact_id': int(fact_id)}


@register_skill(
    description='Link a derived fact to an L0 episode for evidence traceability (v1.16.12).',
    tags=['memory', 'episode', 'l0', 'evidence', 'write_hook'],
)
def episode_link(ctx: dict) -> dict:
    return _skill_episode_link(ctx)


def _load_builtin_skills(bank: SkillBank) -> None:
    """Register the built-in skills that wrap existing astor operations.

    DEPRECATED (v1.16.14): kept as no-op for backward compat.
    Skills now self-register via @register_skill decorator at module
    import time. The 4 built-in skills are auto-registered when
    nest/skills.py is imported.
    """
    return None


def invoke_chain(
    skill_names: list[str],
    context: dict,
    bank: SkillBank | None = None,
    stop_on_error: bool = False,
) -> list[dict]:
    """Invoke a chain of skills in order, threading context.

    Args:
        skill_names: list of skill names to invoke in order.
        context: starting context dict. Each skill's result is
            MERGED into the context (shallow update) before the
            next skill runs, so skills can chain by reading each
            other's outputs (e.g. coref_resolve rewrites ctx['text'],
            then the next skill sees the rewritten text).
        bank: SkillBank (defaults to singleton).
        stop_on_error: if True, abort chain on first skill failure.
            If False (default), continue with next skill regardless.

    Returns:
        list of result dicts (one per invoked skill), in order.
    """
    bank = bank or get_bank()
    ctx = dict(context)  # shallow copy
    results: list[dict] = []
    for name in skill_names:
        s = bank.get(name)
        if s is None:
            results.append({'ok': False, 'skill': name,
                            'error': f'skill not found: {name}'})
            if stop_on_error:
                break
            continue
        r = s.invoke(ctx)
        results.append(r)
        if not r.get('ok') and stop_on_error:
            break
        # Thread outputs into context for next skill
        for k, v in r.items():
            if k not in ('ok', 'skill', 'elapsed_ms', 'error'):
                ctx[k] = v
    return results


# ---------------------------------------------------------------------------
# Controller — small helper for chain selection
# ---------------------------------------------------------------------------

def controller_select(
    requested: list[str],
    available_tags: list[str] | None = None,
    bank: SkillBank | None = None,
) -> list[str]:
    """Pick skill names matching the request.

    Args:
        requested: list of skill names OR tags. Names take precedence.
        available_tags: optional tag filter — only skills matching
            AT LEAST ONE of these tags are considered.
        bank: SkillBank (defaults to singleton).

    Returns:
        list of skill names to invoke in order. Names that don't
        exist are dropped silently (Controller's job to filter).
    """
    bank = bank or get_bank()
    if available_tags:
        candidates = bank.select(available_tags, require_all=False)
        names = {s.name for s in candidates}
    else:
        names = {d['name'] for d in bank.list()}
    out = []
    for r in requested:
        if r in names:
            out.append(r)
        else:
            # r might be a tag — find skills with that tag
            matches = bank.select([r], require_all=False)
            for m in matches:
                if m.name not in out:
                    out.append(m.name)
    return out


__all__ = [
    'Skill',
    'SkillBank',
    'get_bank',
    'reset_bank',
    'controller_select',
    'register_skill',
    'invoke_chain',
]
