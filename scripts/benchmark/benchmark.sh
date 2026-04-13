#!/bin/bash
# OS-dispatcher for benchmark runs.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_NAME="$(uname -s)"

case "$OS_NAME" in
    Darwin) TARGET_SCRIPT="${SCRIPT_DIR}/macos/benchmark.sh" ;;
    Linux)  TARGET_SCRIPT="${SCRIPT_DIR}/linux/benchmark.sh" ;;
    *)
        echo "Unsupported OS: ${OS_NAME}"
        echo "Supported OS targets: Darwin (macOS), Linux"
        exit 1
        ;;
esac

exec "$TARGET_SCRIPT" "$@"
