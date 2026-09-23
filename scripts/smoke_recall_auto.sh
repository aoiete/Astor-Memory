#!/usr/bin/env bash
# smoke_recall_auto.sh — v1.15.2 (Ship b) minimal liveness gate
#
# Verifies the `am recall-auto` subcommand is wired correctly:
#   1. module compiles
#   2. argparse registers `recall-auto` subcommand
#   3. `am recall-auto --help` lists the text argument
#
# Cheap (~3s), no LLM, no server. Nightly-cron safe. Exits 0 on PASS.

set -e

cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python}"
CLI_MODULE="astor_memory.cli.main"
MAIN_PY="astor_memory/cli/main.py"

echo "[smoke_recall_auto] py=$PY cli=$CLI_MODULE"

# 1. Module compiles
$PY -m py_compile $MAIN_PY

# 2. argparse help check
help_out=$($PY -c "
import sys, runpy
sys.argv = ['am', 'recall-auto', '--help']
try: runpy.run_module('$CLI_MODULE', run_name='__main__')
except SystemExit: pass
" 2>/dev/null || true)

# 3. Verify it lists the text arg + the stdin hint (the help text)
#    mentions "Error message or tool output (use \"-\" for stdin)"
echo "$help_out" | grep -q "Error message or tool output" || {
    echo "[FAIL]recall-auto help missing text arg description"; exit 1; }

# 4. Verify `am --help` (top-level) lists recall-auto as a subcommand
top_out=$($PY -c "
import sys, runpy
sys.argv = ['am', '--help']
try: runpy.run_module('$CLI_MODULE', run_name='__main__')
except SystemExit: pass
" 2>/dev/null || true)

echo "$top_out" | grep -q "recall-auto" || {
    echo "[FAIL]recall-auto not listed under 'am --help'"; exit 1; }

echo "[smoke_recall_auto] PASS — am recall-auto subcommand wired"