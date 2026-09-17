#!/bin/sh
# SSH_ASKPASS helper for the Ubuntu ISO QEMU tests (password from env).
set -eu
printf '%s' "${FXROUTE_ASKPASS_PASSWORD:?FXROUTE_ASKPASS_PASSWORD is not set}"
