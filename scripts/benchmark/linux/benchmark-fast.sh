#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BENCHMARK_OS="linux"
export BENCHMARK_PROFILE="fast"

exec "${SCRIPT_DIR}/../run-benchmark-core.sh" "$@"
