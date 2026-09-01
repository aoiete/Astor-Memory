"""Hermes-agent patch for `pre_gateway_dispatch` `authorized` action support.

This file documents the minimal code change that hermes-agent
needs to honor `{"action": "authorized"}` from a plugin's
`pre_gateway_dispatch` callback. With this patch, a plugin can
short-circuit `_is_user_authorized` and proceed straight to
dispatch.

Where it goes
=============

File: `gateway/run.py` in the hermes-agent installation. Specifically,
the `_handle_message` method (or equivalent — search for
`pre_gateway_dispatch` to find the right block).

Find this block in the original file (before patch):

    # Fire pre_gateway_dispatch plugin hook for user-originated messages.
    # Plugins receive the MessageEvent and may return a dict influencing flow:
    #   {"action": "skip",    "reason": ...}    -> drop (no reply, plugin handled)
    #   {"action": "rewrite", "text":  ...}     -> replace event.text, continue
    #   {"action": "allow"}   /   None          -> normal dispatch
    # Hook runs BEFORE auth so plugins can handle unauthorized senders
    # (e.g. customer handover ingest) without triggering the pairing flow.
    if not is_internal:
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _hook_results = _invoke_hook(
                "pre_gateway_dispatch",
                event=event,
                gateway=self,
                session_store=self.session_store,
            )
        except Exception as _hook_exc:
            logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
            _hook_results = []

        for _result in _hook_results:
            if not isinstance(_result, dict):
                continue
            _action = _result.get("action")
            if _action == "skip":
                logger.info(...)
                return None
            if _action == "rewrite":
                _new_text = _result.get("text")
                if isinstance(_new_text, str):
                    event = dataclasses.replace(event, text=_new_text)
                    source = event.source
                break
            if _action == "allow":
                break

And replace it with the block shown below.

PATCHED BLOCK (after)
====================

    # Fire pre_gateway_dispatch plugin hook for user-originated messages.
    # Plugins receive the MessageEvent and may return a dict influencing flow:
    #   {"action": "skip",    "reason": ...}    -> drop (no reply, plugin handled)
    #   {"action": "rewrite", "text":  ...}     -> replace event.text, continue
    #   {"action": "allow"}   /   None          -> normal dispatch
    #   {"action": "authorized"}                -> SKIP _is_user_authorized; bot-binding
    #                                              plugin has confirmed this user
    # Hook runs BEFORE auth so plugins can handle unauthorized senders
    # (e.g. customer handover ingest) without triggering the pairing flow.
    _pre_dispatch_authorized = False
    if not is_internal:
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _hook_results = _invoke_hook(
                "pre_gateway_dispatch",
                event=event,
                gateway=self,
                session_store=self.session_store,
            )
        except Exception as _hook_exc:
            logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
            _hook_results = []

        for _result in _hook_results:
            if not isinstance(_result, dict):
                continue
            _action = _result.get("action")
            if _action == "skip":
                logger.info(
                    "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                    _result.get("reason"),
                    source.platform.value if source.platform else "unknown",
                    source.chat_id or "unknown",
                )
                return None
            if _action == "rewrite":
                _new_text = _result.get("text")
                if isinstance(_new_text, str):
                    event = dataclasses.replace(event, text=_new_text)
                    source = event.source
                break
            if _action == "authorized":
                # Plugin has authoritative knowledge of this user (e.g.
                # bot-binding.db match). Skip _is_user_authorized for this
                # event. The plugin is responsible for any audit logging.
                _pre_dispatch_authorized = True
                logger.info(
                    "pre_gateway_dispatch authorized: platform=%s chat=%s user=%s reason=%s",
                    source.platform.value if source.platform else "unknown",
                    source.chat_id or "unknown",
                    source.user_id or "unknown",
                    _result.get("reason"),
                )
                break
            if _action == "allow":
                break

    # ADDITIONAL CHANGE: in the auth-check chain immediately following
    # the block above, the line `if is_internal:` is followed by
    # `pass` and then `elif source.user_id is None:`. Insert a new
    # branch BEFORE that elif:
    #
    #     if is_internal:
    #         pass
    #     elif _pre_dispatch_authorized:
    #         # 2026-08-18 the maintainer: a plugin (e.g. bot_binding_auth)
    #         # has authoritative knowledge of this user via
    #         # bot-binding.db. Skip _is_user_authorized. The plugin is
    #         # responsible for any audit logging.
    #         logger.info(
    #             "skipping _is_user_authorized: pre_gateway_dispatch "
    #             "plugin authorized user_id=%s platform=%s chat=%s",
    #             source.user_id or "unknown",
    #             source.platform.value if source.platform else "unknown",
    #             source.chat_id or "unknown",
    #         )
    #     elif source.user_id is None:
    #         ...
    #     elif not self._is_user_authorized(source):
    #         ...

How to apply
    ============

1. Save a backup of `gateway/run.py` first.

2. Edit the file manually (this is hermes-agent source — it lives
   in `.venv/Lib/site-packages/gateway/run.py` for hermes installed
   via uv/pip). The edits are line-for-line; no indentation
   gymnastics.

3. After every hermes-agent upgrade, re-apply this patch. The
   upstream hermes-agent maintainer should accept a 6-line patch
   that adds the `authorized` action — until then, document this
   patch in your own release notes.

Upstream proposal
==================

The fix should ideally land in hermes-agent upstream as a single
small PR adding the `authorized` action. Until that PR lands,
operators who want bot-binding.db as the canonical ACL source need
to apply this patch manually after every upgrade.
"""