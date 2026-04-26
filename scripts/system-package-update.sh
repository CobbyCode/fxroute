#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "This helper must run as root" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get upgrade -y
elif command -v dnf >/dev/null 2>&1; then
  dnf -y upgrade --refresh
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive refresh
  zypper --non-interactive update
else
  echo "No supported package manager found" >&2
  exit 1
fi
