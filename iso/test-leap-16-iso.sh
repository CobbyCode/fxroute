#!/usr/bin/env bash
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_SELECTION="${1:-all}"
ISO_PATH="${2:-${FXROUTE_ISO:-$ROOT_DIR/dist/fxroute-leap-16-x86_64.iso}}"
TEST_ROOT="${FXROUTE_ISO_TEST_DIR:-$ROOT_DIR/dist/iso-test}"
VM_RAM="${FXROUTE_VM_RAM:-4096}"
VM_CPUS="${FXROUTE_VM_CPUS:-4}"
VM_DISK_GB="${FXROUTE_VM_DISK_GB:-40}"
VM_EXTRA_DISK_GB="${FXROUTE_VM_EXTRA_DISK_GB:-5}"
TIMEOUT_SECONDS="${FXROUTE_ISO_TEST_TIMEOUT:-5400}"
KEEP_TEST_ROOT="${FXROUTE_KEEP_ISO_TEST:-0}"
AGAMA_USER="${FXROUTE_ISO_USER:-fxroute}"
AGAMA_USER_PASSWORD="${FXROUTE_ISO_USER_PASSWORD:-}"
AGAMA_LIVE_PASSWORD="${FXROUTE_ISO_LIVE_PASSWORD:-}"
LIVE_USER="${FXROUTE_ISO_TRY_USER:-fxroute}"
LIVE_PASSWORD="${FXROUTE_ISO_TRY_PASSWORD:-}"
AGAMA_LOCALE="${FXROUTE_ISO_LOCALE:-en_US.UTF-8}"
AGAMA_KEYMAP="${FXROUTE_ISO_KEYMAP:-us}"
AGAMA_TIMEZONE="${FXROUTE_ISO_TIMEZONE:-Europe/Berlin}"

usage() {
  cat <<EOF
Usage: $0 [all|headless|desktop|try|live] [ISO]

Boot a fresh QEMU/KVM guest for each selected profile, drive the
interactive Agama decisions through the installer's own Agama CLI
(account/password, locale/keyboard/timezone, explicit target-disk
selection, install start), and verify the installed FXRoute service,
DSP setup, and desktop selection. SSH into the installed system uses
the account password created in Agama.

The try/live profile boots the non-persistent Try FXRoute system
directly (no Agama install) and verifies /api/status live:true,
fxroute_dsp_sink, RAM overlay, reboot volatility, and that internal
disks are not auto-mounted.

The installer live password must be known for the installer SSH access:
append live.password=... to the boot entry for automated runs (the
runner types FXROUTE_ISO_KERNEL_EXTRA into the GRUB editor), or read
the generated password from the installer console for manual runs, and
pass it as FXROUTE_ISO_LIVE_PASSWORD. The account password created in
Agama comes from FXROUTE_ISO_USER_PASSWORD. The Agama web ports are
forwarded to the host so the same decisions can also be made manually
in a browser via https://agama.local or the forwarded ports.
For try/live, pass the live SSH password as FXROUTE_ISO_TRY_PASSWORD
(booted as fxroute.live-password=...); release ISOs keep sshd disabled
without it.

Environment:
  FXROUTE_ISO_USER           Account name created in Agama (default: fxroute)
  FXROUTE_ISO_USER_PASSWORD  Account password created in Agama (required for headless/desktop/all)
  FXROUTE_ISO_LIVE_PASSWORD  Installer live password for the installer SSH (required for headless/desktop/all)
  FXROUTE_ISO_TRY_USER       Live user for try (default: fxroute)
  FXROUTE_ISO_TRY_PASSWORD   Live SSH password for try verification (required for try/live/all)
  FXROUTE_ISO_LOCALE         Locale selected in Agama (default: en_US.UTF-8)
  FXROUTE_ISO_KEYMAP         Keymap selected in Agama (default: us)
  FXROUTE_ISO_TIMEZONE       Timezone selected in Agama (default: Europe/Berlin)

Test disks and logs are stored under FXROUTE_ISO_TEST_DIR
(default: dist/iso-test).
EOF
}

die() {
  printf '[iso-test][error] %s\n' "$*" >&2
  exit 1
}

case "$PROFILE_SELECTION" in
  all) PROFILES=(headless desktop try) ;;
  headless|desktop|try|live)
    if [[ "$PROFILE_SELECTION" == "live" ]]; then
      PROFILES=(try)
    else
      PROFILES=("$PROFILE_SELECTION")
    fi
    ;;
  -h|--help) usage; exit 0 ;;
  *) die "Profile must be all, headless, desktop, try, or live" ;;
esac

[[ -f "$ISO_PATH" ]] || die "ISO not found: $ISO_PATH"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img is required"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"
command -v setsid >/dev/null 2>&1 || die "setsid is required for the installer password login"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v base64 >/dev/null 2>&1 || die "base64 is required for the interactive setup phase"
needs_install_profile=0
needs_try_profile=0
for _p in "${PROFILES[@]}"; do
  case "$_p" in
    headless|desktop) needs_install_profile=1 ;;
    try) needs_try_profile=1 ;;
  esac
done
if [[ "$needs_install_profile" -eq 1 ]]; then
  [[ -n "$AGAMA_USER_PASSWORD" ]] || die "FXROUTE_ISO_USER_PASSWORD is required (account password created in Agama)"
  [[ -n "$AGAMA_LIVE_PASSWORD" ]] || die "FXROUTE_ISO_LIVE_PASSWORD is required (installer live password for the installer SSH)"
fi
if [[ "$needs_try_profile" -eq 1 ]]; then
  [[ -n "$LIVE_PASSWORD" ]] || die "FXROUTE_ISO_TRY_PASSWORD is required (live SSH password for try verification)"
  # Typed into the GRUB editor via QEMU monitor sendkey (alnum plus - = . / ,).
  [[ "$LIVE_PASSWORD" =~ ^[A-Za-z0-9=.,/-]+$ ]] \
    || die "FXROUTE_ISO_TRY_PASSWORD must match [A-Za-z0-9=.,/-]+ (GRUB sendkey typing)"
