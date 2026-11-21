"""The Visual C++ runtime check must not fire on a non-Windows host.

The check exists because onnxruntime >= 1.21 dies on a native fault
(exit 139, no traceback) when the Windows VC++ runtime is older than
14.31. That is a Windows problem. On Linux the runtime does not
exist and never did, and the onnxruntime linux wheel needs none of it.

The old reading looked for System32/vcruntime140_1.dll, did not find
it, and reported "the DLL is missing" -- which the server turned into
"the native embedding stack is unavailable on this host" and disabled
semantic recall, and `am preflight` turned into "BLOCKER". The CI
runs on Linux, so the whole Linux path was running with the embedding
stack switched off.

The probe is read as it would read on another host: the platform name
is swapped and the DLL lookup is made to come up empty, which is what
a Linux host looks like to a check that does not know about Linux.
"""
from __future__ import annotations

import platform
from pathlib import Path

from astor_memory import _vcruntime


def _read_as(system: str, dll_present: bool) -> tuple:
    """_read_vcruntime_version() as it would read on `system`."""
    real_system = platform.system
    real_exists = Path.exists
    platform.system = lambda: system
    Path.exists = (lambda self: True) if dll_present else (lambda self: False)
    try:
        return _vcruntime._read_vcruntime_version()
    finally:
        platform.system = real_system
        Path.exists = real_exists


def test_a_non_windows_host_is_not_a_missing_dll() -> None:
    ver, too_old = _read_as("Linux", dll_present=False)
    assert ver is not None, "a non-Windows host has no VC++ runtime, but it is not missing"
    assert too_old is False, "a non-Windows host must not be reported as too old"


def test_the_preflight_probe_does_not_block_a_non_windows_host() -> None:
    """`am preflight` used to print BLOCKER on Linux."""
    ver, too_old = _read_as("Linux", dll_present=False)
    assert not (ver is None or too_old), "preflight would block a host that is fine"


def test_a_windows_host_without_the_dll_is_still_reported_missing() -> None:
    """The Windows check is what the check is for -- keep it."""
    ver, too_old = _read_as("Windows", dll_present=False)
    assert ver is None
    assert too_old is True


def test_a_windows_host_with_the_dll_is_not_reported_missing() -> None:
    ver, too_old = _read_as("Windows", dll_present=True)
    assert ver is not None
    assert too_old is False
