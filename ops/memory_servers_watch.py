#!/usr/bin/env python3
"""memory_servers_watch.py — Watchdog for astor server (port 7803).

This is the versioned copy. The deployed copy lives outside the
repository (it is a service script, and its paths are this machine's
configuration rather than code):

  - <hermes-scripts>/memory_servers_watch.py   <- the deployed one
  - the runtime directory comes from ASTOR_DIR there

The repository copy is genericised: ASTOR_DIR for the runtime directory
(default ~/.astor) and ASTOR_PYTHON for the interpreter (default: this
one). The logic must stay identical between the two -- change both.

2026-08-16 update: replaced memu/mempalace (superseded by astor v1.1).
Watches astor server, restarts on crash.
Logic:
1. HTTP GET /v1/health on port 7803 every CYCLE_SECONDS.
2. If returns non-200 or times out (and is past startup grace period),
   kill stale python procs on that port and respawn astor server.
3. Never kills self or the Hermes gateway.

Two probes here could not answer and were being read as "dead":

  - tasklist enumerates every process on the host, so under load it
    blows past its timeout. Fixed 2026-10-22 (v1.16.90).
  - netstat -ano -p TCP enumerates every TCP connection, with the same
    consequence. Fixed 2026-10-22 (v1.16.92). A probe that cannot
    answer is UNKNOWN, not DEAD.

History:
- 07-09 fixes preserved: cycle heartbeat log, consecutive-fail counter,
  env preload, errors capture.
- 2026-07-15: code-patch mtime detection preserved.
- 2026-08-16: memu/palace removed, astor added.
- 2026-10-22: versioned copy; netstat timeout no longer means "dead".
"""
from __future__ import annotations
import hashlib
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
# Config
ASTOR_PORT = 7803
CYCLE_SECONDS = 60        # 2026-10-15: 900s (15min) -> 60s。
                        # 15 分钟的 cadence 意味着 astor 挂掉后最坏要等 16 分钟
                        # 才被发现 + 拉起(今晚实测: 挂了 5 次都拉不起来)。
                        # 60s 配合下面的 startup-grace 修复, 恢复时间 < 2 分钟。