fi
[[ "$AGAMA_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "FXROUTE_ISO_USER must be a valid Unix username"
[[ "$LIVE_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "FXROUTE_ISO_TRY_USER must be a valid Unix username"

if [[ -e "$TEST_ROOT" ]]; then
  die "Test root already exists; remove it before rerunning: $TEST_ROOT"
fi
mkdir -p "$TEST_ROOT"
PIDS=()
wait_for_qemu_processes() {
  local alive=0
  local pid=""

  for _ in $(seq 1 30); do
    alive=0
    for pid in "${PIDS[@]:-}"; do
      [[ -n "$pid" ]] || continue
      if kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    [[ "$alive" -eq 0 ]] && return 0
    sleep 1
  done

  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -KILL "$pid" 2>/dev/null || true
  done
}

cleanup() {
  local pid
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done
  wait_for_qemu_processes
  if [[ "$KEEP_TEST_ROOT" -eq 0 ]]; then
    rm -rf -- "$TEST_ROOT"
  else
    printf '[iso-test] keeping test root: %s\n' "$TEST_ROOT"
  fi
}
trap cleanup EXIT

find_free_port() {
  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_HTTP_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_HTTP_PORT:-$(find_free_port)}" ;;
    try) printf '%s\n' "${FXROUTE_ISO_TEST_TRY_HTTP_PORT:-$(find_free_port)}" ;;
  esac
}

ssh_port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_SSH_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_SSH_PORT:-$(find_free_port)}" ;;
    try) printf '%s\n' "${FXROUTE_ISO_TEST_TRY_SSH_PORT:-$(find_free_port)}" ;;
  esac
}

agama_https_port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_AGAMA_HTTPS_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_AGAMA_HTTPS_PORT:-$(find_free_port)}" ;;
    try) printf '%s\n' "${FXROUTE_ISO_TEST_TRY_AGAMA_HTTPS_PORT:-$(find_free_port)}" ;;
  esac
}

agama_http_port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_AGAMA_HTTP_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_AGAMA_HTTP_PORT:-$(find_free_port)}" ;;
    try) printf '%s\n' "${FXROUTE_ISO_TEST_TRY_AGAMA_HTTP_PORT:-$(find_free_port)}" ;;
  esac
}

select_boot_entry() {
  local profile="$1"
  local monitor="$2"
  local down_count=""

  # The BIOS GRUB menu contains "Boot from Hard Disk", the regular installer,
  # Desktop, Headless, and Try in that order (Try is staged first so it ends
  # up last and Desktop/Headless downs stay stable). Start from the first
  # entry so this remains independent of any saved GRUB selection.
  case "$profile" in
    desktop) down_count=2 ;;
    headless) down_count=3 ;;
    try) down_count=4 ;;
    *) die "Unknown profile: $profile" ;;
  esac

  for _ in $(seq 1 20); do
    [[ -S "$monitor" ]] && break
    sleep 1
  done
  [[ -S "$monitor" ]] || die "QEMU monitor did not start for $profile"
  FXROUTE_ISO_KERNEL_EXTRA="${FXROUTE_ISO_KERNEL_EXTRA:-}" \
  FXROUTE_ISO_GRUB_LINUX_DOWNS="${FXROUTE_ISO_GRUB_LINUX_DOWNS:-4}" \
  FXROUTE_ISO_GRUB_EDIT_SHOT="${FXROUTE_ISO_GRUB_EDIT_SHOT:-}" \
  python3 - "$monitor" "$down_count" <<'PY'
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

monitor, down_count = sys.argv[1:]
kernel_extra = os.environ.get("FXROUTE_ISO_KERNEL_EXTRA", "")
linux_downs = int(os.environ.get("FXROUTE_ISO_GRUB_LINUX_DOWNS", "4"))
edit_shot = os.environ.get("FXROUTE_ISO_GRUB_EDIT_SHOT", "")
screen_fd, screen_name = tempfile.mkstemp(
    prefix="fxroute-iso-grub-", suffix=".ppm"
)
os.close(screen_fd)
screen_path = Path(screen_name)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5)
sock.connect(monitor)
sock.setblocking(False)


def drain_monitor_output():
    while True:
        try:
            if not sock.recv(4096):
                return
        except BlockingIOError:
            return


def monitor_command(command):
    sock.sendall((command + "\n").encode())
    time.sleep(0.05)
    drain_monitor_output()


def hmp_quote_path(path):
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


KEY_NAMES = {
    " ": "spc",
    "-": "minus",
    "=": "equal",
    ".": "dot",
    "/": "slash",
    ",": "comma",
}


def sendkey(key):
    monitor_command(f"sendkey {key}")


def type_text(value):
    for char in value:
        if char.isascii() and char.isalnum():
            if char.isupper():
                monitor_command(f"sendkey shift-{char.lower()}")
            else:
                sendkey(char.lower())
        elif char in KEY_NAMES:
            sendkey(KEY_NAMES[char])
        else:
            raise SystemExit(f"unsupported kernel-extra character: {char!r}")
        time.sleep(0.02)


