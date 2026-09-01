#!/bin/bash
# install.sh — install astor-memory on Linux
#
# Tested on: Ubuntu 24.04 (primary CI), macOS 14, should work on any Linux
# with Python 3.10-3.13.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aoiete/Astor-Memory/main/scripts/install.sh | bash
#   # or
#   ./install.sh            # latest release
#   ./install.sh v1.13.1    # specific tag
#   ./install.sh --check    # verify install only (no install)
#   ./install.sh --uninstall  # remove astor-memory

set -euo pipefail

VERSION="${1:-latest}"
ASTOR_HOME="${ASTOR_HOME:-$HOME/.astor}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo -e "\033[1;34m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*" >&2; }
err()  { echo -e "\033[1;31m[err]\033[0m  $*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [VERSION|--check|--uninstall|--help]

Install astor-memory (latest release by default).

Arguments:
    VERSION         Specific release tag (e.g. v1.13.1). Default: latest.

Options:
    --check         Verify install only (do not install).
    --uninstall     Remove astor-memory and its data directory.
    --help          Show this message.

Environment:
    ASTOR_HOME      Where to store data (default: \$HOME/.astor).
    PYTHON          Python interpreter to use (default: python3).

Examples:
    $0
    $0 v1.13.1
    $0 --check
    ASTOR_HOME=/var/lib/astor $0
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

check_python() {
    local py="${PYTHON:-python3}"
    if ! command -v "$py" >/dev/null 2>&1; then
        die "Python not found. Install Python 3.10-3.13 first:
  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv
  Fedora:        sudo dnf install python3 python3-pip
  macOS:         brew install python@3.11"
    fi
    local ver
    ver="$($py -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$ver" in
        3.10|3.11|3.12|3.13) log "Python $ver OK" ;;
        *) die "Python $ver unsupported. Need 3.10-3.13." ;;
    esac
}

check_git() {
    if ! command -v git >/dev/null 2>&1; then
        die "git not found. Install git first:
  Ubuntu/Debian: sudo apt install git
  Fedora:        sudo dnf install git
  macOS:         xcode-select --install"
    fi
}

# ---------------------------------------------------------------------------
# Install / Uninstall / Verify
# ---------------------------------------------------------------------------

do_install() {
    check_python
    check_git

    # Use a dedicated venv under ASTOR_HOME to keep system Python clean.
    local venv="$ASTOR_HOME/.venv"
    log "Creating venv at $venv"
    mkdir -p "$ASTOR_HOME"
    python3 -m venv "$venv"

    log "Upgrading pip"
    "$venv/bin/pip" install --quiet --upgrade pip

    if [ "$VERSION" = "latest" ]; then
        log "Installing astor-memory (latest from GitHub)"
        "$venv/bin/pip" install --quiet "git+https://github.com/aoiete/Astor-Memory.git"
    else
        log "Installing astor-memory $VERSION"
        "$venv/bin/pip" install --quiet "git+https://github.com/aoiete/Astor-Memory.git@${VERSION#v}"
    fi

    # Initialize ASTOR_HOME if first install.
    if [ ! -f "$ASTOR_HOME/astor_bus_public.db" ]; then
        log "Initializing ASTOR_HOME at $ASTOR_HOME"
        ASTOR_HOME="$ASTOR_HOME" "$venv/bin/am" init
    fi

    # Smoke test.
    log "Running am --version"
    "$venv/bin/am" --version

    cat <<EOF

\033[1;32m[ok]\033[0m astor-memory installed.

Next steps:
  1. Add $venv/bin/am to your PATH:
       export PATH="$venv/bin:\$PATH"
  2. Verify:   am doctor
  3. Initialize for multi-user mode:
       export ASTOR_HOME=/path/to/data
       am bot add-user --platform telegram --chat-id <id> --role admin

Data directory: $ASTOR_HOME
To uninstall:   $0 --uninstall
EOF
}

do_check() {
    log "Checking astor-memory install"
    if ! command -v am >/dev/null 2>&1 && [ ! -x "$ASTOR_HOME/.venv/bin/am" ]; then
        err "am not found in PATH or $ASTOR_HOME/.venv/bin/"
        exit 1
    fi
    local am_bin="am"
    [ ! -x "$(command -v am 2>/dev/null)" ] && am_bin="$ASTOR_HOME/.venv/bin/am"

    "$am_bin" --version
    "$am_bin" doctor
    log "Install OK"
}

do_uninstall() {
    log "Uninstalling astor-memory"
    if [ -d "$ASTOR_HOME/.venv" ]; then
        rm -rf "$ASTOR_HOME/.venv"
        log "Removed venv at $ASTOR_HOME/.venv"
    fi
    if [ -d "$ASTOR_HOME" ]; then
        read -p "Remove data directory $ASTOR_HOME too? [y/N] " yn
        case "$yn" in
            [Yy]* ) rm -rf "$ASTOR_HOME"; log "Removed $ASTOR_HOME" ;;
            * ) log "Kept $ASTOR_HOME (data preserved)" ;;
        esac
    fi
    if command -v am >/dev/null 2>&1; then
        warn "am is still in PATH; remove the venv bin dir from PATH manually if needed"
    fi
    log "Uninstall complete"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

case "${1:-}" in
    --help|-h)        usage ;;
    --check)          do_check ;;
    --uninstall)      do_uninstall ;;
    ""|v*)           do_install ;;
    *)               err "Unknown argument: $1"; usage; exit 1 ;;
esac