STARTUP_GRACE_SEC = 60
# 2026-10-22: 3.0s -> 15.0s. A /v1/read that ranks a large corpus holds a
# waitress thread for seconds. With 8 threads and several concurrent
# readers the pool was saturated, the health probe could not get a
# thread, and the watchdog killed a perfectly healthy (but busy) server.
HEALTH_TIMEOUT_S = 15.0
STARTUP_TIMEOUT_S = 120
CONSECUTIVE_FAIL_ALERT = 3
# 2026-08-27 fix: restart-loop breaker. After FAIL_BREAK_THRESHOLD consecutive
# failed cycles, back off for FAIL_BREAK_COOLDOWN_SEC before trying again. Prevents
# kill/restart storms when astor itself is unfixable (missing DLL, port conflict,
# DB corruption) — instead of wasting CPU + spawning zombies, we wait.
FAIL_BREAK_THRESHOLD = 5
FAIL_BREAK_COOLDOWN_SEC = 600   # 10 min cooldown when break threshold hit
_fail_streak: dict[str, int] = {"astor": 0}
_cycle_count: int = 0
# 2026-08-27 fix: timestamp of when the restart-loop breaker tripped; we skip
# _ensure_astor entirely while we're cooling down.
_break_until: float = 0.0
# 2026-10-22: the absolute paths here were machine-specific and are now
# generic. The runtime directory comes from ASTOR_DIR (same variable the
# rest of astor uses) and defaults to ~/.astor, so the same script works
# on a developer box and on a deployed node.
RUNTIME_DIR = Path(os.environ.get("ASTOR_DIR") or (Path.home() / ".astor"))
SCRIPT_DIR = RUNTIME_DIR
LOG_FILE = RUNTIME_DIR / "logs" / "astor_server_watch.log"
# 2026-08-27 fix: watchdog self-heartbeat file. The watchdog supervisor (separate
# script) checks this file's mtime every 30 min; if stale → restart this daemon.
HEARTBEAT_FILE = RUNTIME_DIR / "logs" / "memory_servers_watch.heartbeat"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
HEARTBEAT_FILE.touch()   # ensure exists on first boot
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200 | CREATE_NO_WINDOW
# Astor server launcher: use pythonw.exe from hermes-agent venv (has flask + numpy + chromadb)
# OR use the venv that has astor installed.
# 2026-08-16: astor was installed as editable package — use the system python that has it.
# We fall back to the venv python if system python can't import astor_memory.
def _find_python() -> Path:
    """Find a pythonw.exe that can import astor_memory.
    2026-08-27 fix: only consider pythonw.exe (GUI subsystem, no console
    window). The previous list mixed pythonw.exe and python.exe; if the
    first pythonw.exe failed the import test, the script would fall through
    to Astor-Memory-venv\\Scripts\\python.exe (a console-subsystem binary, since removed),
    which caused a black console window to flash every time the watchdog
    restarted astor. pythonw.exe + CREATE_NO_WINDOW is what keeps this
    fully hidden.
    """
    # 2026-09-03 fix: one interpreter is the single source of truth for
    # astor on a given host; the retired venv and the roaming uv path are
    # gone, and a second interpreter is for ML only. Which one that is
    # differs per machine, so it is configuration (ASTOR_PYTHON) rather
    # than a hardcoded path.
    candidates = [
        # v1.16.92: the interpreter is an env override, not a fixed path.
        # ASTOR_PYTHON wins; then this interpreter; then the usual
        # per-environment layouts next to the runtime dir. The loop below
        # picks the first candidate that can actually import
        # astor_memory.server, so an entry that does not exist is simply
        # skipped.
        Path(os.environ["ASTOR_PYTHON"]) if os.environ.get("ASTOR_PYTHON") else None,
        Path(sys.executable),
        RUNTIME_DIR / ".venv" / "Scripts" / "pythonw.exe",
        RUNTIME_DIR / "venv" / "Scripts" / "pythonw.exe",
        Path("/usr/bin/python3"),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME_DIR)
    for p in candidates:
        if p and p.exists():
            try:
                r = subprocess.run([str(p), "-c", "import astor_memory.server; print('ok')"],
                                   capture_output=True, text=True, timeout=10,
                                   env=env,
                                   creationflags=CREATE_NO_WINDOW)
                if "ok" in r.stdout:
                    return p
            except Exception:
                continue
    raise RuntimeError(
        f"memory_servers_watch: no pythonw.exe in {candidates} can import "
        "astor_memory.server — install astor into a venv or fix PYTHONPATH"
    )
ASTOR_PY = _find_python()
# Launcher: python -m astor_memory.server --host 127.0.0.1 --port 7803
ASTOR_LAUNCH = ["-m", "astor_memory.server", "--host", "127.0.0.1", "--port", "7803"]
ASTOR_CWD = RUNTIME_DIR
KILL_SAFE_SCRIPTS = ("astor_memory.server", "flask")
# 2026-08-27 fix: PID blacklist. Never kill these — they're the watchdog itself,
# the hermes gateway, or critical system processes. PIDs are checked first (cheaper
# than cmdline lookup), then cmdline is checked for safe markers.
_MY_PID = os.getpid()
_KILL_PID_BLACKLIST = (
    _MY_PID,
    # Hermes gateway PIDs are loaded at startup
)
def _load_kill_pid_blacklist() -> tuple[int, ...]:
    """Discover Hermes gateway PIDs so we never kill them by accident.
    2026-08-27 fix: the previous KILL_SAFE_SCRIPTS check was a fragile string
    match — any python process with 'flask' in its cmdline could be flagged.
    Now we explicitly blacklist: (a) our own PID, (b) all hermes_cli.main
    gateway PIDs (read from gateway.pid file), (c) any process whose parent is
    the Hermes Agent launcher.
    """
    pids = {_MY_PID}
    gw_pid_file = Path(r"C:\WINDOWS\system32\config\systemprofile\AppData\Local\hermes\gateway.pid")
    if gw_pid_file.exists():
        try:
            import json as _json
            data = _json.loads(gw_pid_file.read_text().strip())
            if data.get("pid"):
                pids.add(int(data["pid"]))
        except Exception:
            pass
    # Also walk all python processes and pick out hermes gateway ones
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\") | "
             "Where-Object { $_.CommandLine -match 'hermes_cli.main.*gateway' } | "
             "ForEach-Object { $_.ProcessId }"],
            stderr=subprocess.DEVNULL, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in out.decode().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass
    return tuple(pids)