def grub_menu_ready(path):
    try:
        data = path.read_bytes()
    except OSError:
        return False
    header = data.split(b"\n", 3)
    if len(header) != 4 or header[0] != b"P6":
        return False
    try:
        width, height = (int(value) for value in header[1].split())
        max_value = int(header[2])
    except ValueError:
        return False
    if width < 1 or height < 1 or max_value != 255:
        return False
    pixels = header[3]
    if len(pixels) < width * height * 3:
        return False

    # The openSUSE GRUB theme draws the active menu item as a wide, uniform
    # bar. This distinguishes the menu from SeaBIOS and its loading screen.
    background = tuple(pixels[:3])
    highlighted_rows = 0
    for row in range(height):
        row_start = row * width * 3
        row_end = row_start + width * 3
        previous = background
        run_length = 0
        longest_run = 0
        for offset in range(row_start, row_end, 3):
            color = tuple(pixels[offset : offset + 3])
            if color != background and color == previous:
                run_length += 1
            elif color != background:
                run_length = 1
            else:
                run_length = 0
            previous = color
            longest_run = max(longest_run, run_length)
        if longest_run >= max(64, width // 4):
            highlighted_rows += 1
    return highlighted_rows >= 5


try:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            screen_path.unlink()
        except FileNotFoundError:
            pass
        monitor_command(f"screendump {hmp_quote_path(screen_path)}")
        for _ in range(10):
            if grub_menu_ready(screen_path):
                monitor_command("sendkey home")
                for _ in range(int(down_count)):
                    monitor_command("sendkey down")
                if kernel_extra:
                    # Edit the entry to append test-only kernel options
                    # (for example live.password=... for Agama API access).
                    # The editor shows a leading setparams line plus blank
                    # separator rows; four downs from the top reach the
                    # wrapped linux line (verified via screendump).
                    sendkey("e")
                    time.sleep(2.0)
                    for _ in range(6):
                        sendkey("up")
                        time.sleep(0.1)
                    for _ in range(linux_downs):
                        sendkey("down")
                        time.sleep(0.15)
                    sendkey("end")
                    time.sleep(0.15)
                    sendkey("spc")
                    type_text(kernel_extra)
                    time.sleep(0.3)
                    if edit_shot:
                        monitor_command(f"screendump {hmp_quote_path(edit_shot)}")
                        time.sleep(0.3)
                    monitor_command("sendkey ctrl-x")
                else:
                    monitor_command("sendkey ret")
                raise SystemExit(0)
            time.sleep(0.05)
        time.sleep(0.2)
    raise SystemExit("timed out waiting for the GRUB menu")
finally:
    sock.close()
    try:
        screen_path.unlink()
    except FileNotFoundError:
        pass
PY
}

ssh_installer() {
  local ssh_port="$1"
  shift
  SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" \
  SSH_ASKPASS_REQUIRE=force \
  DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$AGAMA_LIVE_PASSWORD" \
  setsid ssh \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -o ConnectTimeout=8 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -p "$ssh_port" root@127.0.0.1 "$@" < /dev/null
}

installer_config_load() {
  local ssh_port="$1"
  local payload_file="$2"
  local remote_file="$3"
  local payload=""

  payload="$(base64 -w0 < "$payload_file")"
  ssh_installer "$ssh_port" \
    "echo '$payload' | base64 -d > '$remote_file' && agama config load '$remote_file'"
}

setup_agama_interactive() {
  local profile="$1"
  local agama_port="$2"
  local ssh_port="$3"
  local pid="$4"
  local started=$SECONDS
  local setup_file="$TEST_ROOT/$profile-agama-setup.json"
  local storage_file="$TEST_ROOT/$profile-agama-storage.json"

  printf '[iso-test] waiting for the %s Agama API\n' "$profile"
  while (( SECONDS - started < 900 )); do
    kill -0 "$pid" 2>/dev/null || {
      printf '[iso-test] QEMU exited while waiting for the %s Agama API\n' "$profile" >&2
      return 1
    }
    if curl --insecure --fail --silent --connect-timeout 3 --max-time 10 \
        "https://127.0.0.1:$agama_port/" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  (( SECONDS - started < 900 )) || {
    printf '[iso-test] timeout waiting for the %s Agama API\n' "$profile" >&2
    return 1
  }

  # The installer is driven through its own Agama CLI over a root SSH
  # session authenticated with the live password; the host CLI may be
  # newer than the installer's Agama API.
  printf '[iso-test] waiting for the %s installer SSH\n' "$profile"
  while (( SECONDS - started < 1200 )); do
    kill -0 "$pid" 2>/dev/null || {
      printf '[iso-test] QEMU exited while waiting for the %s installer SSH\n' "$profile" >&2
      return 1
    }
    if ssh_installer "$ssh_port" true >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  (( SECONDS - started < 1200 )) || {
    printf '[iso-test] timeout waiting for the %s installer SSH\n' "$profile" >&2
    return 1
  }

  # Account/password and locale/keyboard/timezone are the interactive
  # decisions; the release image ships no credentials of its own.
  ssh_installer "$ssh_port" 'agama config show' > "$TEST_ROOT/$profile-agama-current.json"
  AGAMA_L10N_KEY="$(python3 - "$TEST_ROOT/$profile-agama-current.json" <<'PY'
import json
import sys

current = json.load(open(sys.argv[1]))
print("l10n" if "l10n" in current else "localization")
PY
)"
  AGAMA_USER="$AGAMA_USER" AGAMA_USER_PASSWORD="$AGAMA_USER_PASSWORD" \
    AGAMA_LOCALE="$AGAMA_LOCALE" \
    AGAMA_KEYMAP="$AGAMA_KEYMAP" AGAMA_TIMEZONE="$AGAMA_TIMEZONE" \
    AGAMA_L10N_KEY="$AGAMA_L10N_KEY" \
    python3 - "$setup_file" <<'PY'
import json
import os
import sys

l10n_key = os.environ["AGAMA_L10N_KEY"]
if l10n_key == "l10n":
    locale_section = {
        "locale": os.environ["AGAMA_LOCALE"],
        "keymap": os.environ["AGAMA_KEYMAP"],
        "timezone": os.environ["AGAMA_TIMEZONE"],
    }
else:
    locale_section = {
        "language": os.environ["AGAMA_LOCALE"],
        "keyboard": os.environ["AGAMA_KEYMAP"],
        "timezone": os.environ["AGAMA_TIMEZONE"],
    }
