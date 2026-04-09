#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
TERM_VALUE="${TERM:-xterm-256color}"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 is required but was not found in PATH."
  exit 1
fi

cd "${REPO_ROOT}"

if [[ "${EUID}" -eq 0 ]]; then
  exec env TERM="${TERM_VALUE}" \
    "${PYTHON_BIN}" "${REPO_ROOT}/run.py" "$@"
fi

# Prompt before launching curses so sudo never interrupts the UI.
sudo -v

exec sudo \
  --preserve-env=TERM,LANG,LC_ALL,LC_CTYPE,COLORTERM,TZ,TMUX,DISPLAY,XAUTHORITY,SUDO_UID,SUDO_GID,SUDO_USER \
  env TERM="${TERM_VALUE}" \
  "${PYTHON_BIN}" "${REPO_ROOT}/run.py" "$@"