_last_spawn: dict[str, float] = {}
# 2026-08-27 fix: track CONTENT hash (sha256 of first 4KB) instead of mtime. Mtime
# changes on touch/stat/backup operations even when file content is unchanged —
# which previously caused spurious "script mtime changed, reloading" → kill+restart
# cycles. Now we only reload when actual content changes.
_script_hash_loaded: dict[str, str] = {}
def _file_content_hash(path: Path) -> str | None:
    """Hash first 4 KB of file. Cheap; collisions negligible for change detection."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            h.update(f.read(4096))
        return h.hexdigest()[:16]
    except OSError:
        return None
def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
def _health(port: int) -> bool:
    """3-layer liveness probe.
    2026-08-27 fix: previously this only checked HTTP 200 + '"status":"ok"'
    in body. That gave 3 false-state paths:
      - false NEGATIVE during astor startup (socket bound but Flask not ready)
        → watchdog kills the very process it just spawned
      - false NEGATIVE during shutdown (socket half-closed) → same
      - false POSITIVE on zombie (process alive, HTTP responds, but main thread
        deadlocked) → watchdog thinks astor is healthy when it isn't
    New 3-layer probe:
      L1 (network): is anyone LISTENING on the port? Fast, cheap.
      L2 (HTTP): does /v1/health return 200 + '"status":"ok"'?
      L3 (process): is the PID bound to the port actually responsive (not
          a zombie)? We verify by checking the PID's kernel-level state.
    Returns True only if all 3 layers pass.
    """
    # L1: is the port LISTENING?
    if not _port_listening(port):
        return False
    # L2: HTTP health probe
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT_S)
        conn.request("GET", "/v1/health")
        r = conn.getresponse()
        body = r.read().decode("utf-8", errors="ignore")
        if not (r.status == 200 and '"status":"ok"' in body):
            return False
        # L3: the PID holding the port must be alive (not zombie)
        pid = _pid_on_port(port)
        if pid is None:
            # netstat could not answer (it enumerates every TCP
            # connection on the host and blows past its timeout under
            # load). That is UNKNOWN, not DEAD. L1 already confirmed
            # something is LISTENING on the port, and a dead server does
            # not hold its socket, so treat this as alive rather than
            # restarting a healthy server mid-request.
            return True
        if not _is_pid_alive_l3(pid):
            return False
        return True
    except (socket.timeout, ConnectionResetError, ConnectionAbortedError,
            ConnectionError, http.client.RemoteDisconnected) as e:
        # 2026-10-22: the port is listening and the PID is alive, but the
        # health request did not complete. That is a BUSY server, not a
        # dead one -- every worker thread was busy ranking a corpus.
        # Killing it here is what caused the 10054/10061 connection
        # storms in the control-set gate: a healthy server was restarted
        # mid-flight because it was busy answering a question. Busy is not
        # broken. Treat it as alive and let the next cycle decide.
        #
        # The exception list matters: a busy waitress does not necessarily
        # raise socket.timeout. Under load it resets the connection
        # (WinError 10054) or aborts it, and those landed in a bare
        # `except Exception: return False`, so the probe declared a
        # perfectly healthy server dead and restarted it. The symptom was
        # a client seeing ConnectionResetError while the watchdog log
        # said "unhealthy on port 7803, restarting".
        #
        # The distinguishing test is the one that was already there: if
        # the port is listening and the owning PID is alive, the server
        # is alive, whatever the probe's own request experienced.
        pid = _pid_on_port(port)
        if pid is not None and _is_pid_alive_l3(pid):
            return True
        # netstat could not answer (timeout) -- that is UNKNOWN, not DEAD.
        # Fall back to the cheapest possible probe: a single connect_ex
        # against the port. It spawns no process, so it cannot itself
        # time out under load. A socket in LISTEN means some process is
        # still holding it, and a dead server does not hold its socket.
        if _port_listening(port):
            return True
        return False
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
def _port_listening(port: int) -> bool:
    """L1 probe — is anyone LISTENING on this TCP port? Cheap (single syscall)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        finally:
            s.close()
    except Exception:
        return False
