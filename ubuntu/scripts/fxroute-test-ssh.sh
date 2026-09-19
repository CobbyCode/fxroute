#!/usr/bin/env bash
# FXRoute QEMU test SSH hook. Only in test-seed ISOs (never product).
# Without fxroute.live-password= on the kernel cmdline this is a no-op.
set -Eeuo pipefail

password=""
if [[ -r /proc/cmdline ]]; then
  for token in $(cat /proc/cmdline); do
    case "$token" in
      fxroute.live-password=*)
        password="${token#fxroute.live-password=}"
        ;;
    esac
  done
fi
[[ -n "$password" ]] || exit 0

user="ubuntu"
id -u "$user" >/dev/null 2>&1 || \
  user="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\// && $7 !~ /(nologin|false)$/ {print $1}' | head -n 1)"
[[ -n "$user" ]] || exit 0
printf '%s:%s\n' "$user" "$password" | chpasswd
systemctl start ssh.service 2>/dev/null \
  || systemctl start sshd.service 2>/dev/null || true
