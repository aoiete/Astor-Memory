"""
astor-memory installer: per-agent priority negotiation framework.

Per Plan Insight 18 (v0.2):
- 4-tier agent classification (A: priority hook / B: patchable / C: coexist / D: skip)
- 4 install modes (priority / coexist / replace / verify)
- Universal priority marker (collaborative, not aggressive)
- 9 agents: Claude Code / Cline / OpenCode / Hermes / OpenClaw / Cursor / Continue / Windsurf / Aider

CLI signature:
  am install --ide=X --mode=Y
  - ide: claude-code | cline | opencode | hermes | openclaw | cursor | continue | windsurf | aider
  - mode: priority | coexist | replace | verify
"""
from .handlers import astor_install, INSTALLERS
from .registry import (
    AGENT_TIERS, AGENT_MODE_SUPPORT, SUPPORTED_AGENTS, PRIORITY_MARKER,
    astor_get_agent_tier, astor_supports_mode, astor_list_supported_agents,
    astor_verify_agent,
)

__all__ = [
    'astor_install', 'INSTALLERS',
    'AGENT_TIERS', 'AGENT_MODE_SUPPORT', 'SUPPORTED_AGENTS', 'PRIORITY_MARKER',
    'astor_get_agent_tier', 'astor_supports_mode', 'astor_list_supported_agents',
    'astor_verify_agent',
]
