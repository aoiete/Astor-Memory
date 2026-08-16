"""
Configuration management.

Per Plan § Config:
- Priority: CLI flag > env > config.yaml > defaults
- Config file: ~/.astor/config.yaml
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ASTOR_DIR_NAME = '~/.astor'
DEFAULT_CONFIG_PATH_NAME = 'config.yaml'
DEFAULT_BUS_PATH_NAME = 'astor_bus.db'
DEFAULT_FORGE_PATH_NAME = 'astor_forge.db'
DEFAULT_NEST_PATH_NAME = 'astor_nest.db'


def get_default_astor_dir() -> Path:
    """Get default astor dir (reads env at call time)."""
    return Path(os.environ.get('ASTOR_DIR', DEFAULT_ASTOR_DIR_NAME)).expanduser()


def get_default_config_path() -> Path:
    """Get default config path (reads env at call time)."""
    return get_default_astor_dir() / DEFAULT_CONFIG_PATH_NAME


def get_default_bus_path() -> Path:
    """Get default bus DB path (events + canonical facts + audit_log)."""
    return get_default_astor_dir() / DEFAULT_BUS_PATH_NAME


def get_default_forge_path() -> Path:
    """Get default forge DB path (LLM extraction cache)."""
    return get_default_astor_dir() / DEFAULT_FORGE_PATH_NAME


def get_default_nest_path() -> Path:
    """Get default nest DB path (vector embeddings)."""
    return get_default_astor_dir() / DEFAULT_NEST_PATH_NAME


def load_config(config_path: Path | None = None) -> dict:
    """Load config from YAML file. Returns defaults if file missing."""
    path = config_path or get_default_config_path()
    if not path.exists():
        return _default_config()
    try:
        import yaml
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
    except ImportError:
        # No yaml lib; use minimal parser
        user_cfg = _minimal_yaml_parse(path)
    return _merge_config(_default_config(), user_cfg)


def _default_config() -> dict:
    """Default config values per Plan."""
    return {
        'astor_dir': str(DEFAULT_ASTOR_DIR_NAME).replace('~', str(Path.home())),
        'astor_bus_path': str(get_default_bus_path()),
        'astor_forge_path': str(get_default_forge_path()),
        'astor_nest_path': str(get_default_nest_path()),
        'embedding': {
            'model': None,  # auto-detect based on RAM
        },
        'extract_mode': 'auto',  # 'auto' | 'none' | 'regex' | 'llm'
        'rate_limits': {
            'recall_per_hour': 60,
            'write_per_hour': 100,
            'verify_per_hour': 3,
            'compact_per_hour': 2,
            'soft_warn_threshold': 5,
            'abuse_degrade': True,
        },
        'memory_thresholds': {
            'alarm_mb': 800,
            'warning_mb': 1000,
            'oom_mb': 1200,
        },
        'lifecycle': {
            'compact_frequency_hours': 6,
            'recency_half_life_days': 30,
            'long_gap_thin_days': 30,
        },
        'dedup': {
            'enabled': True,
            'similarity_threshold': 0.95,
            'lookback_days': 7,
            'candidate_limit': 100,
            'auto_merge': True,
        },
        'recall': {
            'top_k': 5,
            'ranking_weights': {
                'similarity': 0.45,
                'recency': 0.2,
                'popularity': 0.1,
                'confidence': 0.1,
                'platform': 0.05,
                'pin': 0.05,
                'annotation': 0.05,
            },
        },
        'skill_timeouts': {
            'default_seconds': 30,
            'per_skill': {},
        },
        'admin_lock_path': str(get_default_astor_dir() / 'admin.lock'),
    }


def _merge_config(defaults: dict, user: dict) -> dict:
    """Merge user config into defaults (deep merge)."""
    merged = defaults.copy()
    for key, value in user.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def _minimal_yaml_parse(path: Path) -> dict:
    """Very minimal YAML parser (just key: value pairs)."""
    result: dict = {}
    stack = [result]
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip()
                if not line or line.startswith('#'):
                    continue
                indent = len(line) - len(line.lstrip())
                content = line.lstrip()
                if ':' not in content:
                    continue
                key, _, value = content.partition(':')
                key = key.strip()
                value = value.strip()
                if value == '':
                    new_dict: dict = {}
                    stack[-1][key] = new_dict
                    stack.append(new_dict)
                else:
                    stack[-1][key] = value
                    while len(stack) > indent // 2:
                        stack.pop()
    except Exception:
        pass
    return result


def save_config(config: dict, config_path: Path | None = None) -> None:
    """Save config to YAML file."""
    path = config_path or get_default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    except ImportError:
        _minimal_yaml_dump(config, path)


def _minimal_yaml_dump(config: dict, path: Path, indent: int = 0) -> None:
    """Very minimal YAML dump."""
    with open(path, 'w') as f:
        for key, value in config.items():
            if isinstance(value, dict):
                f.write(f'{"  " * indent}{key}:\n')
                _minimal_yaml_dump(value, path, indent + 1)
            else:
                f.write(f'{"  " * indent}{key}: {value}\n')


__all__ = [
    'get_default_astor_dir', 'get_default_config_path',
    'get_default_bus_path', 'get_default_forge_path', 'get_default_nest_path',
    'load_config', 'save_config',
]
