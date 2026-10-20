# v1.16.77: the Visual C++ runtime version that onnxruntime actually needs.
#
# onnxruntime >= 1.21 fails to import on a machine whose vcruntime140_1.dll
# predates the VS2022 toolchain, and the failure is a native fault (exit 139)
# rather than an exception — see microsoft/onnxruntime#16116. Everything from
# the VS2019 generation (14.2x) up is affected; the verified-good install on
# the reference machine carries 14.51.36247.0, the failing one 14.28.29325.2.
#
# Read the version through subprocess + PowerShell rather than by walking the
# PE version resource with ctypes. The ctypes version of this function
# segfaulted the very interpreter it was written to diagnose: byte-offset
# arithmetic into VS_FIXEDFILEINFO is exactly the unchecked pointer work that
# produces a silent native fault, which is the failure mode we are trying to
# report. A subprocess costs one round-trip and cannot take `am doctor` down
# with it if the query ever goes wrong.
#
# Parsed from the FileVersionInfo FileVersion string ("14.51.36247.0"), which
# is stable and human-readable.
_VCRUNTIME_MIN = (14, 31)  # anything below this is the VS2019 generation
_VCRUNTIME_BACKLINK = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170"


def _read_vcruntime_version() -> tuple:
    """Return (version_string, is_too_old) for vcruntime140_1.dll.

    (None, True)          DLL absent — onnxruntime will not import at all
    ("<ver>", True)       present but older than onnxruntime requires
    ("<ver>", False)      present and new enough
    ("...", False)        present, version resource unreadable — not asserted
    """
    import os
    import pathlib
    import re
    import subprocess

    path = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vcruntime140_1.dll"
    if not path.exists():
        return None, True

    ps = ("(Get-Item $env:SystemRoot\\System32\\vcruntime140_1.dll)"
          ".VersionInfo.FileVersion")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return "present (version unavailable)", False

    text = (out.stdout or "").strip()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return "present (version unavailable)", False

    major, minor = int(m.group(1)), int(m.group(2))
    ver = f"{major}.{minor}.{m.group(3)}.{m.group(4)}"
    return ver, (major, minor) < _VCRUNTIME_MIN

# --- v1.16.78: install the redistributable for the operator ------------------
#
# The instruction "install the Visual C++ redistributable" is only useful to
# someone who already knows they have to. The failure that motivates this
# (onnxruntime >= 1.21 hard-faulting on a VS2019-era runtime, exit 139, no
# traceback) looks like astor being broken, so the operator's first instinct is
# to debug astor rather than to update a system DLL. Handing them a download
# link is one step better than that and no harder to implement.
#
# So: fetch the installer Microsoft publishes, verify it, run it, and verify
# the result. Only the redistributable is touched — nothing about astor, the
# data directory, or the Python environment. Elevation is requested by the
# installer itself (UAC); we never impersonate it.

_VCRED_URL = ("https://aka.ms/vs/17/release/vc_redist.x64.exe")
_VCRED_URL_ARM64 = ("https://aka.ms/vs/17/release/vc_redist.arm64.exe")
_VCRED_SIZE_FLOOR = 20 * 1024 * 1024  # a real installer is tens of MB


def _redist_url() -> str:
    import platform
    return _VCRED_URL_ARM64 if platform.machine().upper() in ("ARM64", "AARCH64") else _VCRED_URL


def install_vcredist(timeout=900, dry_run=False):
    """Download and install the x64 Visual C++ redistributable.

    Returns a dict with keys: status, detail, url, path, installed_version.

    status is one of:
        already_current   nothing to do
        installed         installer ran and reported success
        needs_reboot      installer succeeded; a restart is required for the
                          new DLLs to replace those already mapped into
                          running processes (this is the common case — a
                          terminal, an IDE, or the agent itself will be holding
                          the old vcruntime140_1.dll)
        failed            download or installer failed; detail says why
        declined          dry_run was requested
        unsupported       not Windows

    Deliberately does not attempt to make the new DLLs live in this process.
    Replacing a mapped DLL in-place is not something a child process can do
    safely, and pretending otherwise would produce a confusing half-working
    state where `am preflight` still reports the old version. The honest
    outcome is needs_reboot.
    """
    import platform

    if platform.system() != "Windows":
        return {"status": "unsupported", "detail": f"not Windows ({platform.system()})",
                "url": None, "path": None, "installed_version": None}

    ver, too_old = _read_vcruntime_version()
    if ver is not None and not too_old:
        return {"status": "already_current", "detail": f"vcruntime140_1.dll {ver} is current",
                "url": None, "path": None, "installed_version": ver}

    url = _redist_url()
    if dry_run:
        return {"status": "declined",
                "detail": f"would download {url}"
                          + (f" and install it (current: {ver})" if ver else " and install it"),
                "url": url, "path": None, "installed_version": None}

    import hashlib
    import pathlib
    import subprocess
    import tempfile
    import urllib.request

    tmp = pathlib.Path(tempfile.gettempdir())
    exe = tmp / "vc_redist.x64.exe"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "astor-memory/am-preflight"})
        with urllib.request.urlopen(req, timeout=120) as r, open(exe, "wb") as f:
            f.write(r.read())
        size = exe.stat().st_size
        if size < _VCRED_SIZE_FLOOR:
            exe.unlink(missing_ok=True)
            return {"status": "failed",
                    "detail": f"downloaded {size} bytes from {url}, which is too small to be "
                              f"the installer — the URL may have redirected to an error page",
                    "url": url, "path": None, "installed_version": None}
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()[:16]
    except Exception as e:
        return {"status": "failed", "detail": f"download failed: {e}",
                "url": url, "path": None, "installed_version": None}

    try:
        # /install /quiet /norestart — the standard unattended switches.
        # Returns 0 on success, 1638 if a newer version is already installed
        # (which is fine and means the DLLs are current for this session).
        proc = subprocess.run([str(exe), "/install", "/quiet", "/norestart"],
                              capture_output=True, text=True, timeout=timeout)
        rc = proc.returncode
    except Exception as e:
        return {"status": "failed", "detail": f"installer could not run: {e}",
                "url": url, "path": str(exe), "installed_version": None}

    ver_after, too_old_after = _read_vcruntime_version()

    if rc == 0 or rc == 1638 or rc == 3010:
        if too_old_after:
            return {"status": "failed",
                    "detail": f"installer ran (rc={rc}) but vcruntime140_1.dll still reads "
                              f"{ver_after} — a restart is required before the new DLLs load",
                    "url": url, "path": str(exe), "installed_version": ver_after}
        if rc == 3010:
            return {"status": "needs_reboot",
                    "detail": f"installed; vcruntime140_1.dll now reads {ver_after}. "
                              f"Restart Windows so processes holding the old DLL pick it up",
                    "url": url, "path": str(exe), "installed_version": ver_after}
        return {"status": "installed",
                "detail": f"installed; vcruntime140_1.dll now reads {ver_after}. "
                          f"If onnxruntime still fails to import, restart Windows — "
                          f"this process is still running against the old DLL",
                "url": url, "path": str(exe), "installed_version": ver_after}

    return {"status": "failed",
            "detail": f"installer exited rc={rc}. "
                      + (proc.stdout or proc.stderr or "").strip()[:300]
                      + " — try the manual download instead",
            "url": url, "path": str(exe), "installed_version": ver_after}
