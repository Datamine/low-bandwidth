#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Linux system dependencies are not required on this platform."
  exit 0
fi

if command -v nethogs >/dev/null 2>&1; then
  echo "nethogs is already installed."
  exit 0
fi

run_as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
    return
  fi
  echo "This installer needs root privileges and could not find sudo."
  exit 1
}

install_with_apt() {
  run_as_root apt-get update
  run_as_root apt-get install -y nethogs
}

install_with_dnf() {
  run_as_root dnf install -y nethogs
}

install_with_yum() {
  run_as_root yum install -y nethogs
}

install_with_pacman() {
  run_as_root pacman -Sy --noconfirm nethogs
}

install_with_zypper() {
  run_as_root zypper --non-interactive install nethogs
}

install_with_apk() {
  run_as_root apk add nethogs
}

if command -v apt-get >/dev/null 2>&1; then
  install_with_apt
elif command -v dnf >/dev/null 2>&1; then
  install_with_dnf
elif command -v yum >/dev/null 2>&1; then
  install_with_yum
elif command -v pacman >/dev/null 2>&1; then
  install_with_pacman
elif command -v zypper >/dev/null 2>&1; then
  install_with_zypper
elif command -v apk >/dev/null 2>&1; then
  install_with_apk
else
  echo "Unsupported Linux package manager. Install nethogs manually."
  exit 1
fi

if command -v nethogs >/dev/null 2>&1; then
  echo "Installed nethogs successfully."
  exit 0
fi

echo "nethogs installation command completed, but nethogs is still not in PATH."
exit 1