def _pid_on_port(port: int) -> int | None:
    """Find the PID currently LISTENING on the given TCP port. Returns None if not found.

    2026-10-22: a `netstat` timeout used to mean "no PID". It does not.
    `netstat -ano -p TCP` enumerates EVERY TCP connection on the host, so
    on a loaded box (or one with hundreds of sockets in TIME_WAIT) it
    routinely blows past a 3s budget. The timeout landed in the same
    `except: return None` as a genuine "nothing is listening", the
    caller read that as "the server is dead", and the watchdog killed a
    perfectly healthy astor mid-request -- which surfaced to clients as
    WinError 10054 / 10061.

    A probe that could not answer is UNKNOWN, not DEAD. This mirrors the
    fix already applied to the tasklist probe above. The timeout is also
    raised to 10s, because enumerating every connection is genuinely
    slow on this host.
    """
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], text=True, timeout=10,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    for line in out.splitlines():
        if f":{port} " not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) >= 5:
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None
def _is_pid_alive_l3(pid: int) -> bool:
    """L3 probe — is the PID a real, responsive process? Distinguishes:
      - alive + responsive  (CPU > 0 OR threads > 1 → doing real work)
      - zombie (PID exists but WorkingSet=0, CPU=0, threads=1 for ages)
      - dead (PID does not exist)

    2026-10-22: a `tasklist` timeout used to mean "dead". It does not.
    tasklist enumerates EVERY process on the host, so on a loaded box it
    routinely takes longer than the 3s budget — and when it timed out,
    the probe returned False, the watchdog declared a healthy server
    dead, and restarted it. The server was then killed mid-request, which
    surfaced as WinError 10054/10061 in the control-set gate. A probe
    that could not answer is UNKNOWN, not DEAD, so a timeout now falls
    back to a cheap existence check via the OS.
    """
    try:
        # tasklist is fast and reliable on Windows
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        if str(pid) not in out:
            return False
        # Parse status column — look for "Not Responding" or "Unknown"
        # "Running" = healthy, anything else = suspect
        if "Not Responding" in out or "Paused" in out:
            return False
        return True
    except subprocess.TimeoutExpired:
        # The probe could not answer. That is not evidence of death.
        return _pid_exists(pid)
    except Exception:
        return False