config = {
    "user": {
        "fullName": "FXRoute",
        "userName": os.environ["AGAMA_USER"],
        "password": os.environ["AGAMA_USER_PASSWORD"],
    },
    l10n_key: locale_section,
}
with open(sys.argv[1], "w") as handle:
    json.dump(config, handle)
PY
  installer_config_load "$ssh_port" "$setup_file" /tmp/fxroute-agama-setup.json

  # Loads apply asynchronously ("Configure software" re-solve); wait until
  # the account is visible before sending the next change, otherwise a
  # slow apply can overwrite it.
  printf '[iso-test] waiting for the %s account to apply\n' "$profile"
  started=$SECONDS
  while (( SECONDS - started < 600 )); do
    if ssh_installer "$ssh_port" 'agama config show' 2>/dev/null | python3 -c "
import json
import sys

try:
    config = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if config.get('user', {}).get('userName') == '$AGAMA_USER' else 1)
"; then
      break
    fi
    sleep 5
  done
  ssh_installer "$ssh_port" 'agama config show' 2>/dev/null | python3 -c "
import json
import sys

config = json.load(sys.stdin)
raise SystemExit(0 if config.get('user', {}).get('userName') == '$AGAMA_USER' else 1)
" || { printf '[iso-test] the %s account did not apply\n' "$profile" >&2; return 1; }

  # Explicit target-disk selection: a small decoy disk is attached besides
  # the main disk, so the proposal must name the large disk. Never wipe a
  # disk without this explicit selection. The alias keeps the bootloader
  # reference (boot.device) intact.
  python3 - <<PY
import json

storage = {
    "storage": {
        "drives": [
            {
                "alias": "boot",
                "search": {
                    "condition": {"size": {"greater": "30 GiB"}},
                    "max": 1,
                },
                "partitions": [{"generate": "default"}],
            }
        ]
    }
}
with open("$storage_file", "w") as handle:
    json.dump(storage, handle)
PY
  installer_config_load "$ssh_port" "$storage_file" /tmp/fxroute-agama-storage.json
  printf '[iso-test] waiting for the %s target-disk selection to apply\n' "$profile"
  started=$SECONDS
  while (( SECONDS - started < 600 )); do
    if ssh_installer "$ssh_port" 'agama config show' 2>/dev/null | python3 -c "
import json
import sys

try:
    config = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if '\"greater\": \"30 GiB\"' in json.dumps(config.get('storage', {})) else 1)
"; then
      break
    fi
    sleep 5
  done
  ssh_installer "$ssh_port" 'agama config show' > "$TEST_ROOT/$profile-agama-proposal.json"
  python3 - "$TEST_ROOT/$profile-agama-proposal.json" <<'PY'
import json
import sys

proposal = json.load(open(sys.argv[1]))
text = json.dumps(proposal)
assert '"greater": "30 GiB"' in text, "explicit target-disk selection is missing"
assert '"alias": "boot"' in text, "boot drive alias is missing"
PY
  start_agama_install "$ssh_port" "$profile"
}

start_agama_install() {
  local ssh_port="$1"
  local profile="$2"
  local attempt=1

  # agama install refuses while preconditions are unmet (for example while
  # the initial probing is still running) without changing anything, so
  # retry until the installation actually starts. The SSH call is the
  # explicit confirmation of the destructive step. The CLI leaves the
  # installer in the live environment after the install phase; finish it
  # explicitly so the requested reboot is performed.
  while (( attempt <= 8 )); do
    printf '[iso-test] starting the %s installation, attempt %d (explicit confirmation)\n' "$profile" "$attempt"
    ssh_installer "$ssh_port" \
      "rm -f /tmp/fxroute-agama-install.log; nohup bash -c 'agama install > /tmp/fxroute-agama-install.log 2>&1 && agama finish reboot >> /tmp/fxroute-agama-install.log 2>&1' < /dev/null > /dev/null 2>&1 & echo \$!" \
      > "$TEST_ROOT/$profile-install-pid" 2>/dev/null || true
    sleep 45
    ssh_installer "$ssh_port" 'cat /tmp/fxroute-agama-install.log' \
      > "$TEST_ROOT/$profile-install.log" 2>&1 || true
    if grep -q '\[1/3\]\|\[2/3\]\|\[3/3\]' "$TEST_ROOT/$profile-install.log" 2>/dev/null; then
      printf '[iso-test] the %s installation started\n' "$profile"
      return 0
    fi
    printf '[iso-test] the %s installation did not start yet, retrying\n' "$profile" >&2
    tail -5 "$TEST_ROOT/$profile-install.log" >&2 || true
    sleep 45
    attempt=$((attempt + 1))
  done
  printf '[iso-test] the %s installation failed to start\n' "$profile" >&2
  cat "$TEST_ROOT/$profile-install.log" >&2 || true
  return 1
}

ssh_guest() {
  local ssh_port="$1"
  shift
  SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" \
  SSH_ASKPASS_REQUIRE=force \
  DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$AGAMA_USER_PASSWORD" \
  setsid ssh \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=1 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -p "$ssh_port" "$AGAMA_USER@127.0.0.1" "$@"
}

sudo_guest() {
  local ssh_port="$1"
  shift
  printf '%s\n' "$AGAMA_USER_PASSWORD" | ssh_guest "$ssh_port" sudo -S -- "$@"
}

sudo_guest_script() {
  local ssh_port="$1"
  shift
  {
    printf '%s\n' "$AGAMA_USER_PASSWORD"
    cat
  } | ssh_guest "$ssh_port" sudo -S -- "$@"
}

stop_guest() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"

  sudo_guest "$ssh_port" systemctl poweroff >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  printf '[iso-test] forcing QEMU shutdown for %s\n' "$profile" >&2
  kill "$pid" 2>/dev/null || true
}

