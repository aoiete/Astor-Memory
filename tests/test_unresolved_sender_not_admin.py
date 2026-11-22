"""An unresolved bot sender must not read as the operator.

v1.17.84. The Hermes astor plugin forces tier=public when it cannot
resolve a sender to a known user -- that part was right. But it kept
the configured user_id, and the configured default is "admin":

    "user_id": os.environ.get("ASTOR_USER_ID", "admin"),

The server binds the caller from body.user/body.user_id BEFORE it
looks at the tier, so an unbound sender -- someone we could not
resolve to a known user -- was still sent as user="admin", bound as
admin, and got admin-only tiers.

What that reaches: the server's meta-recall consults the source tier
on a public read (the global-lessons path). The source tier is admin
+ system only, and it holds the operator's own R-class lessons -- the
ones that name the operator's own chat ids, paths and job ids.

On the operator's host, a public-tier recall returned a source-tier
lesson containing the operator's Telegram chat id. Not via a peer
(the peer share gate only sends 'commons' rows of the public tier)
and not via the public tier (the chat-id detector was fixed to keep
the public tier clean). It was this: an unresolved sender inheriting
the operator's identity.

The rule: "we don't know who this is" must not resolve to "this is
the operator". An unresolved sender reads as anonymous, and the ACL
decides what anonymous may see -- public tier only.

These tests pin that rule at the plugin boundary, and pin the server
side that makes it meaningful.
"""

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from astor_memory.server import _astor_resolve_actor  # noqa: E402


# --- the plugin's own boundary -------------------------------------------

def _plugin_path():
    """The plugin __init__.py. HERMES_AGENT_PATH wins, else the D-drive source."""
    candidates = []
    env = os.environ.get('HERMES_AGENT_PATH')
    if env:
        # HERMES_AGENT_PATH points at the hermes-agent tree; the plugin
        # lives under memory/astor_memory/ in the source layout.
        candidates.append(Path(env) / 'memory' / 'astor_memory' / '__init__.py')
        candidates.append(Path(env) / 'plugins' / 'memory' / 'astor_memory' / '__init__.py')
    candidates.append(
        Path('D:/AI/admin/hermes-config/hermes-agent/memory/astor_memory/__init__.py')
    )
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def _hermes_agent_path():
    """The hermes-agent tree the plugin imports `agent.*` from."""
    env = os.environ.get('HERMES_AGENT_PATH')
    if env and (Path(env) / 'agent' / 'memory_provider.py').exists():
        return Path(env)
    for c in (
        Path('C:/Users/TheNuts/AppData/Local/hermes/hermes-agent'),
        Path('D:/AI/admin/hermes-config/hermes-agent'),
    ):
        if (c / 'agent' / 'memory_provider.py').exists():
            return c
    return None


def _load_plugin_module():
    """Load the plugin package by path, without importing hermes itself."""
    path = _plugin_path()
    if path is None:
        pytest.skip('hermes astor plugin source not found on this machine')
    hermes_root = _hermes_agent_path()
    if hermes_root is not None:
        # The plugin does `from agent.memory_provider import MemoryProvider`.
        if str(hermes_root) not in sys.path:
            sys.path.insert(0, str(hermes_root))
    import importlib.util
    pkg_dir = path.parent
    # The plugin is a package; give it a name that does not collide with
    # the installed astor_memory package.
    name = '_astor_hermes_plugin_under_test'
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(pkg_dir)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # the plugin imports hermes modules
        pytest.skip(f'plugin import failed ({type(e).__name__}): {e}')
    return mod


def test_plugin_default_user_id_is_admin():
    """The precondition of the bug: the default identity IS admin."""
    mod = _load_plugin_module()
    assert mod._load_config()['user_id'] == 'admin'


def test_unresolved_sender_is_not_admin():
    """_force_public must drop the operator's identity."""
    mod = _load_plugin_module()

    class _Fake:
        _user_id = 'admin'
        _tier = 'private'
        _identity_resolved = True
        _force_public = mod.AstorMemoryProvider._force_public

    obj = _Fake()
    obj._force_public('unbound sender platform=telegram id=12345')

    assert obj._identity_resolved is False
    assert obj._tier == 'public'
    # The heart of it: NOT admin, and not empty either -- an empty
    # user id is falsy, which the server reads as "no identity given"
    # and then binds as admin. Only an explicit non-admin id works.
    assert obj._user_id not in ('', 'admin')
    assert obj._user_id == 'anonymous'


def test_the_anonymous_identity_resolves_to_a_plain_user():
    """The server must treat 'anonymous' as a plain user, not admin."""
    actor, role, plan = _astor_resolve_actor('anonymous')
    assert role == 'user'
    assert actor == 'user:anonymous'
    assert plan == 'free'


def test_an_empty_identity_still_resolves_to_admin():
    """The server's own fallback is unchanged -- this test documents it.

    The plugin no longer relies on this: it sends 'anonymous'
    explicitly. But the fallback is still there for the local
    single-operator CLI, so it is pinned rather than silently changed.
    """
    actor, role, _plan = _astor_resolve_actor('')
    assert role == 'admin'
    assert actor == 'admin:admin'


# --- the server side that makes it meaningful ----------------------------

def test_anonymous_may_not_read_the_source_tier():
    """An anonymous caller gets the public tier and nothing more."""
    from astor_memory._internal.acl import astor_init_acl
    from astor_memory.server import _meta_recall_tiers

    astor_init_acl(actor='user:anonymous', role='user', tier='public',
                   user_id=None, subscription_plan='free')
    tiers = [t for t, _ in _meta_recall_tiers('public', 'anonymous')]
    assert 'source' not in tiers
    assert 'public' in tiers
