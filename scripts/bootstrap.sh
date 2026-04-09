#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required for bootstrap but was not found in PATH."
  exit 1
fi

cd "${REPO_ROOT}"
uv sync "$@"

if [[ "$(uname -s)" == "Linux" ]]; then
  "${SCRIPT_DIR}/install-linux-deps.sh"
fi

echo "Bootstrap complete."
