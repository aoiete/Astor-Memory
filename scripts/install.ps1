<#
install.ps1 — install astor-memory on Windows

Tested on: Windows 10/11 (PowerShell 5.1+, recommended 7+).

Usage:
    # One-liner (from GitHub raw)
    iwr -useb https://raw.githubusercontent.com/<repo_owner>/<repo_name>/main/scripts/install.ps1 | iex

    # Or local
    .\install.ps1                          # install latest (interactive)
    .\install.ps1 v1.13.1                  # install specific version
    .\install.ps1 -NonInteractive          # install with defaults (no prompts)
    .\install.ps1 -Check                   # verify install only
    .\install.ps1 -Uninstall               # remove astor-memory
    .\install.ps1 -Dir 'D:\astor\data'     # install to custom directory

For Linux/macOS, use scripts/install.sh.
#>

[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [string]$Version = "latest",

    [string]$Dir,

    [switch]$NonInteractive,

    [switch]$Check,

    [switch]$Uninstall,

    [switch]$Help
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

$DefaultDataDir = if ($env:ASTOR_HOME) { $env:ASTOR_HOME } else { Join-Path $env:USERPROFILE ".astor" }

if ($Dir) {
    $DataDir = $Dir
} else {
    $DataDir = $DefaultDataDir
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log($msg) { Write-Host "[install] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[warn]    $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[err]     $msg" -ForegroundColor Red }
function Write-Ok($msg)  { Write-Host "[ok]      $msg" -ForegroundColor Green }

function Show-Usage {
@"
Usage: .\install.ps1 [VERSION] [OPTIONS]

Install astor-memory on Windows (PowerShell 5.1+).

Arguments:
    VERSION              Specific release tag (e.g. v1.13.1). Default: latest.

Options:
    -NonInteractive      Skip prompts; use defaults (good for CI/automation).
    -Dir PATH            Install data to PATH (overrides ASTOR_HOME).
    -Check               Verify install only (do not install).
    -Uninstall           Remove astor-memory and its data directory.
    -Help                Show this message.

Environment:
    ASTOR_HOME           Where to store data (default: `$env:USERPROFILE\.astor).
    PYTHON               Python interpreter to use (default: python).

Examples:
    .\install.ps1
    .\install.ps1 v1.13.1
    .\install.ps1 -NonInteractive
    .\install.ps1 -Dir 'D:\astor\data'
    .\install.ps1 -Check
    `$env:ASTOR_HOME = 'D:\astor\data'; .\install.ps1

Detected OS: Windows

After install:
    # Activate the venv
    & "$DataDir\.venv\Scripts\Activate.ps1"

    # Verify
    am --version
    am doctor
"@
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

function Find-Python {
    # Prefer PYTHON env var, then python launcher, then common paths.
    $candidates = @()
    if ($env:PYTHON) { $candidates += $env:PYTHON }
    $candidates += @("python", "python3", "py")
    foreach ($c in $candidates) {
        try {
            $cmd = Get-Command $c -ErrorAction SilentlyContinue
            if ($cmd) {
                $ver = & $cmd.Path -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($ver -match "^(3\.(10|11|12|13))$") {
                    Write-Log "Python $ver found at $($cmd.Path)"
                    return $cmd.Path
                }
            }
        } catch { }
    }
    return $null
}

function Find-Git {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    return $null
}

function Test-PythonOk {
    param([string]$Py)
    try {
        $ver = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -match "^(3\.(10|11|12|13))$") {
            return $true
        }
        Write-Err "Python $ver unsupported. Need 3.10-3.13."
        return $false
    } catch {
        Write-Err "Cannot run Python at $Py"
        return $false
    }
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

function Prompt-DataDir {
    if ($NonInteractive) { return }
    $answer = Read-Host "Where should astor-memory store its data? [$DataDir]"
    if ($answer) { $script:DataDir = $answer }
    Write-Log "Data directory: $DataDir"
}

# ---------------------------------------------------------------------------
# Install / Uninstall / Verify
# ---------------------------------------------------------------------------

function Install-Astor {
    $py = Find-Python
    if (-not $py) {
        Write-Err "Python 3.10-3.13 not found. Install first:"
        Write-Host "  - python.org: https://www.python.org/downloads/windows/"
        Write-Host "  - winget:     winget install Python.Python.3.11"
        Write-Host "  - chocolatey: choco install python311"
        exit 1
    }

    if (-not (Test-PythonOk -Py $py)) { exit 1 }

    $git = Find-Git
    if (-not $git) {
        Write-Err "git not found. Install first:"
        Write-Host "  - https://git-scm.com/download/win"
        Write-Host "  - winget install Git.Git"
        exit 1
    }

    Prompt-DataDir

    $venv = Join-Path $DataDir ".venv"

    # Create data dir + venv
    if (-not (Test-Path $DataDir)) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    }
    Write-Log "Creating venv at $venv"
    & $py -m venv $venv

    # Upgrade pip
    Write-Log "Upgrading pip"
    & "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip

    # Install astor-memory
    if ($Version -eq "latest") {
        Write-Log "Installing astor-memory (latest from GitHub)"
        & "$venv\Scripts\pip.exe" install --quiet "git+https://github.com/<repo_owner>/<repo_name>.git"
    } else {
        $tag = $Version.TrimStart("v")
        Write-Log "Installing astor-memory $Version"
        & "$venv\Scripts\pip.exe" install --quiet "git+https://github.com/<repo_owner>/<repo_name>.git@$tag"
    }

    # Initialize data dir on first install
    $busDb = Join-Path $DataDir "astor_bus_public.db"
    if (-not (Test-Path $busDb)) {
        Write-Log "Initializing ASTOR_HOME at $DataDir"
        $env:ASTOR_HOME = $DataDir
        & "$venv\Scripts\am.exe" init
    }

    # Smoke test
    Write-Log "Running am --version"
    & "$venv\Scripts\am.exe" --version

    Write-Ok "astor-memory installed."

    Write-Host ""
    Write-Host "Detected OS: Windows"
    Write-Host "Data directory: $DataDir"
    Write-Host "Python venv:    $venv"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Activate the venv:"
    Write-Host "       & '$venv\Scripts\Activate.ps1'"
    Write-Host "  2. Verify install:"
    Write-Host "       am --version"
    Write-Host "       am doctor"
    Write-Host "  3. Initialize for multi-user mode (optional):"
    Write-Host "       `$env:ASTOR_HOME = '$DataDir'"
    Write-Host "       am bot add-user --platform telegram --chat-id <id> --role admin"
    Write-Host ""
    Write-Host "To uninstall:"
    Write-Host "  .\install.ps1 -Uninstall [-Dir '$DataDir']"
}

function Check-Astor {
    Write-Log "Checking astor-memory install (Windows)"
    $am = Get-Command am -ErrorAction SilentlyContinue
    if (-not $am) {
        $amBin = Join-Path $DataDir ".venv\Scripts\am.exe"
        if (Test-Path $amBin) {
            $am = @{ Path = $amBin }
        } else {
            Write-Err "am not found in PATH or $amBin"
            exit 1
        }
    }
    & $am.Path --version
    & $am.Path doctor
    Write-Log "Install OK"
}

function Uninstall-Astor {
    Prompt-DataDir
    Write-Log "Uninstalling astor-memory (Windows)"
    $venv = Join-Path $DataDir ".venv"
    if (Test-Path $venv) {
        Remove-Item -Recurse -Force $venv
        Write-Log "Removed venv at $venv"
    }
    if (Test-Path $DataDir) {
        if ($NonInteractive) {
            Write-Log "Skipping $DataDir (data preserved; -NonInteractive)"
        } else {
            $yn = Read-Host "Remove data directory $DataDir too? [y/N]"
            if ($yn -match "^[Yy]") {
                Remove-Item -Recurse -Force $DataDir
                Write-Log "Removed $DataDir"
            } else {
                Write-Log "Kept $DataDir (data preserved)"
            }
        }
    }
    $am = Get-Command am -ErrorAction SilentlyContinue
    if ($am) {
        Write-Warn "am is still in PATH; remove the venv bin dir from PATH manually if needed"
    }
    Write-Log "Uninstall complete"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if ($Help) { Show-Usage; exit 0 }
if ($Check) { Check-Astor; exit 0 }
if ($Uninstall) { Uninstall-Astor; exit 0 }
Install-Astor
