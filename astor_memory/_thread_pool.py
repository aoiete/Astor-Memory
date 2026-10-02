"""
astor thread pool — bounded concurrency for hot path endpoints.

Per R-class 12518 / DC bot 2026-10-01 parallel timeout reports:
- Flask dev server (threaded=False): 1 request at a time, no parallelism
- Flask dev server (threaded=True): unbounded thread spawn → OOM crash (1.75GB+)
- Solution: explicit ThreadPoolExecutor max_workers=8, wraps the work,
  Flask stays threaded=False (stable). Endpoints push work to pool.

Hot path endpoints benefit:
- /v1/read (DC bot recall, ~390ms each)
- /v1/write (muse, ~500ms with embed)
- /v1/dashboard (cached 5min, low load)

Cold path (audit/install/identity) stays serial.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import time
import traceback

# Pool sizes per R-class 12518 — 8 = good for small bus, scales w/ cores
HOT_PATH_MAX_WORKERS = 8

_pool = ThreadPoolExecutor(max_workers=HOT_PATH_MAX_WORKERS, thread_name_prefix='astor-hot')


def submit_async(fn, *args, **kwargs):
    """Submit work to the hot path pool. Returns Future."""
    return _pool.submit(fn, *args, **kwargs)


def pool_stats() -> dict:
    """Internal pool stats (debug)."""
    # ThreadPoolExecutor doesn't expose running count directly, but
    # we can check if any futures are pending.
    return {
        'max_workers': HOT_PATH_MAX_WORKERS,
        'pool_alive': not _pool._shutdown,
    }


# Convenience: run in pool + return result (block caller)
def run_in_pool(fn, *args, timeout=30, **kwargs):
    """Run fn in pool, block caller up to timeout. Returns result or raises."""
    fut = _pool.submit(fn, *args, **kwargs)
    return fut.result(timeout=timeout)


__all__ = ['submit_async', 'pool_stats', 'run_in_pool', 'HOT_PATH_MAX_WORKERS']