wait_for_guest() {
  local profile="$1"
  local pid="$2"
  local http_port="$3"
  local ssh_port="$4"
  local started=$SECONDS
  local status_file="$TEST_ROOT/$profile-status.json"

  while (( SECONDS - started < TIMEOUT_SECONDS )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '[iso-test] QEMU exited while waiting for %s\n' "$profile" >&2
      tail -100 "$TEST_ROOT/$profile-serial.log" >&2 || true
      tail -100 "$TEST_ROOT/$profile-qemu.log" >&2 || true
      return 1
    fi
    if ssh_guest "$ssh_port" test -f /var/lib/fxroute-iso/install-failed >/dev/null 2>&1; then
      printf '[iso-test] %s first-boot setup failed\n' "$profile" >&2
      sudo_guest "$ssh_port" cat /var/lib/fxroute-iso/install-failed >&2 || true
      sudo_guest "$ssh_port" systemctl status fxroute-first-boot.service --no-pager -l >&2 || true
      return 1
    fi
    if ssh_guest "$ssh_port" test -f /var/lib/fxroute-iso/install-complete >/dev/null 2>&1; then
      if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
          "http://127.0.0.1:$http_port/api/status" > "$status_file"; then
        printf '[iso-test] %s guest is installed and FXRoute is serving HTTP\n' "$profile"
        return 0
      fi
    fi
    sleep 5
  done
  printf '[iso-test] timeout waiting for %s\n' "$profile" >&2
  tail -160 "$TEST_ROOT/$profile-serial.log" >&2 || true
  tail -160 "$TEST_ROOT/$profile-qemu.log" >&2 || true
  return 1
}

verify_ssh_defaults() {
  local ssh_port="$1"
  sudo_guest_script "$ssh_port" bash -s <<'EOF'
set -Eeuo pipefail
test -f /etc/ssh/sshd_config.d/90-fxroute-iso.conf
grep -Fxq 'PermitRootLogin no' /etc/ssh/sshd_config.d/90-fxroute-iso.conf
grep -Fxq 'PasswordAuthentication yes' /etc/ssh/sshd_config.d/90-fxroute-iso.conf
grep -Fxq 'PubkeyAuthentication yes' /etc/ssh/sshd_config.d/90-fxroute-iso.conf
sshd -T | grep -Fx 'passwordauthentication yes' >/dev/null
sshd -T | grep -Fx 'permitrootlogin no' >/dev/null
! sshd -T | grep -Fx 'passwordauthentication no' >/dev/null
! sshd -T | grep -Fx 'kbdinteractiveauthentication no' >/dev/null
EOF
  sudo_guest_script "$ssh_port" bash -s -- "$AGAMA_USER" <<'EOF'
set -Eeuo pipefail
account="$1"
root_password_hash="$(getent shadow root | cut -d: -f2)"
case "$root_password_hash" in
  ""|\!*|\**)
    ;;
  *)
    printf 'root password is unexpectedly set\n' >&2
    exit 1
    ;;
esac
user_password_hash="$(getent shadow "$account" | cut -d: -f2)"
case "$user_password_hash" in
  ""|\!*|\**)
    printf 'the Agama account password is missing\n' >&2
    exit 1
    ;;
esac
EOF
  if SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" \
      SSH_ASKPASS_REQUIRE=force \
      DISPLAY=:0 \
      FXROUTE_ASKPASS_PASSWORD="$AGAMA_USER_PASSWORD" \
      setsid ssh \
        -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no \
        -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -p "$ssh_port" root@127.0.0.1 true < /dev/null >/dev/null 2>&1; then
    printf '[iso-test] root SSH unexpectedly succeeded\n' >&2
    return 1
  fi
}

verify_account_region_network_storage() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s -- "$AGAMA_USER" "$AGAMA_TIMEZONE" "$AGAMA_LOCALE" "$AGAMA_KEYMAP" <<'EOF'
set -Eeuo pipefail
account="$1"
timezone="$2"
locale="$3"
keymap="$4"
test "$(id -un)" = "$account"
[[ -d "$HOME" ]]
test "$(timedatectl show -p Timezone --value)" = "$timezone"
localectl status | grep -Fq "System Locale: LANG=$locale"
localectl status | grep -Fq "VC Keymap: $keymap"
nmcli -t -f NAME connection show | grep -q .
ip -4 route show default | grep -q .
root_source="$(findmnt -n -o SOURCE /)"
root_block_device="${root_source%%[*}"
root_disk="$(lsblk -n -o PKNAME "$root_block_device" | head -1)"
[[ -n "$root_disk" ]]
root_disk_size_bytes="$(lsblk -n -b -o SIZE "/dev/$root_disk" | head -1)"
[[ "$root_disk_size_bytes" -gt 30000000000 ]]
EOF
}

reboot_guest() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"
  local old_boot_id
  local new_boot_id

  printf '[iso-test] rebooting %s guest to verify its configured boot target\n' "$profile"
  old_boot_id="$(ssh_guest "$ssh_port" cat /proc/sys/kernel/random/boot_id)"
  sudo_guest "$ssh_port" systemctl reboot >/dev/null 2>&1 || true
  for _ in $(seq 1 90); do
    kill -0 "$pid" 2>/dev/null || {
      printf '[iso-test] QEMU exited while rebooting %s\n' "$profile" >&2
      return 1
    }
    new_boot_id="$(ssh_guest "$ssh_port" cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
    if [[ -n "$new_boot_id" && "$new_boot_id" != "$old_boot_id" ]]; then
      return 0
    fi
    sleep 2
  done
  printf '[iso-test] timeout waiting for %s guest reboot\n' "$profile" >&2
  return 1
}

