#!/bin/bash
# install.sh — install astor-memory on Linux + macOS
#
# Tested on:
#   - Ubuntu 24.04 (primary CI)
#   - macOS 14 (Apple Silicon + Intel)
#   - Debian 12, Fedora 40
# Should work on any Linux/macOS with Python 3.10-3.13.
#
# Usage:
#   # Linux/macOS one-liner
#   curl -fsSL https://raw.githubusercontent.com/<repo_owner>/<repo_name>/main/scripts/install.sh | bash
#
#   # Or local
#   ./install.sh                       # install latest (interactive)
#   ./install.sh v1.13.1               # install specific version
#   ./install.sh --non-interactive     # install with defaults (no prompts)
#   ./install.sh --check               # verify install only
#   ./install.sh --uninstall           # remove astor-memory
#   ./install.sh --dir /path/to/data   # install to a custom directory
#
# Windows: use scripts/install.ps1 instead.

set -euo pipefail

VERSION="${1:-latest}"
NON_INTERACTIVE=0
CUSTOM_DIR=""

# Parse args
while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive|--no-prompt|-y)
            NON_INTERACTIVE=1
            shift
            ;;
        --dir)
            CUSTOM_DIR="$2"
            shift 2
            ;;
        --check)
            VERSION="__check__"
            shift
            ;;
        --uninstall)
            VERSION="__uninstall__"
            shift
            ;;
        --help|-h)
            VERSION="__help__"
            shift
            ;;
        v*) shift ;;
        *) shift ;;
    esac
done

# Detect OS for messaging
detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux) echo "linux" ;;
        *) echo "other" ;;
    esac
}

OS_TYPE="$(detect_os)"

ASTOR_HOME="${ASTOR_HOME:-${CUSTOM_DIR:-${HOME}/.astor}}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[err]\033[0m  %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [VERSION] [OPTIONS]

Install astor-memory on Linux or macOS (for Windows use install.ps1).

Arguments:
    VERSION              Specific release tag (e.g. v1.13.1). Default: latest.

Options:
    --non-interactive    Skip prompts; use defaults (good for CI/automation).
    --dir PATH           Install data to PATH (overrides ASTOR_HOME).
    --check              Verify install only (do not install).
    --uninstall          Remove astor-memory and its data directory.
    --help               Show this message.

Environment:
    ASTOR_HOME           Where to store data (default: \$HOME/.astor).
    PYTHON               Python interpreter to use (default: python3).

Examples:
    $0
    $0 v1.13.1
    $0 --non-interactive
    $0 --dir /opt/astor/data
    $0 --check
    ASTOR_HOME=/var/lib/astor $0

Detected OS: $OS_TYPE

After install:
    # Activate the venv
    source \$ASTOR_HOME/.venv/bin/activate

    # Verify
    am --version
    am doctor
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

check_python() {
    local py="${PYTHON:-python3}"

    # macOS Homebrew users may have python3 only in PATH after brew install
    if [ "$OS_TYPE" = "macos" ] && [ ! -command -v "$py" ]; then
        for alt in python3.13 python3.12 python3.11 python3.10 python3; do
            if command -v "$alt" >/dev/null 2>&1; then
                py="$alt"
                break
            fi
        done
    fi

    if ! command -v "$py" >/dev/null 2>&1; then
        die "Python not found. Install Python 3.10-3.13 first:
  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv
  Fedora:        sudo dnf install python3 python3-pip
  macOS (brew):  brew install python@3.11"
    fi

    local ver
    ver="$($py -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$ver" in
        3.10|3.11|3.12|3.13) log "Python $ver OK" ;;
        *) die "Python $ver unsupported. Need 3.10-3.13." ;;
    esac

    # Check venv module (Linux: python3-venv; macOS: bundled with python.org installer)
    if ! "$py" -c "import venv" >/dev/null 2>&1; then
        die "Python venv module missing.
  Ubuntu/Debian: sudo apt install python3-venv
  Fedora:        sudo dnf install python3-virtualenv
  macOS:         python.org installer includes venv; brew needs 'brew install python3'"
    fi

    PYTHON_BIN="$py"
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
# Interactive prompts
# ---------------------------------------------------------------------------

prompt_path() {
    if [ "$NON_INTERACTIVE" = "1" ]; then
        return 0
    fi
    local default="$ASTOR_HOME"
    local answer
    printf "Where should astor-memory store its data? [%s] " "$default"
    read -r answer
    if [ -n "$answer" ]; then
        ASTOR_HOME="$answer"
    fi
    log "Data directory: $ASTOR_HOME"
}

# ---------------------------------------------------------------------------
# Install / Uninstall / Verify
# ---------------------------------------------------------------------------

do_install() {
    check_python
    check_git

    prompt_path

    local venv="$ASTOR_HOME/.venv"

    log "Creating venv at $venv"
    mkdir -p "$ASTOR_HOME"
    "$PYTHON_BIN" -m venv "$venv"

    log "Upgrading pip"
    "$venv/bin/pip" install --quiet --upgrade pip

    if [ "$VERSION" = "latest" ]; then
        log "Installing astor-memory (latest from GitHub)"
        "$venv/bin/pip" install --quiet "git+https://github.com/<repo_owner>/<repo_name>.git"
    else
        log "Installing astor-memory $VERSION"
        "$venv/bin/pip" install --quiet "git+https://github.com/<repo_owner>/<repo_name>.git@${VERSION#v}"
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

Detected OS: $OS_TYPE
Data directory: $ASTOR_HOME
Python venv:    $venv

Next steps:
  1. Activate the venv:
       source $venv/bin/activate
  2. Verify install:
       am --version
       am doctor
  3. Initialize for multi-user mode (optional):
       export ASTOR_HOME=$ASTOR_HOME
       am bot add-user --platform telegram --chat-id <id> --role admin

To uninstall:
  $0 --uninstall [--dir $ASTOR_HOME]
EOF
}

do_check() {
    log "Checking astor-memory install ($OS_TYPE)"
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
    prompt_path
    log "Uninstalling astor-memory ($OS_TYPE)"
    if [ -d "$ASTOR_HOME/.venv" ]; then
        rm -rf "$ASTOR_HOME/.venv"
        log "Removed venv at $ASTOR_HOME/.venv"
    fi
    if [ -d "$ASTOR_HOME" ]; then
        if [ "$NON_INTERACTIVE" = "1" ]; then
            log "Skipping $ASTOR_HOME (data preserved; --non-interactive)"
        else
            local yn
            read -p "Remove data directory $ASTOR_HOME too? [y/N] " yn
            case "$yn" in
                [Yy]* ) rm -rf "$ASTOR_HOME"; log "Removed $ASTOR_HOME" ;;
                * ) log "Kept $ASTOR_HOME (data preserved)" ;;
            esac
        fi
    fi
    if command -v am >/dev/null 2>&1; then
        warn "am is still in PATH; remove the venv bin dir from PATH manually if needed"
    fi
    log "Uninstall complete"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

case "$VERSION" in
    __help__)       usage ;;
    __check__)      do_check ;;
    __uninstall__)  do_uninstall ;;
    *)              do_install ;;
esac
