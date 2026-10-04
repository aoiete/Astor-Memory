"""sync_to_runtime.py — Sync source tree changes to local runtime deployment.

Compares the source tree (default: D:/AI/astor-memory/) against the
runtime tree (default: D:/AI/Astor-Memory-Runtime/) and copies files
that differ. Optionally restarts the astor server via NSSM so the
runtime picks up the new code.

Why this script exists:
- Astor source SSoT = D:/AI/astor-memory/ (git-tracked, public)
- Astor runtime SSoT = D:/AI/Astor-Memory-Runtime/ (deployment, not git)
- Server.py imports resolve via PYTHONPATH=D:/AI/Astor-Memory-Runtime,
  so runtime must mirror source for the server to see the latest code.
- Without this helper, the operator must manually copy each changed file
  AND restart the NSSM-wrapped server, which is error-prone (missed
  files, drift between source and runtime).

Usage:
    python scripts/sync_to_runtime.py                  # dry-run (default)
    python scripts/sync_to_runtime.py --execute       # actually copy + restart
    python scripts/sync_to_runtime.py --no-restart    # copy only, no server restart
    python scripts/sync_to_runtime.py --src D:/foo --runtime D:/bar

Exit codes:
    0  = all files in sync OR successfully synced
    1  = some files failed to copy
    2  = source or runtime dir not found

Notes:
- Uses Python 3.11 stdlib only (no third-party deps).
- Skips .git/, __pycache__/, *.pyc, .test_workspace/, .tmp_*.ps1.
- Compares by md5 (binary-correct). Skips identical files (fast on
  large repos).
- Restarts via NSSM if --no-restart NOT set. NSSM respawn latency
  ~3-5s, so /v1/health may take a few seconds to return 200 after
  the restart fires.
- Logs to stdout with timestamps. Pipe to file if you want
  persistent audit (e.g. >> sync_audit.log).
"""
from __future__ import annotations

import argparse
import hashlib
import os  # v1.16.62: ASTOR_HOME env var
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_SRC = Path(r'D:/AI/astor-memory')
DEFAULT_RUNTIME = Path(r'D:/AI/Astor-Memory-Runtime')
# v1.16.62: when --src/--runtime not given, prefer paths from
# ~/.astor/install.json if it exists (written by `am init` on first install).
# ASTOR_HOME env var overrides Path.home() (e.g. when run from a service
# or kernel context where Path.home() returns a system dir).
INSTALL_HOME = Path(os.environ.get('ASTOR_HOME') or str(Path.home()))
INSTALL_JSON = INSTALL_HOME / '.astor' / 'install.json'
NSSM_SERVICE = 'MemoryServersWatch'
HEALTH_URL = 'http://127.0.0.1:7803/v1/health'
HEALTH_TIMEOUT_S = 30


def _load_install_paths() -> tuple[Path, Path]:
    """Return (src, runtime) from ~/.astor/install.json if it exists.

    install.json schema (written by `am init`):
        {
          "astor_dir": "/var/lib/astor",      # runtime deployment
          "source_dir": "/home/me/astor-memory",  # source checkout (optional)
          "installed_at": "2026-10-04T..."
        }
    Returns (None, None) entries if file missing or malformed.
    """
    if not INSTALL_JSON.exists():
        return None, None
    try:
        import json
        cfg = json.loads(INSTALL_JSON.read_text(encoding='utf-8'))
        src = Path(cfg['source_dir']).expanduser() if cfg.get('source_dir') else None
        rt = Path(cfg['astor_dir']).expanduser() if cfg.get('astor_dir') else None
        return src, rt
    except Exception:
        return None, None

# Skip patterns (relative path globs)
SKIP_GLOBS = ['.git', '__pycache__', '.test_workspace']
SKIP_SUFFIXES = ('.pyc', '.pyo')


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _is_skipped(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_GLOBS for p in parts):
        return True
    if rel.suffix in SKIP_SUFFIXES:
        return True
    return False


def _walk_tracked_files(root: Path) -> list[Path]:
    """Return all files under root, skipping patterns."""
    out = []
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _is_skipped(rel):
            continue
        out.append(p)
    return out


def _log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


def diff_files(src: Path, runtime: Path) -> tuple[list[tuple[Path, str, str]], list[Path]]:
    """Return (changed, missing_in_runtime) where each entry:
    - changed: (rel_path, src_md5, runtime_md5_or_None)
    - missing_in_runtime: rel_path (file exists in src but not in runtime)
    """
    src_files = _walk_tracked_files(src)
    changed = []
    missing = []
    for sp in src_files:
        rel = sp.relative_to(src)
        rp = runtime / rel
        if not rp.exists():
            missing.append(rel)
            continue
        h_s = _md5(sp)
        h_r = _md5(rp)
        if h_s != h_r:
            changed.append((rel, h_s, h_r))
    return changed, missing


def copy_files(src: Path, runtime: Path, rels: list[Path]) -> tuple[int, int]:
    """Copy each rel from src to runtime. Returns (ok, fail) counts."""
    ok = 0
    fail = 0
    for rel in rels:
        sp = src / rel
        rp = runtime / rel
        try:
            rp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, rp)
            ok += 1
            _log(f'  copied: {rel}')
        except Exception as e:
            fail += 1
            _log(f'  FAIL  {rel}: {e}')
    return ok, fail