verify_headless() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s -- "$AGAMA_USER" <<'EOF'
set -Eeuo pipefail
account="$1"
test -f /var/lib/fxroute-iso/install-complete
test -x /usr/local/libexec/fxroute-first-boot-install.sh
systemctl is-enabled fxroute-first-boot.service
systemctl is-active fxroute-first-boot.service
test "$(systemctl get-default)" = multi-user.target
! rpm -q plasma6-session >/dev/null 2>&1
rpm -q openssh-server >/dev/null
test -d "$HOME/fxroute/.git"
test "$(git -C "$HOME/fxroute" remote get-url origin)" = 'https://github.com/CobbyCode/fxroute.git'
bash -lc 'cd ~/fxroute && scripts/update_fxroute.sh --check >/tmp/fxroute-update-check.log 2>&1'
grep -Eqi 'already up to date|update available|reconciliation is incomplete' /tmp/fxroute-update-check.log
test -x "$HOME/fxroute/native_dsp/build/fxroute-dsp"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user is-enabled fxroute.service
systemctl --user is-active fxroute.service
pactl list sinks short | awk '{print $2}' | grep -Fx 'fxroute_dsp_sink' >/dev/null
curl --fail --silent http://127.0.0.1:8000/api/status >/dev/null
EOF
}

verify_desktop() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s -- "$AGAMA_USER" <<'EOF'
set -Eeuo pipefail
account="$1"
trap 'status=$?; printf "[iso-test] verify_desktop failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR
test -f /var/lib/fxroute-iso/install-complete
test -x /usr/local/libexec/fxroute-first-boot-install.sh
systemctl is-enabled fxroute-first-boot.service
systemctl is-active fxroute-first-boot.service
test "$(systemctl get-default)" = graphical.target
rpm -q plasma6-session sddm-qt6 MozillaFirefox >/dev/null
! rpm -q google-chrome-stable >/dev/null 2>&1
test ! -e /etc/zypp/repos.d/google-chrome.repo
test -f /etc/sddm.conf.d/10-fxroute-autologin.conf
grep -Fxq "User=$account" /etc/sddm.conf.d/10-fxroute-autologin.conf
grep -Fxq 'Session=default.desktop' /etc/sddm.conf.d/10-fxroute-autologin.conf
systemctl is-enabled display-manager.service
test "$(readlink -f /etc/systemd/system/display-manager.service)" = /usr/lib/systemd/system/display-manager-legacy.service
test -f /etc/systemd/logind.conf.d/10-fxroute-appliance.conf
grep -Fxq 'HandleLidSwitch=ignore' /etc/systemd/logind.conf.d/10-fxroute-appliance.conf
test -f /usr/share/wallpapers/fxroute-wallpaper.png
test -f /usr/share/pixmaps/fxroute.svg
test -f "$HOME/Desktop/FXRoute.desktop"
grep -Fq 'http://127.0.0.1:8000/' "$HOME/Desktop/FXRoute.desktop"
test -f "$HOME/Desktop/Spotify Download.desktop"
grep -Fq 'https://www.spotify.com/download/linux/' "$HOME/Desktop/Spotify Download.desktop"
test -f "$HOME/.config/kwalletrc"
grep -Fxq 'Enabled=false' "$HOME/.config/kwalletrc"
test -f "$HOME/.config/kscreenlockerrc"
grep -Fxq 'Autolock=false' "$HOME/.config/kscreenlockerrc"
test -f "$HOME/.config/powerdevilrc"
grep -Fxq 'AutoSuspendAction=0' "$HOME/.config/powerdevilrc"
grep -Fxq 'DimDisplayWhenIdle=false' "$HOME/.config/powerdevilrc"
grep -Fxq 'TurnOffDisplayWhenIdle=false' "$HOME/.config/powerdevilrc"
test -f "$HOME/.config/kxkbrc"
grep -Eq '^LayoutList=' "$HOME/.config/kxkbrc"
test -f "$HOME/.config/kdeglobals"
grep -Fxq 'ScaleFactor=1.25' "$HOME/.config/kdeglobals"
grep -Fxq 'ScreenScaleFactors=eDP-1=1.25;DP-1=1.25;HDMI-1=1.25;DP-2=1.25;HDMI-2=1.25;' "$HOME/.config/kdeglobals"
test -f "$HOME/.local/share/opensuse-welcome/launched"
grep -Fq 'firefox --kiosk http://127.0.0.1:8000/' /usr/local/bin/fxroute-desktop-launcher
firefox_policy="$(find /usr/lib64/firefox /usr/lib/firefox -maxdepth 3 -path '*/distribution/policies.json' 2>/dev/null | head -n 1 || true)"
test -n "$firefox_policy"
grep -Fq 'http://127.0.0.1:8000/' "$firefox_policy"
for _ in $(seq 1 90); do
  if systemctl is-active --quiet display-manager.service; then
    session_id=""
    while read -r candidate; do
      [[ -n "$candidate" ]] || continue
      if [[ "$(loginctl show-session "$candidate" -p Type --value)" = x11 ]]; then
        session_id="$candidate"
        break
      fi
    done < <(loginctl list-sessions --no-legend | awk -v user="$account" '$3 == user {print $1}')
    if [[ -n "$session_id" ]]; then
      break
    fi
  fi
  sleep 2
done
systemctl is-active display-manager.service
session_id=""
while read -r candidate; do
  [[ -n "$candidate" ]] || continue
  if [[ "$(loginctl show-session "$candidate" -p Type --value)" = x11 ]]; then
    session_id="$candidate"
    break
  fi
done < <(loginctl list-sessions --no-legend | awk -v user="$account" '$3 == user {print $1}')
test -n "$session_id"
test "$(loginctl show-session "$session_id" -p Type --value)" = x11
test -x /usr/local/bin/fxroute-desktop-launcher
test -f "$HOME/.config/autostart/fxroute.desktop"
grep -Fxq 'Exec=/usr/local/bin/fxroute-desktop-launcher' "$HOME/.config/autostart/fxroute.desktop"
grep -Fxq 'TryExec=firefox' "$HOME/.config/autostart/fxroute.desktop"
for _ in $(seq 1 90); do
  if pgrep -u "$account" -f '(^|/)firefox( |$)' >/dev/null &&
     pgrep -u "$account" -f '.*--kiosk' >/dev/null &&
     pgrep -u "$account" -f '127\.0\.0\.1:8000' >/dev/null; then
    break
  fi
  sleep 2
