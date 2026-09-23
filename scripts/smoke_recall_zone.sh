#!/usr/bin/env bash
# smoke_recall_zone.sh — v1.15.1 (Ship c) minimal liveness gate
#
# Verifies the `--zone` shortcut on `am recall` is still wired correctly:
#   1. argparse still accepts the flag
#   2. all 4 zone values (failure / success / lesson / all-zones) are listed
#   3. cmd_recall body still reads args.zone
#   4. cli module compiles (no SyntaxError)
#
# Cheap (~5s), no LLM, no server. Designed to be nightly-cronnable.
# Exits 0 on PASS, 1 on any failure.

set -e

cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python}"
CLI_MODULE="astor_memory.cli.main"
MAIN_PY="astor_memory/cli/main.py"

echo "[smoke_recall_zone] py=$PY cli=$CLI_MODULE"

# 1. Module compiles
$PY -m py_compile $MAIN_PY

# 2. Static checks on source (catches accidental revert of --zone)
src=$($PY -c "import pkgutil, $CLI_MODULE; print(pkgutil.get_data('$CLI_MODULE', 'main.py').decode())")

echo "$src" | grep -q "add_argument('--zone'" || {
    echo "[FAIL] --zone flag missing from argparse"; exit 1; }

echo "$src" | grep -q "'failure':" || {
    echo "[FAIL] ZONE_KINDS['failure'] missing"; exit 1; }

echo "$src" | grep -q "'success':" || {
    echo "[FAIL] ZONE_KINDS['success'] missing"; exit 1; }

echo "$src" | grep -q "'lesson':" || {
    echo "[FAIL] ZONE_KINDS['lesson'] missing"; exit 1; }

echo "$src" | grep -q "'all-zones':" || {
    echo "[FAIL] ZONE_KINDS['all-zones'] missing"; exit 1; }

echo "$src" | grep -q "args.zone" || {
    echo "[FAIL] cmd_recall body not reading args.zone"; exit 1; }

# 3. argparse --help shows the flag (proves choices registration).
# argparse wraps long usage lines; check for both the flag and each zone value
# individually rather than expecting a single contiguous string.
help_out=$($PY -c "
import sys, runpy
sys.argv = ['am', 'recall', '--help']
try: runpy.run_module('$CLI_MODULE', run_name='__main__')
except SystemExit: pass
" 2>/dev/null || true)

echo "$help_out" | grep -q "\-\-zone" || {
    echo "[FAIL] --zone not visible in --help output"; exit 1; }

for z in failure success lesson all-zones; do
    echo "$help_out" | grep -q "$z" || {
        echo "[FAIL] zone value '$z' not visible in --help output"; exit 1; }
done

echo "[smoke_recall_zone] PASS — --zone shortcut alive, all 4 zones registered"