def restart_server_via_nssm(timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    """Restart the astor server via NSSM. Returns True if /v1/health
    returns 200 within timeout."""
    _log(f'restarting NSSM service: {NSSM_SERVICE}')
    try:
        subprocess.run(['nssm', 'restart', NSSM_SERVICE],
                       capture_output=True, timeout=30, check=True)
    except subprocess.CalledProcessError as e:
        _log(f'NSSM restart failed: {e.stderr.decode("utf-8", errors="replace")}')
        return False
    except Exception as e:
        _log(f'NSSM restart error: {e}')
        return False

    # Poll /v1/health
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    body = r.read().decode('utf-8', errors='replace')
                    if '"status": "ok"' in body:
                        _log(f'/v1/health 200 OK after restart')
                        return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    _log(f'/v1/health did not return 200 within {timeout_s}s')
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--src', type=Path, default=None,
                    help=f'Source tree (default: from ~/.astor/install.json source_dir, else {DEFAULT_SRC})')
    ap.add_argument('--runtime', type=Path, default=None,
                    help=f'Runtime tree (default: from ~/.astor/install.json astor_dir, else {DEFAULT_RUNTIME})')
    ap.add_argument('--write-install', type=Path, default=None,
                    help='Persist these --src and --runtime to ~/.astor/install.json (sets source_dir + astor_dir)')
    ap.add_argument('--execute', action='store_true',
                    help='Actually copy files (default: dry-run)')
    ap.add_argument('--no-restart', action='store_true',
                    help='Skip NSSM restart after copy')
    ap.add_argument('--files', nargs='*', default=None,
                    help='Only sync these rel paths (default: all changed)')
    ap.add_argument('--git-diff', action='store_true',
                    help='Sync only files in `git diff --name-only` (vs full tree walk)')
    args = ap.parse_args()

    # Resolve defaults from install.json if not given
    if args.src is None or args.runtime is None:
        saved_src, saved_rt = _load_install_paths()
        if args.src is None and saved_src is not None:
            args.src = saved_src
        if args.runtime is None and saved_rt is not None:
            args.runtime = saved_rt

    if args.src is None:
        args.src = DEFAULT_SRC
    if args.runtime is None:
        args.runtime = DEFAULT_RUNTIME

    # Persist if requested
    if args.write_install is not None:
        import json
        cfg = {}
        if INSTALL_JSON.exists():
            try:
                cfg = json.loads(INSTALL_JSON.read_text(encoding='utf-8'))
            except Exception:
                pass
        cfg['source_dir'] = str(args.src)
        cfg['astor_dir'] = str(args.runtime)
        import time as _t
        cfg.setdefault('installed_at', _t.strftime('%Y-%m-%dT%H:%M:%SZ', _t.gmtime()))
        INSTALL_JSON.parent.mkdir(parents=True, exist_ok=True)
        INSTALL_JSON.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        _log(f'wrote {INSTALL_JSON}: source_dir={args.src} astor_dir={args.runtime}')

    if not args.src.exists():
        _log(f'ERROR: src not found: {args.src}')
        return 2
    if not args.runtime.exists():
        _log(f'ERROR: runtime not found: {args.runtime}')
        return 2

    _log(f'src     = {args.src}')
    _log(f'runtime = {args.runtime}')

    if args.git_diff:
        # Only sync files in the current git diff
        r = subprocess.run(['git', '-C', str(args.src), 'diff', '--name-only'],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            _log(f'git diff failed: {r.stderr.strip()}')
            return 1
        rels = [p for p in r.stdout.strip().split('\n') if p]
        _log(f'--git-diff mode: {len(rels)} files')
        # Just check md5 of those specific files vs runtime
        changed, missing = [], []
        for rel_str in rels:
            rel = Path(rel_str)
            sp = args.src / rel
            rp = args.runtime / rel
            if not sp.exists():
                continue
            if not rp.exists():
                missing.append(rel)
                continue
            h_s = _md5(sp)
            h_r = _md5(rp)
            if h_s != h_r:
                changed.append((rel, h_s, h_r))
    else:
        changed, missing = diff_files(args.src, args.runtime)
    _log(f'changed: {len(changed)} files')
    _log(f'missing in runtime: {len(missing)} files')

    if args.files:
        # Filter to user-specified
        wanted = set(args.files)
        changed = [c for c in changed if str(c[0]) in wanted]
        missing = [m for m in missing if str(m) in wanted]
        _log(f'after --files filter: {len(changed)} changed, {len(missing)} missing')

    if not changed and not missing:
        _log('already in sync')
        return 0

    if not args.execute:
        _log('DRY-RUN (use --execute to copy)')
        for rel, h_s, h_r in changed:
            _log(f'  WOULD COPY: {rel} (src {h_s[:8]} -> runtime {h_r[:8] if h_r else "MISSING"})')
        for rel in missing:
            _log(f'  WOULD CREATE: {rel}')
        return 0

    # Execute
    to_copy = [c[0] for c in changed] + missing
    ok, fail = copy_files(args.src, args.runtime, to_copy)
    _log(f'copy result: {ok} ok, {fail} fail')

    if fail > 0:
        return 1

    if not args.no_restart:
        if not restart_server_via_nssm():
            return 1

    _log('done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
