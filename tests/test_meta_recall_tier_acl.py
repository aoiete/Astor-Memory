"""The meta-recall tier list must come from the ACL, not from a hardcoded list.

v1.17.84. Before this, every meta-recall function in server.py built its
own tier list by hand:

    if tier and tier.startswith('private'):
        tiers_to_query.append(('private', user))
        tiers_to_query.append(('public', None))
        tiers_to_query.append(('source', None))     # <- always
    elif tier == 'source':
        tiers_to_query.append(('source', None))
    else:
        tiers_to_query.append(('public', None))
        tiers_to_query.append(('source', None))     # <- always

The comment said why: "the source tier is global lessons that apply
across users". But the ACL says the opposite, in so many words -- the
source tier is admin + system only, and "source is not a shared-readable
layer". The two disagreed, and the ACL lost.

Downstream, astor_bus(tier='source') did refuse a plain user -- but
only because the per-tier bus open raises PermissionError_ and the
meta-recall loop swallowed it in a bare `except Exception: continue`.
The tier list itself was never derived from the ACL, so the guarantee
was accidental rather than designed, and it lived in six copies of the
same block.

The concrete symptom on the operator's host: a public-tier /v1/read
returned a source-tier lesson containing the operator's own Telegram
chat id. The caller was identity-less, which resolves to admin:admin --
so the source bus opened, and the lesson came back.

These tests pin the rule: the tier list is what the ACL says it is.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astor_memory._internal.acl import astor_init_acl  # noqa: E402
from astor_memory.server import _meta_recall_tiers  # noqa: E402


def _bind(actor, role, user_id=None, plan=None):
    kwargs = {'actor': actor, 'role': role, 'tier': 'public', 'user_id': user_id}
    if plan is not None:
        kwargs['subscription_plan'] = plan
    astor_init_acl(**kwargs)


def _tiers(tier, user):
    return [t for t, _ in _meta_recall_tiers(tier, user)]


# --- admin: unchanged behavior, global lessons still injected -------------

def test_admin_still_gets_the_source_tier_on_a_public_read():
    """The global-lessons intent is preserved for the operator."""
    _bind('admin:admin', 'admin', user_id='admin')
    assert 'source' in _tiers('public', 'admin')


def test_admin_still_gets_the_source_tier_on_a_private_read():
    _bind('admin:admin', 'admin', user_id='admin')
    tiers = _tiers('private', 'admin')
    assert tiers == ['private', 'public', 'source']


def test_admin_asking_for_the_source_tier_gets_it():
    _bind('admin:admin', 'admin', user_id='admin')
    assert _tiers('source', 'admin') == ['source']


# --- plain user: the source tier is not theirs to read --------------------

def test_plain_user_does_not_get_the_source_tier_on_a_public_read():
    _bind('user:yuqi', 'user', user_id='yuqi', plan='free')
    assert 'source' not in _tiers('public', 'yuqi')


def test_plain_user_does_not_get_the_source_tier_on_a_private_read():
    _bind('user:yuqi', 'user', user_id='yuqi', plan='free')
    tiers = _tiers('private', 'yuqi')
    assert tiers == ['private', 'public']
    assert 'source' not in tiers


def test_plain_user_asking_for_the_source_tier_gets_nothing():
    """Asking for the source tier explicitly does not override the ACL."""
    _bind('user:yuqi', 'user', user_id='yuqi', plan='free')
    assert _tiers('source', 'yuqi') == []


# --- system: the other role the ACL allows -------------------------------

def test_system_gets_the_source_tier():
    _bind('system', 'system')
    assert 'source' in _tiers('public', 'system')


# --- the shape of the fix ------------------------------------------------

def test_the_tier_list_is_never_duplicated():
    """A private read must not list a tier twice."""
    _bind('admin:admin', 'admin', user_id='admin')
    for tier in ('public', 'private', 'source', None):
        tiers = _tiers(tier, 'admin')
        assert len(tiers) == len(set(tiers)), tier


@pytest.mark.parametrize('tier', ['public', 'private', 'source', None])
def test_every_tier_returns_a_list(tier):
    _bind('admin:admin', 'admin', user_id='admin')
    out = _meta_recall_tiers(tier, 'admin')
    assert isinstance(out, list)
    assert all(isinstance(t, str) for t, _ in out)


def test_the_helper_does_not_swallow_acl_bugs():
    """Only PermissionError_ is a refusal; a broken helper must not hide.

    If the ACL helper raised NameError (a missing import, say), a bare
    `except Exception` would silently drop the source tier for admin
    too -- and the symptom would read as "the global lessons stopped
    showing up" instead of "the ACL helper is broken".
    """
    import astor_memory.server as srv

    _bind('admin:admin', 'admin', user_id='admin')
    real = srv.astor_check_read

    def boom(tier, user_id=None):
        raise NameError('simulated broken ACL helper')

    srv.astor_check_read = boom
    try:
        with pytest.raises(NameError):
            _meta_recall_tiers('public', 'admin')
    finally:
        srv.astor_check_read = real