def _pid_exists(pid: int) -> bool:
    """Cheap existence check that does not enumerate the whole process table.

    OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION succeeds only for a
    live PID, and it costs a single syscall instead of a tasklist spawn.
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False
def _kill_port(port: int) -> None:
    """Kill python processes listening on the given port (only astor-related).
    2026-08-27 fix: previous version used a fragile KILL_SAFE_SCRIPTS string match
    ('flask' or 'astor_memory.server' substring). That gave false positives: any
    python process whose cmdline contained 'flask' could be killed — including
    Hermes gateway adapters and unrelated flask tooling.
    New logic:
      1. Read LISTENING PIDs from netstat
      2. For each candidate PID, check PID blacklist FIRST (cheap)
      3. Then check cmdline for explicit astor markers (positive match required)
      4. Skip anything not in both safe sets
    """
    blacklist = set(_load_kill_pid_blacklist())
    # 2026-08-28 fix: netstat can stall for >5s when the gateway is restarting
    # (high system load). Use 15s timeout + 1 retry. Without this, _kill_port
    # returns silently with "command timed out" and the old astor processes
    # remain bound to the port — leading to multiple astor instances
    # listening on 7803.
    netstat_out = None
    for attempt in (1, 2):
        try:
            netstat_out = subprocess.check_output(
                ["netstat", "-ano"], text=True, timeout=15,
                creationflags=CREATE_NO_WINDOW)
            break
        except subprocess.TimeoutExpired:
            log(f"  netstat attempt {attempt} timed out after 15s")
            if attempt == 1:
                time.sleep(2)
                continue
        except Exception as e:
            log(f"  netstat attempt {attempt} failed: {e!r}")
            break
    if netstat_out is None:
        log(f"  _kill_port({port}): netstat failed after 2 attempts — skipping kill")
        return
    out = netstat_out
    candidate_pids: set[int] = set()
    for line in out.splitlines():
        if f":{port} " not in line:
            continue
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) >= 5:
            try:
                candidate_pids.add(int(parts[-1]))
            except ValueError:
                pass
        if not candidate_pids:
            log(f"  no LISTENING pid on port {port}")
            return
        for pid in candidate_pids:
            # 2026-08-27 fix: PID blacklist check FIRST. Never kill:
            #   - our own PID
            #   - any hermes_cli.main gateway PID
            if pid in blacklist:
                log(f"  PID {pid} in blacklist (self or gateway), skip")
                continue
            # Then check cmdline for safe markers
            # 2026-08-28 fix: powershell.exe can hang >5s when system is busy
            # (e.g. during gateway restart). Bump timeout to 15s.
            try:
                result = subprocess.run(
                    [
                        "powershell.exe", "-NoProfile", "-Command",
                        f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
                    ],
                    capture_output=True, text=True, timeout=15,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
                cmdline = (result.stdout or "").strip()
            except subprocess.TimeoutExpired:
                log(f"  cmdline lookup timeout for PID {pid} (15s) — skipping (cannot verify it's safe)")
                continue
            except Exception as e:
                log(f"  cmdline lookup failed for PID {pid}: {e}")
                continue
            if not any(s in cmdline for s in KILL_SAFE_SCRIPTS):
                log(f"  PID {pid} not in KILL_SAFE_SCRIPTS, skip ({cmdline[:60]}...)")
                continue
            log(f"  killing PID {pid} on port {port}")
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    stdin=subprocess.DEVNULL, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as e:
                log(f"  kill failed for PID {pid}: {e}")
def _astor_listener_pids() -> list[int]:
    """Every PID holding a LISTENING socket on the astor port.
    netstat can return more than one row when two servers both bound the port
    (Windows allows this via SO_REUSEADDR), so the caller must not assume the
    first match is the only one — that assumption is what let a duplicate
    server survive a restart and fight over the same SQLite files.
    """
    out: list[int] = []
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=10,
                             stdin=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        log(f"  listener scan failed: {e}")
        return out
    needle = f":{ASTOR_PORT}"
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if needle in parts[1] and parts[3].upper() == "LISTENING":
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid not in out:
                out.append(pid)
    return out
def _kill_duplicate_servers(keep_pid: int | None = None) -> int:
    """Leave exactly one astor server listening. Returns how many were killed.
    Two servers on one port means two processes writing the same SQLite
    files, which surfaces as "database is locked" and
    "Cannot operate on a closed database" in astor_server.log, then a 502 from
    the tunnel that the watchdog then "fixes" by killing and respawning — a
    loop that never converges. Picking a survivor is arbitrary (either one
    serves), so keep the oldest: it has been holding the port longer and is
    less likely to be mid-write.
    """
    pids = _astor_listener_pids()
    if len(pids) <= 1:
        return 0
    if keep_pid in pids:
        victims = [p for p in pids if p != keep_pid]
    else:
        ages = []
        for p in pids:
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Process -Id {p} -ErrorAction SilentlyContinue)"
                     f".StartTime.ToString('yyyy-MM-dd HH:mm:ss')"],
                    capture_output=True, text=True, timeout=10,
                    stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
                ages.append((p, res.stdout.strip()))
            except Exception:
                ages.append((p, ""))
        ages.sort(key=lambda kv: kv[1] or "9999")
        survivors = [ages[0][0]]
        victims = [a[0] for a in ages[1:]]
    killed = 0
    for p in victims:
        log(f"  duplicate astor server on port {ASTOR_PORT}: killing PID {p}")
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(p)],
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
            killed += 1
        except Exception as e:
            log(f"  duplicate kill failed for PID {p}: {e}")
    if killed:
        log(f"  de-duplicated astor: kept {survivors if keep_pid is None else keep_pid}, "
            f"killed {killed}")
        time.sleep(1.0)
    return killed
def _astor_env_clean() -> dict:
    """Environment for the server child, with the Hermes injection removed.

    PYTHONPATH is OVERWRITTEN rather than appended, which handles a leaked
    value — but the leak can also arrive through a key we do not set. Hermes
    injects a PYTHONPATH naming its own transient install venv; if any part
    of that survives, fastembed resolves PIL from the Hermes venv and dies
    with "ImportError: cannot import name '_imaging' from 'PIL'". Drop every
    PYTHON* variable and rebuild only what the server needs.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(ASTOR_CWD)
    env["ASTOR_DIR"] = str(ASTOR_CWD)
    return env


