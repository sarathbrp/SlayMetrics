#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BENCHMARK_OS="macos"
export BENCHMARK_PROFILE="standard"

exec "${SCRIPT_DIR}/../run-benchmark-core.sh" "$@"
