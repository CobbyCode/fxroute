#!/bin/sh
# SSH_ASKPASS helper for the Leap 16 ISO test runner. Prints the installer
# live password provided via FXROUTE_ASKPASS_PASSWORD.
set -eu
printf '%s' "${FXROUTE_ASKPASS_PASSWORD:?FXROUTE_ASKPASS_PASSWORD is required}"
