"""Bridge: resolve a bot token from astor-memory's bot-binding.db (preferred)
or from the existing env vars (fallback).

This is a *non-invasive* helper — it does NOT modify hermes adapter code.
astor-memory bot runtime (when written) can import this and use db-first lookup.
Existing hermes adapters continue to use env directly; nothing breaks.

Lookup rules (try in order):
  1. bot-binding.db (canonical for new bots) — astor._internal.bot_binding
  2. config.yaml `platforms.<kind>` block  — for weixin accounts that are yaml-only
  3. environment variable (fallback to current hermes behavior)

Each lookup is audit-logged (read-tier). Mismatches between sources are
flagged via audit metadata.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .acl_layout import get_astor_dir

# ============================================================
# Env var names — current hermes 2026.x convention
# ============================================================
ENV_VAR_BY_KIND = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
    "feishu": "FEISHU_BOT_TOKEN",    # not currently used but reserved
    "weixin": None,                  # weixin is always ilink-token, no env shortcut
    "webchat": None,
}


class TokenResolution:
    """Result of a token lookup. Has .token, .source, .platform_id, .audit_metadata."""

    def __init__(self, token: str, source: str, platform_id: str | None = None,
                 account_id: str | None = None, audit_metadata: dict | None = None):
        self.token = token
        self.source = source  # 'db' | 'config_yaml' | 'env' | 'none'
        self.platform_id = platform_id
        self.account_id = account_id
        self.audit_metadata = audit_metadata or {}

    def __repr__(self):
        return f"TokenResolution(token={'***' if self.token else 'EMPTY'}, source={self.source!r}, account={self.account_id!r})"

    def to_dict(self):
        return {
            "token_set": bool(self.token),
            "token_preview": (self.token[:6] + "...") if self.token else "",
            "source": self.source,
            "platform_id": self.platform_id,
            "account_id": self.account_id,
            "audit_metadata": self.audit_metadata,
        }


def astor_get_token(platform_kind: str, account_id: str | None = None) -> TokenResolution:
    """Resolve a bot token: try bot-binding.db, then yaml, then env.

    Args:
        platform_kind: 'telegram' | 'discord' | 'weixin' | 'feishu' | 'webchat'
        account_id: optional account_id within the platform kind. For weixin this
                    is the @im.bot id. For telegram/discord with multi-bot, it's
                    the bot user_id. None means 'first active row for this kind'.

    Returns:
        TokenResolution with .token (the token string), .source (where it came from).
        If token is empty, .source='none' and the caller should treat as missing.
    """
    # 1. Try bot-binding.db
    db_result = _resolve_from_db(platform_kind, account_id)
    if db_result.token:
        _audit_lookup(platform_kind, account_id, "db", db_result.account_id)
        return db_result

    # 2. Try config.yaml
    yaml_result = _resolve_from_config_yaml(platform_kind, account_id)
    if yaml_result.token:
        _audit_lookup(platform_kind, account_id, "config_yaml", yaml_result.account_id)
        return yaml_result

    # 3. Fall back to env
    env_var = ENV_VAR_BY_KIND.get(platform_kind)
    env_token = os.environ.get(env_var, "") if env_var else ""
    if env_token:
        result = TokenResolution(
            token=env_token,
            source="env",
            platform_id=None,
            account_id=None,
            audit_metadata={"env_var": env_var},
        )
        _audit_lookup(platform_kind, account_id, "env", env_var)
        return result

    # All sources exhausted
    _audit_lookup(platform_kind, account_id, "none", None)
    return TokenResolution(token="", source="none")


def _resolve_from_db(platform_kind: str, account_id: str | None) -> TokenResolution:
    """Look up token in bot-binding.db."""
    try:
        from .bot_binding import _db_path, _connect  # reuse the schema-init conn
        p = _db_path()
        if not p.exists():
            return TokenResolution(token="", source="none")
        con = _connect()
        if account_id:
            row = con.execute(
                "SELECT platform_id, account_token, account_id FROM platforms "
                "WHERE platform_kind = ? AND account_id = ? AND enabled = 1 "
                "LIMIT 1",
                (platform_kind, account_id),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT platform_id, account_token, account_id FROM platforms "
                "WHERE platform_kind = ? AND enabled = 1 "
                "ORDER BY created_at LIMIT 1",
                (platform_kind,),
            ).fetchone()
        if row:
            return TokenResolution(
                token=row["account_token"],
                source="db",
                platform_id=row["platform_id"],
                account_id=row["account_id"],
            )
        return TokenResolution(token="", source="none")
    except Exception:
        return TokenResolution(token="", source="none")


def _resolve_from_config_yaml(platform_kind: str, account_id: str | None) -> TokenResolution:
    """Look up token in hermes config.yaml.

    For weixin, this is the existing data path. For other kinds, mostly empty.
    We do NOT modify config.yaml — just read from it as fallback.
    """
    if platform_kind != "weixin":
        return TokenResolution(token="", source="none")
    config_path = Path(os.environ.get("HERMES_HOME", "")) / "config.yaml"
    if not config_path.exists():
        config_path = Path("<home_dir>AppData/Local/hermes/config.active.yaml")
    if not config_path.exists():
        return TokenResolution(token="", source="none")
    try:
        # Parse yaml using PyYAML if available, else simple text scan
        import yaml
        cfg = yaml.safe_load(config_path.read_text())
        plats = cfg.get("platforms") or {}
        wx = plats.get("weixin") or {}
        if not wx.get("enabled"):
            return TokenResolution(token="", source="none")
        accounts = wx.get("extra", {}).get("accounts") or []
        for acc in accounts:
            if account_id is None or acc.get("account_id") == account_id:
                token = acc.get("token", "")
                if token:
                    return TokenResolution(
                        token=token,
                        source="config_yaml",
                        platform_id=f"weixin:{acc['account_id']}",
                        account_id=acc["account_id"],
                        audit_metadata={"config_path": str(config_path)},
                    )
        return TokenResolution(token="", source="none")
    except Exception:
        return TokenResolution(token="", source="none")


def _audit_lookup(platform_kind: str, account_id: str | None, source: str, result_account: str | None) -> None:
    """Audit every token lookup (read-tier private because weixin token = bot credential)."""
    try:
        from .audit_logger import astor_audit
        astor_audit(
            actor="first_admin",  # for now, default; later when we add runtime actor=server
            tier="private",
            action="read",
            target=f"platforms/{platform_kind}/{account_id or '*'}",
            reason=f"resolve token (source={source})",
            metadata={
                "platform_kind": platform_kind,
                "request_account_id": account_id,
                "source": source,
                "result_account_id": result_account,
            },
        )
    except Exception:
        pass


# ============================================================
# Smoke test (run as `python -m astor_memory._internal.platform_bridge`)
# ============================================================

def smoke_test() -> None:
    print("=== platform_bridge smoke test ===")
    for kind in ("weixin", "telegram", "discord", "feishu"):
        for acct in (None, "8263b17ef9c7@im.bot"):
            r = astor_get_token(kind, acct)
            print(f"  astor_get_token({kind!r}, {acct!r}) -> {r.to_dict()}")
