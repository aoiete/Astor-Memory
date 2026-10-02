"""v1.16.42: Cold-cache prewarm for /v1/dashboard."""
import time as _t


def _astor_prewarm_dashboard_cache(astor_dir_str):
    """Build dashboard payload in background thread at startup.

    Per R-class 12485 / 100-user scale: 16+ user DB aggregates take
    ~1s on cold cache. With 100 users scales linearly. Pre-warm so
    first /v1/dashboard hit is fast.
    """
    try:
        from .dashboard_data import build_dashboard_payload
        payload = build_dashboard_payload(astor_dir_str)
        # Update _DASHBOARD_CACHE in server.py
        import sys as _s
        srv_mod = _s.modules.get('astor_memory.server')
        if srv_mod is not None:
            srv_mod._DASHBOARD_CACHE['payload'] = payload
            srv_mod._DASHBOARD_CACHE['astor_dir'] = astor_dir_str
            srv_mod._DASHBOARD_CACHE['ts'] = _t.time()
            print('   Dashboard cache pre-warmed ({} sections)'.format(
                len(payload) if payload else 0), flush=True)
    except Exception as exc:
        print('   Dashboard prewarm failed (non-fatal): {}'.format(exc), flush=True)