done
pgrep -u "$account" -f '(^|/)firefox( |$)' >/dev/null
pgrep -u "$account" -f '.*--kiosk' >/dev/null
pgrep -u "$account" -f '127\.0\.0\.1:8000' >/dev/null
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user is-active fxroute.service
pactl list sinks short | awk '{print $2}' | grep -Fx 'fxroute_dsp_sink' >/dev/null
curl --fail --silent http://127.0.0.1:8000/api/status >/dev/null
test -d "$HOME/fxroute/.git"
test "$(git -C "$HOME/fxroute" remote get-url origin)" = 'https://github.com/CobbyCode/fxroute.git'
bash -lc 'cd ~/fxroute && scripts/update_fxroute.sh --check >/tmp/fxroute-update-check.log 2>&1'
grep -Eqi 'already up to date|update available|reconciliation is incomplete' /tmp/fxroute-update-check.log
EOF
}

ssh_live() {
  local ssh_port="$1"
  shift
  SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" \
  SSH_ASKPASS_REQUIRE=force \
  DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$LIVE_PASSWORD" \
  setsid ssh \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=1 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -p "$ssh_port" "$LIVE_USER@127.0.0.1" "$@"
}

sudo_live() {
  local ssh_port="$1"
  shift
  printf '%s\n' "$LIVE_PASSWORD" | ssh_live "$ssh_port" sudo -S -- "$@"
}

wait_for_live() {
  local profile="$1"
  local pid="$2"
  local http_port="$3"
  local ssh_port="$4"
  local started=$SECONDS
  local status_file="$TEST_ROOT/$profile-status.json"

  while (( SECONDS - started < TIMEOUT_SECONDS )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '[iso-test] QEMU exited while waiting for %s live\n' "$profile" >&2
      tail -100 "$TEST_ROOT/$profile-serial.log" >&2 || true
      return 1
    fi
    if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        "http://127.0.0.1:$http_port/api/status" > "$status_file" 2>/dev/null; then
      if python3 - "$status_file" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
live = payload.get("live") is True or (isinstance(payload.get("system"), dict) and payload["system"].get("live") is True)
raise SystemExit(0 if live else 1)
PY
      then
        if ssh_live "$ssh_port" true >/dev/null 2>&1; then
          printf '[iso-test] %s live guest is serving FXRoute with live:true\n' "$profile"
          return 0
        fi
      fi
    fi
    sleep 5
  done
  printf '[iso-test] timeout waiting for %s live\n' "$profile" >&2
  tail -160 "$TEST_ROOT/$profile-serial.log" >&2 || true
  return 1
}

verify_live() {
  local ssh_port="$1"
  ssh_live "$ssh_port" bash -s -- "$LIVE_USER" <<'EOF'
set -Eeuo pipefail
account="$1"
test "$(id -un)" = "$account"
test -f /etc/fxroute-live
test "$(cat /etc/hostname)" = fxroute-live
# No installer first-boot in live mode.
! systemctl is-enabled fxroute-first-boot.service >/dev/null 2>&1
! test -f /var/lib/fxroute-iso/install-complete
# RAM overlay backs root and home.
findmnt -n -o SOURCE,FSTYPE,OPTIONS / | grep -Eq 'LiveOS_rootfs|overlay'
findmnt -n -o OPTIONS / | grep -Eq 'rw'
# FXRoute serves HTTP locally with live flag.
curl --fail --silent http://127.0.0.1:8000/api/status | python3 -c "import json,sys; p=json.load(sys.stdin); assert p.get('live') is True or p.get('system',{}).get('live') is True"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user is-active fxroute.service
pactl list sinks short | awk '{print $2}' | grep -Fx 'fxroute_dsp_sink' >/dev/null
# Desktop appliance is present.
test -f /usr/local/bin/fxroute-desktop-launcher
grep -Fq 'firefox --kiosk http://127.0.0.1:8000/' /usr/local/bin/fxroute-desktop-launcher
test -f "$HOME/Desktop/LIVE-MODE-README.txt"
grep -Fq 'Live Mode' "$HOME/Desktop/LIVE-MODE-README.txt"
# Live banner asset is served.
curl --fail --silent http://127.0.0.1:8000/ | grep -Fq 'live-banner'
EOF
}

verify_live_no_internal_mounts() {
  local ssh_port="$1"
  ssh_live "$ssh_port" bash -s <<'EOF'
set -Eeuo pipefail
# Internal ATA/NVMe block devices exist in the test guest (two virtio disks),
# but none of their partitions may be mounted in live mode. Only the live
# medium (iso9660/squashfs/tmpfs/overlay) backs /.
if ls /dev/vd* >/dev/null 2>&1; then
  mounted="$(findmnt -rn -o SOURCE,TARGET,FSTYPE || true)"
  printf '%s\n' "$mounted"
  # No /dev/vd* or /dev/sd* or /dev/nvme* source may appear as a mount source,
  # except the live ISO itself is sr0/loop (iso9660/squashfs), never vd*.
  if printf '%s\n' "$mounted" | grep -Eq '^/dev/(vd|sd|nvme|hd)[a-z0-9]+'; then
    printf '[iso-test] internal disk is mounted in live mode:\n%s\n' "$mounted" >&2
    exit 1
  fi
fi
# Agama must not be running in live mode.
! systemctl is-active --quiet agama.service
! systemctl is-active --quiet fxroute-first-boot.service
EOF
}

verify_live_volatility() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"
  local http_port="$4"
  local probe_path=".cache/fxroute-live-try-probe"
  ssh_live "$ssh_port" "touch ~/$probe_path && test -f ~/$probe_path"
  reboot_live_guest "$profile" "$pid" "$ssh_port"
  wait_for_live "$profile" "$pid" "$http_port" "$ssh_port"
  if ssh_live "$ssh_port" "test -f ~/$probe_path" >/dev/null 2>&1; then
    printf '[iso-test] live home survived reboot (not volatile)\n' >&2
    return 1
  fi
}