def _astor_importable(env: dict) -> tuple[bool, str]:
    """Verify astor_memory imports in THIS env before we spawn a server.

    Spawning into a broken env costs a 60s cold-start window and a 502 while
    the failed process briefly holds port 7803. Ten seconds of import check
    is cheaper than that, and it turns a silent crash-loop into a logged
    refusal.
    """
    probe = "import astor_memory.server; import PIL._imaging; print('ok')"
    try:
        r = subprocess.run([str(ASTOR_PY), "-c", probe],
                           capture_output=True, text=True, timeout=45,
                           cwd=str(ASTOR_CWD), env=env,
                           stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "import probe timed out after 45s"
    except Exception as e:
        return False, f"import probe could not run: {e}"
    if r.returncode != 0:
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
        return False, tail[:160]
    return True, ""


def _start_astor() -> bool:
    log(f"  starting astor: {ASTOR_PY} {' '.join(ASTOR_LAUNCH)}")
    server_log = SCRIPT_DIR / "logs" / "astor_server.log"
    run_env = _astor_env_clean()
    ok, why = _astor_importable(run_env)
    if not ok:
        log(f"  REFUSING to spawn - astor_memory does not import in this env: {why}")
        log("  (Hermes PYTHONPATH leak into fastembed/PIL is the usual cause; "
            "the caller must scrub PYTHON* before invoking this script)")
        return False
    try:
        log_fh = open(server_log, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [str(ASTOR_PY)] + ASTOR_LAUNCH,
                cwd=str(ASTOR_CWD),
                env=run_env,
                creationflags=DETACHED,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
            )
            _last_spawn["astor"] = time.monotonic()
            log(f"  spawned astor PID={proc.pid}")
        finally:
            log_fh.close()
        return True
    except Exception as e:
        log(f"  start failed: {e}")
        return False
def _ensure_astor(port: int) -> None:
    if _health(port):
        if _fail_streak["astor"] > 0:
            log(f"[astor] recovered after {_fail_streak['astor']} failed cycles")
        _fail_streak["astor"] = 0
        return
    # 2026-08-27 fix: restart-loop breaker. If we've failed FAIL_BREAK_THRESHOLD
    # times in a row, stop trying and wait FAIL_BREAK_COOLDOWN_SEC. This prevents
    # the watchdog itself from spawning zombie processes when astor is unfixable
    # (e.g. missing DLL, port permanently held by another process). The user gets
    # an alert + a clear log; manual intervention is required.
    if _fail_streak["astor"] >= FAIL_BREAK_THRESHOLD:
        now = time.monotonic()
        if now < _break_until:
            log(f"[astor] restart loop breaker active — cooling down "
                f"({int(_break_until - now)}s remaining); streak={_fail_streak['astor']}")
            return
        # Cooldown expired — reset streak, try again
        log(f"[astor] restart loop breaker cooldown expired; resetting streak and retrying")
        _fail_streak["astor"] = 0
        _break_until = 0.0
    last = _last_spawn.get("astor", 0.0)
    if last and (time.monotonic() - last) < STARTUP_GRACE_SEC:
        log(f"[astor] health fail but within {STARTUP_GRACE_SEC}s grace, waiting")
        return
    log(f"[astor] unhealthy on port {port}, restarting...")
    _kill_port(port)
    time.sleep(1)
    _start_astor()
    # 2026-08-27 fix: extend grace period so we don't double-restart during
    # Flask cold-start. The previous grace was 60s but the verification loop
    # polled every 1s for 120s — too aggressive. Now grace is the full startup window.
    for i in range(STARTUP_TIMEOUT_S):
        time.sleep(1)
        if _health(port):
            log(f"[astor] back up after {i+1}s")
            _fail_streak["astor"] = 0
            return
    _fail_streak["astor"] += 1
    log(f"[astor] FAILED to come back up after {STARTUP_TIMEOUT_S}s "
        f"— see astor_server.log (fail streak: {_fail_streak['astor']})")
    # Trip the breaker if we've now exceeded the threshold
    if _fail_streak["astor"] >= FAIL_BREAK_THRESHOLD and _break_until == 0.0:
        _break_until = time.monotonic() + FAIL_BREAK_COOLDOWN_SEC
        log(f"[astor] RESTART-LOOP BREAKER TRIPPED: {_fail_streak['astor']} consecutive fails, "
            f"cooling down {FAIL_BREAK_COOLDOWN_SEC}s before next attempt")
    if _fail_streak["astor"] >= CONSECUTIVE_FAIL_ALERT:
        log(f"[astor] ALERT: {_fail_streak['astor']} consecutive fails, shipping system_event to bus")
        _alert_to_bus(_fail_streak["astor"])
def _alert_to_bus(fail_streak: int) -> None:
    """Ship alert to astor bus (not the old memory_bus.db)."""
    try:
        import sqlite3
        from datetime import datetime, timezone
        db_path = RUNTIME_DIR / "public" / "memory" / "astor_bus_public.db"
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            # 2026-09-04 fix: events schema requires (ts, namespace, agent_id, source, action, content).
            # Old code only inserted (action, content, source, ts) and hit NOT NULL on namespace/agent_id.
            now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%fZ')
            conn.execute(
                """INSERT INTO events (ts, namespace, agent_id, source, action, content, metadata, tombstone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now_iso,
                    "admin",
                    "memory_servers_watch",
                    "astor_server_watch",
                    "alert",
                    f"astor_server_watch alert: failed {fail_streak} consecutive restart cycles "
                    f"(last at {now_iso}). Server may be in a death loop. Manual check needed.",
                    "{}",
                    0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log(f"  alert-to-bus failed (non-fatal): {e}")
def main() -> None:
    log(f"=== astor_server_watch started, pid={os.getpid()} ===")
    log(f"  watching port {ASTOR_PORT}, cycle={CYCLE_SECONDS}s")
    init_time = time.monotonic()
    # 2026-10-15 R-class fix: 原来这里写 `_last_spawn["astor"] = init_time`,
    # 结果每次 watchdog 重启后的**第一个 cycle 必然落在 STARTUP_GRACE_SEC
    # (60s) 宽限内被line 515 的 `if ... < STARTUP_GRACE_SEC: return` 跳过**,
    # 而下一个 cycle 要等 CYCLE_SECONDS=900s。症状: watchdog 起来后 16 分钟内
    # 永远不拉起 astor(今晚 astor 死 5 次都拉不起来就是这个原因)。
    # 修: 不预填 _last_spawn, 让第一个 cycle 走完整判断 -> 立刻拉起。
    # 只在 astor **真的** UP 时才记 _last_spawn(由 _ensure_astor 的成功分支负责)。
    log("  (startup grace not pre-seeded; first cycle will evaluate immediately)")
    # 2026-08-27 fix: seed CONTENT hash instead of mtime. mtime-based reload
    # was triggering spurious kill+restart cycles on touch/stat operations even
    # when the script content was unchanged. Content hash is stable across
    # mtime updates.
    try:
        server_py = ASTOR_CWD / "astor_memory" / "server.py"
        _script_hash_loaded["astor"] = _file_content_hash(server_py)
    except OSError:
        pass
    time.sleep(10)
    while True:
        global _cycle_count
        _cycle_count += 1
        log(f"--- cycle {_cycle_count} begin ---")
        # 2026-08-27 fix: heartbeat touch FIRST, before any potentially-failing
        # work. The watchdog supervisor reads this file's mtime to detect a
        # deadlocked daemon; if main() blocks for hours in _health() or _kill_port(),
        # the supervisor will still see fresh mtime if we touched it at the start
        # of every cycle. Worst case we miss one cycle's heartbeat, which is fine.
        try:
            HEARTBEAT_FILE.touch()
        except Exception:
            pass
        try:
            # 2026-08-27 fix: code-patch detection now uses content hash, not mtime
            try:
                server_py = ASTOR_CWD / "astor_memory" / "server.py"
                cur_hash = _file_content_hash(server_py)
                prev_hash = _script_hash_loaded.get("astor")
                if prev_hash is not None and cur_hash is not None and cur_hash != prev_hash:
                    log(f"[astor] script content changed (hash {prev_hash[:8]}->{cur_hash[:8]}), reloading…")
                    _kill_port(ASTOR_PORT)
                    time.sleep(1)
                    _start_astor()
                    _script_hash_loaded["astor"] = cur_hash
                    _last_spawn["astor"] = time.monotonic()
                    _fail_streak["astor"] = 0
                    time.sleep(3)
            except OSError:
                pass
            _ensure_astor(ASTOR_PORT)
        except Exception as e:
            import traceback
            log(f"cycle error: {e}\n{traceback.format_exc()}")
        astor_ok = _health(ASTOR_PORT)
        log(f"[cycle {_cycle_count}] astor:7803={'UP' if astor_ok else 'DOWN'} "
            f"streak={_fail_streak['astor']}")
        # 2026-09-04 fix: split CYCLE_SECONDS into 30s chunks + heartbeat touch
        # so a deadlock in the next iteration's I/O doesn't leave the watch
        # silently stalled for 15 minutes before the next log line.
        try:
            _remaining = CYCLE_SECONDS
            while _remaining > 0:
                chunk = min(30, _remaining)
                time.sleep(chunk)
                _remaining -= chunk
                try:
                    HEARTBEAT_FILE.touch()
                except Exception:
                    pass
        except KeyboardInterrupt:
            log('=== astor_server_watch interrupted, exiting ===')
            return
if __name__ == "__main__":
    main()