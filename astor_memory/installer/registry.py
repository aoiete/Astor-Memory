"""
Agent tier classification + mode capability matrix.

Per Plan Insight 18:
- Tier A: agent has native priority hook (Claude Code / Cline / OpenCode)
- Tier B: agent code is patchable (Hermes / OpenClaw)
- Tier C: lexical ordering only (Cursor / Continue / Windsurf / Aider)
- Tier D: deprecated or unknown (Roo Code / Antigravity)

Each entry maps to an installer handler in astor_memory.installer.
"""

# Tier classification
AGENT_TIERS: dict[str, str] = {
    # Tier A: priority hook
    'claude-code': 'A',
    'cline': 'A',
    'opencode': 'A',
    # Tier B: patchable
    'hermes': 'B',
    'openclaw': 'B',
    # Tier C: coexist only
    'cursor': 'C',
    'continue': 'C',
    'windsurf': 'C',
    'aider': 'C',
    # Tier D: skip
    'roo-code': 'D',
    'antigravity': 'D',
}

# Mode capability per agent
# Format: agent_id -> {mode -> bool (supported)}
AGENT_MODE_SUPPORT: dict[str, dict[str, bool]] = {
    'claude-code': {'priority': True, 'coexist': True, 'replace': True, 'verify': True},
    'cline':       {'priority': True, 'coexist': True, 'replace': True, 'verify': True},
    'opencode':    {'priority': True, 'coexist': True, 'replace': True, 'verify': True},
    'hermes':      {'priority': True, 'coexist': True, 'replace': False, 'verify': True},
    'openclaw':    {'priority': True, 'coexist': True, 'replace': False, 'verify': True},
    'cursor':      {'priority': False, 'coexist': True, 'replace': False, 'verify': True},
    'continue':    {'priority': False, 'coexist': True, 'replace': False, 'verify': True},
    'windsurf':    {'priority': False, 'coexist': True, 'replace': False, 'verify': True},
    'aider':       {'priority': False, 'coexist': True, 'replace': False, 'verify': True},
    'roo-code':    {'priority': False, 'coexist': False, 'replace': False, 'verify': True},
    'antigravity': {'priority': False, 'coexist': False, 'replace': False, 'verify': True},
}

# All supported agents (Tier A/B/C — Tier D agents are excluded from CLI choices)
SUPPORTED_AGENTS = [k for k, t in AGENT_TIERS.items() if t in ('A', 'B', 'C')]

# Priority marker (universal, collaborative — not aggressive)
PRIORITY_MARKER = """\
## Memory source priority

When answering questions about user preferences, decisions, or past work:

1. **First:** Query Astor-Memory via `am_read` MCP tool (or `am read "<query>"` CLI).
2. **Then:** Cross-reference with conversation context and agent native memory.
3. **On conflict:** Prefer Astor-Memory output for facts older than the current session,
   prefer conversation context for facts introduced in this session.

Astor-Memory is the canonical store for cross-session facts.
Conversation context is canonical for this-session-only state.
"""


def astor_get_agent_tier(agent_id: str) -> str:
    """Return tier classification for an agent (A/B/C/D)."""
    return AGENT_TIERS.get(agent_id, 'D')


def astor_supports_mode(agent_id: str, mode: str) -> bool:
    """Return True if agent supports the given install mode."""
    return AGENT_MODE_SUPPORT.get(agent_id, {}).get(mode, False)


def astor_list_supported_agents() -> list[str]:
    """Return list of all agents that can be installed (Tier A/B/C)."""
    return list(SUPPORTED_AGENTS)


def astor_verify_agent(agent_id: str) -> dict:
    """Return dict describing agent's install capabilities."""
    tier = astor_get_agent_tier(agent_id)
    if tier == 'D':
        return {
            'agent': agent_id,
            'tier': tier,
            'supported': False,
            'reason': 'Agent is deprecated (Roo Code shutdown 2026-05) or has unknown API (Antigravity)',
            'modes_supported': [],
            'recommended_mode': None,
        }
    modes = [m for m, sup in AGENT_MODE_SUPPORT.get(agent_id, {}).items() if sup and m != 'verify']
    recommended = 'priority' if tier == 'A' else ('priority' if tier == 'B' and agent_id in ('hermes', 'openclaw') else 'coexist')
    return {
        'agent': agent_id,
        'tier': tier,
        'supported': True,
        'tier_meaning': {
            'A': 'Native priority hook (agent system prompt slot)',
            'B': 'Patchable (agent code visible, can be patched)',
            'C': 'Lexical ordering only (priority marker + filename prefix)',
        }.get(tier, ''),
        'modes_supported': modes,
        'recommended_mode': recommended,
    }


__all__ = [
    'AGENT_TIERS', 'AGENT_MODE_SUPPORT', 'SUPPORTED_AGENTS',
    'PRIORITY_MARKER',
    'astor_get_agent_tier', 'astor_supports_mode', 'astor_list_supported_agents',
    'astor_verify_agent',
]