reboot_live_guest() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"
  local old_boot_id
  local new_boot_id

  printf '[iso-test] rebooting %s live guest to verify volatility\n' "$profile"
  old_boot_id="$(ssh_live "$ssh_port" cat /proc/sys/kernel/random/boot_id)"
  sudo_live "$ssh_port" systemctl reboot >/dev/null 2>&1 || ssh_live "$ssh_port" sudo reboot >/dev/null 2>&1 || true
  for _ in $(seq 1 90); do
    kill -0 "$pid" 2>/dev/null || {
      printf '[iso-test] QEMU exited while rebooting %s live\n' "$profile" >&2
      return 1
    }
    new_boot_id="$(ssh_live "$ssh_port" cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
    if [[ -n "$new_boot_id" && "$new_boot_id" != "$old_boot_id" ]]; then
      return 0
    fi
    sleep 2
  done
  printf '[iso-test] timeout waiting for %s live reboot\n' "$profile" >&2
  return 1
}

for profile in "${PROFILES[@]}"; do
  http_port="$(port_for_profile "$profile")"
  ssh_port="$(ssh_port_for_profile "$profile")"
  while [[ "$ssh_port" == "$http_port" ]]; do
    ssh_port="$(ssh_port_for_profile "$profile")"
  done
  agama_https_port="$(agama_https_port_for_profile "$profile")"
  while [[ "$agama_https_port" == "$http_port" || "$agama_https_port" == "$ssh_port" ]]; do
    agama_https_port="$(agama_https_port_for_profile "$profile")"
  done
  agama_http_port="$(agama_http_port_for_profile "$profile")"
  while [[ "$agama_http_port" == "$http_port" || "$agama_http_port" == "$ssh_port" || "$agama_http_port" == "$agama_https_port" ]]; do
    agama_http_port="$(agama_http_port_for_profile "$profile")"
  done
  disk="$TEST_ROOT/$profile.qcow2"
  extra_disk="$TEST_ROOT/$profile-extra.qcow2"
  serial_log="$TEST_ROOT/$profile-serial.log"
  qemu_log="$TEST_ROOT/$profile-qemu.log"
  pid_file="$TEST_ROOT/$profile.pid"
  monitor="$TEST_ROOT/$profile-monitor.sock"

  printf '[iso-test] creating fresh %s disks\n' "$profile"
  qemu-img create -f qcow2 "$disk" "${VM_DISK_GB}G" >/dev/null
  qemu-img create -f qcow2 "$extra_disk" "${VM_EXTRA_DISK_GB}G" >/dev/null
  qemu-system-x86_64 \
    -name "fxroute-iso-$profile" \
    -accel kvm \
    -cpu host \
    -smp "$VM_CPUS" \
    -m "${VM_RAM}M" \
    -drive "file=$disk,format=qcow2,if=virtio" \
    -drive "file=$extra_disk,format=qcow2,if=virtio" \
    -cdrom "$ISO_PATH" \
    -boot once=d,menu=off \
    -nic "user,model=virtio-net-pci,hostfwd=tcp::$http_port-:8000,hostfwd=tcp::$ssh_port-:22,hostfwd=tcp::$agama_https_port-:443,hostfwd=tcp::$agama_http_port-:80" \
    -monitor "unix:$monitor,server=on,wait=off" \
    -device virtio-rng-pci \
    -audiodev driver=none,id=fxroute-audio \
    -device ich9-intel-hda \
    -device hda-duplex,audiodev=fxroute-audio \
    -vga virtio \
    -display none \
    -serial "file:$serial_log" \
    -D "$qemu_log" \
    -pidfile "$pid_file" \
    -daemonize
  pid="$(<"$pid_file")"
  PIDS+=("$pid")
  if [[ "$profile" == "try" ]]; then
    # Try boot needs no Agama interaction; pass the live SSH password via
    # kernel cmdline (release boots omit it and keep sshd disabled).
    FXROUTE_ISO_KERNEL_EXTRA="fxroute.live-password=$LIVE_PASSWORD" \
      select_boot_entry "$profile" "$monitor"
    wait_for_live "$profile" "$pid" "$http_port" "$ssh_port"
    verify_live "$ssh_port"
    verify_live_no_internal_mounts "$ssh_port"
    verify_live_volatility "$profile" "$pid" "$ssh_port" "$http_port"
    printf '[iso-test] %s profile passed\n' "$profile"
    # Live guest uses the live password for sudo; poweroff via live SSH.
    printf '%s\n' "$LIVE_PASSWORD" | ssh_live "$ssh_port" sudo -S -- systemctl poweroff >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill "$pid" 2>/dev/null || true
    continue
  fi
  select_boot_entry "$profile" "$monitor"

  setup_agama_interactive "$profile" "$agama_https_port" "$ssh_port" "$pid"
  wait_for_guest "$profile" "$pid" "$http_port" "$ssh_port"
  verify_ssh_defaults "$ssh_port"
  verify_account_region_network_storage "$ssh_port"
  case "$profile" in
    headless) verify_headless "$ssh_port" ;;
    desktop)
      reboot_guest "$profile" "$pid" "$ssh_port"
      wait_for_guest "$profile" "$pid" "$http_port" "$ssh_port"
      verify_desktop "$ssh_port"
      ;;
  esac
  printf '[iso-test] %s profile passed\n' "$profile"
  stop_guest "$profile" "$pid" "$ssh_port"
done
