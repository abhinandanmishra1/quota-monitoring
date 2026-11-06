#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "=== Running Quota Metering Load Tests & Benchmarks ==="
export PYTHONPATH=src
./.venv/bin/python3 load_tests/run_benchmarks.py
