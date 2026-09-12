#!/usr/bin/env bash
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

SOURCE_ARCHIVE="/opt/fxroute-iso-source.tar"
SOURCE_DIR="/opt/fxroute-iso/source"
PROFILE_FILE="/etc/fxroute-iso-profile"
STATE_DIR="/var/lib/fxroute-iso"
COMPLETE_MARKER="$STATE_DIR/install-complete"
FAILED_MARKER="$STATE_DIR/install-failed"
IN_PROGRESS_MARKER="$STATE_DIR/install-in-progress"
FXROUTE_USER=""
staging_dir=""
retry_attempt=0
completed=0

[[ "$(id -u)" -eq 0 ]] || {
  printf '%s\n' "FXRoute first-boot setup must run as root" >&2
  exit 1
}

mkdir -p "$STATE_DIR"
if [[ -f "$COMPLETE_MARKER" ]]; then
  exit 0
fi
# A failed attempt is retained for diagnostics, but must not block a retry on
# the next boot after a transient network or package-manager failure.
if [[ -f "$FAILED_MARKER" ]]; then
  retry_attempt=1
fi
if [[ -f "$IN_PROGRESS_MARKER" ]]; then
  retry_attempt=1
fi
rm -f -- "$FAILED_MARKER"
touch "$IN_PROGRESS_MARKER"
chmod 600 "$IN_PROGRESS_MARKER"

cleanup() {
  local status=$?
  if [[ -n "$staging_dir" ]]; then
    rm -rf -- "$staging_dir"
  fi
  if [[ "$status" -eq 0 && "$completed" -eq 1 ]]; then
    rm -f -- "$IN_PROGRESS_MARKER" "$FAILED_MARKER"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$COMPLETE_MARKER"
  elif [[ "$status" -ne 0 ]]; then
    printf 'first-boot setup failed with status %s\n' "$status" > "$FAILED_MARKER"
  fi
  exit "$status"
}
trap cleanup EXIT

[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  printf '%s\n' "Missing FXRoute source archive: $SOURCE_ARCHIVE" >&2
  exit 1
}
[[ -f "$PROFILE_FILE" && ! -L "$PROFILE_FILE" ]] || {
  printf '%s\n' "Missing FXRoute profile marker: $PROFILE_FILE" >&2
  exit 1
}

profile="$(<"$PROFILE_FILE")"
case "$profile" in
  headless|desktop) ;;
  *)
    printf 'Unknown FXRoute ISO profile: %s\n' "$profile" >&2
    exit 1
    ;;
esac

# The installer no longer ships credentials: the end-user account is created
# interactively in Agama. Prefer the documented appliance account name and
# otherwise use the single regular user Agama created.
discover_fxroute_user() {
  local candidate=""
  local candidates=()

  if id -u fxroute >/dev/null 2>&1; then
    FXROUTE_USER="fxroute"
    return 0
  fi
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    candidates+=("$candidate")
  done < <(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\// && $7 !~ /(nologin|false)$/ {print $1}')
  if [[ ${#candidates[@]} -eq 1 ]]; then
    FXROUTE_USER="${candidates[0]}"
    return 0
  fi
  printf 'Cannot determine the FXRoute user (found: %s)\n' "${candidates[*]:-none}" >&2
  printf 'Create exactly one regular user in Agama (for example fxroute).\n' >&2
  exit 1
}

discover_fxroute_user
install -d -m 755 /run/sshd
ssh-keygen -A
# Appliance SSH default: the account created in Agama is administrable over
# the LAN with its password, SSH keys may be added later, and root login
# stays disabled. The image never switches to key-only SSH.
install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-fxroute-iso.conf <<'EOF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin no
PubkeyAuthentication yes
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-iso.conf
sshd -t
systemctl enable --now sshd.service
systemctl reload-or-restart sshd.service
systemctl start network-online.target

staging_dir="$(mktemp -d /opt/fxroute-iso-source.XXXXXX)"
tar --extract --file "$SOURCE_ARCHIVE" --directory "$staging_dir" --no-same-owner
[[ -f "$staging_dir/install.sh" ]] || {
  printf '%s\n' "The FXRoute source archive does not contain install.sh" >&2
  exit 1
}
rm -rf -- "$SOURCE_DIR"
mkdir -p "$(dirname "$SOURCE_DIR")"
mv -- "$staging_dir" "$SOURCE_DIR"
staging_dir=""

chmod 755 "$SOURCE_DIR/install.sh"
fxroute_home="$(getent passwd "$FXROUTE_USER" | cut -d: -f6)"
[[ "$fxroute_home" == /* && -d "$fxroute_home" ]] || {
  printf 'Agama did not create the home directory for user %s\n' "$FXROUTE_USER" >&2
  exit 1
}

reset_incomplete_install() {
  local root_state_dir="/var/lib/fxroute/state/$(id -u "$FXROUTE_USER")"
  local root_state_file="$root_state_dir/install-state.json"
  local target="$fxroute_home/fxroute"

  [[ "$retry_attempt" -eq 1 ]] || return 0
  if [[ -s "$root_state_file" ]] && python3 - "$root_state_file" "$target" <<'PY'
import json
import sys
from pathlib import Path

try:
    state = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    raise SystemExit(1)
if state.get("install_root") != sys.argv[2]:
    raise SystemExit(1)
PY
  then
    return 0
  fi

  rm -rf -- "$target"
  rm -f -- "$fxroute_home/.config/fxroute/install-state.json"
  rm -f -- "$fxroute_home/.config/fxroute/install-config.env"
  rm -rf -- "$root_state_dir"
}

derive_fxroute_device_name() {
  # Unique, stable .local name for image installs: fxroute-<6 machine-id
  # chars>. Derived from /etc/machine-id so it survives reboots/re-installs
  # of the same machine while staying unique across a fleet of images.
  local machine_id=""
  machine_id="$(cat /etc/machine-id 2>/dev/null || true)"
  machine_id="${machine_id//[!a-z0-9]/}"
  if [[ ${#machine_id} -ge 6 ]]; then
    printf 'fxroute-%s\n' "${machine_id:0:6}"
  else
    printf 'fxroute\n'
  fi
}

reset_incomplete_install
export HOME="$fxroute_home"

"$SOURCE_DIR/install.sh" \
  --source "$SOURCE_DIR" \
  --target "$fxroute_home/fxroute" \
  --user "$FXROUTE_USER" \
  --providers none \
  --with-lan-name \
  --with-caddy \
  --device-name "$(derive_fxroute_device_name)" \
  --yes

# Re-create a git checkout inside the installed tree so the standard
# git-based updater (scripts/update_fxroute.sh) works on ISO installs.
# Best effort: a transient GitHub outage must not fail the first boot.
enable_git_updates() {
  local target="$fxroute_home/fxroute"
  local remote_url="https://github.com/CobbyCode/fxroute.git"
  local build_commit="" attempt=""

  if ! command -v git >/dev/null 2>&1; then
    printf '%s\n' "git is not installed; git-based FXRoute updates unavailable" >&2
    return 0
  fi
  # Everything runs as the target user: the install tree is fxroute-owned,
  # and git refuses repositories with a different owner (dubious ownership).
  if runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  if [[ -f /opt/fxroute-iso-build-commit ]]; then
    build_commit="$(tr -d '[:space:]' < /opt/fxroute-iso-build-commit)"
    [[ "$build_commit" =~ ^[0-9a-f]{40}$ ]] || build_commit=""
  fi

  runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" init -q -b main
  runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" remote add origin "$remote_url"
  for attempt in 1 2 3; do
    if runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" \
        -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 \
        fetch -q --no-tags origin \
        "+refs/heads/main:refs/remotes/origin/main"; then
      break
    fi
    if [[ "$attempt" -eq 3 ]]; then
      printf '%s\n' "Could not fetch the FXRoute git history; git-based updates stay disabled" >&2
      return 0
    fi
    sleep 5
  done

  if [[ -n "$build_commit" ]] \
      && runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
          git -C "$target" cat-file -e "$build_commit^{commit}" 2>/dev/null; then
    # The built commit is on the remote: make the tree exactly match it.
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      git -C "$target" checkout -q -f -B main "$build_commit"
  else
    # Development/test ISO: the built commit is not on the remote.  Never
    # overwrite the freshly installed tree with an older checkout; record
    # the installed state as a local commit on top of origin/main instead.
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" reset -q --soft origin/main
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" add -A
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      git -C "$target" -c user.name="FXRoute ISO Install" -c user.email="install@fxroute.local" \
      commit -q -m "FXRoute ISO install snapshot"
    build_commit="$(runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" rev-parse HEAD)"
  fi
  runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" config branch.main.remote origin
  runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" git -C "$target" config branch.main.merge refs/heads/main
  printf '%s\n' "Prepared git-based updates from $remote_url at $build_commit"
}
enable_git_updates

install_desktop_stack() {
  local sddm_config="/etc/sddm.conf.d/10-fxroute-autologin.conf"
  local displaymanager_config="/etc/sysconfig/displaymanager"
  local launcher="/usr/local/bin/fxroute-desktop-launcher"
  local session_init="/usr/local/libexec/fxroute-appliance-session-init.sh"
  local autostart_dir="$fxroute_home/.config/autostart"
  local autostart_file="$autostart_dir/fxroute.desktop"
  local config_dir="$fxroute_home/.config"
  local fxroute_group=""
  local wallpaper_src="$SOURCE_DIR/assets/fxroute-wallpaper.png"
  local wallpaper_dst="/usr/share/wallpapers/fxroute-wallpaper.png"
  local pixmaps_dir="/usr/share/pixmaps"
  local desktop_dir="$fxroute_home/Desktop"
  local firefox_policy=""
  local xkb_layout=""
  local xkb_model="pc105"
  local xkb_variant=""

  fxroute_group="$(id -gn "$FXROUTE_USER")"

  # Firefox comes from the Leap repositories as part of the desktop profile.
  # No third-party browser repository or key is added on the installed
  # system; Chrome is deliberately not part of the appliance image.
  if ! rpm -q MozillaFirefox >/dev/null 2>&1; then
    printf '%s\n' "MozillaFirefox is missing from the desktop profile" >&2
    exit 1
  fi

  install -d -m 755 /etc/sddm.conf.d
  # Autologin uses the default Plasma (X11) session: that is the session the
  # Leap desktop image boots into (default.desktop is the vendor default and
  # identical to plasma6.desktop here). A Wayland session name would not
  # match the display-manager-legacy/X11 stack Agama installs.
  cat > "$sddm_config" <<EOF
[Autologin]
User=$FXROUTE_USER
Session=default.desktop
Relogin=false
EOF
  chmod 644 "$sddm_config"

  if [[ -f "$displaymanager_config" ]]; then
    if grep -q '^DISPLAYMANAGER_AUTOLOGIN=' "$displaymanager_config"; then
      sed -i "s|^DISPLAYMANAGER_AUTOLOGIN=.*|DISPLAYMANAGER_AUTOLOGIN=\"$FXROUTE_USER\"|" "$displaymanager_config"
    else
      printf 'DISPLAYMANAGER_AUTOLOGIN="%s"\n' "$FXROUTE_USER" >> "$displaymanager_config"
    fi
  fi

  # The appliance never suspends, hibernates, or locks on its own: logind
  # ignores lid and idle triggers, and the hardware power button always
  # powers off cleanly instead of suspending. Explicit user actions (the
  # FXRoute suspend/shutdown menu) keep working through logind.
  install -d -m 755 /etc/systemd/logind.conf.d
  cat > /etc/systemd/logind.conf.d/10-fxroute-appliance.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
HandlePowerKey=poweroff
EOF
  chmod 644 /etc/systemd/logind.conf.d/10-fxroute-appliance.conf

  install -d -m 755 "$pixmaps_dir"
  cp -- "$SOURCE_DIR/static/favicon.svg" "$pixmaps_dir/fxroute.svg"
  chmod 644 "$pixmaps_dir/fxroute.svg"

  # Wallpaper for the Plasma desktop (applied at the first graphical login).
  if [[ -f "$wallpaper_src" && ! -L "$wallpaper_src" ]]; then
    install -d -m 755 /usr/share/wallpapers
    cp -- "$wallpaper_src" "$wallpaper_dst"
    chmod 644 "$wallpaper_dst"
  fi

  # Visible entry points on the desktop: FXRoute (fixed start target) and
  # the official Spotify download page. Honor an existing localized desktop
  # folder (e.g. Schreibtisch on German systems); the session init ensures
  # the links again at the first graphical login.
  local xdg_entry=""
  if [[ -f "$fxroute_home/.config/user-dirs.dirs" ]]; then
    xdg_entry="$(grep -E '^XDG_DESKTOP_DIR=' "$fxroute_home/.config/user-dirs.dirs" | tail -n 1 | cut -d= -f2- | tr -d '"')"
    case "$xdg_entry" in
      '$HOME'/*) desktop_dir="$fxroute_home/${xdg_entry#'$HOME'/}" ;;
      /*) desktop_dir="$xdg_entry" ;;
    esac
  fi
  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 "$desktop_dir"
  cat > "$desktop_dir/FXRoute.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=FXRoute
Comment=Open the FXRoute control surface
URL=http://127.0.0.1:8000/
Icon=/usr/share/pixmaps/fxroute.svg
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$desktop_dir/FXRoute.desktop"
  chmod 644 "$desktop_dir/FXRoute.desktop"
  cat > "$desktop_dir/Spotify Download.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=Spotify
Comment=Official Spotify download page for Linux
URL=https://www.spotify.com/download/linux/
Icon=internet-web-browser
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$desktop_dir/Spotify Download.desktop"
  chmod 644 "$desktop_dir/Spotify Download.desktop"

  # Firefox is the default browser and always opens the FXRoute control
  # surface first (homepage/new-window start target).
  for candidate in /usr/lib64/firefox/distribution /usr/lib/firefox/distribution; do
    if install -d -m 755 "$candidate" 2>/dev/null; then
      firefox_policy="$candidate/policies.json"
      break
    fi
  done
  if [[ -n "$firefox_policy" ]]; then
    cat > "$firefox_policy" <<'EOF'
{
  "policies": {
    "Homepage": {
      "URL": "http://127.0.0.1:8000/",
      "StartPage": "homepage"
    },
    "DontCheckDefaultBrowser": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true
  }
}
EOF
    chmod 644 "$firefox_policy"
  fi

  # Per-user desktop defaults for the appliance account: the keyboard layout
  # chosen in Agama is adopted by Plasma (kxkbrc), KWallet/keyring onboarding
  # is suppressed, and no automatic screen lock or PowerDevil sleep/dim/screen
  # turn-off timeouts are active.
  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 "$config_dir"

  # Keyboard: read the X11 layout Agama/localectl wrote and apply it to the
  # Plasma Wayland keyboard config.
  if [[ -f /etc/X11/xorg.conf.d/00-keyboard.conf ]]; then
    xkb_layout="$(sed -n 's/^[[:space:]]*Option[[:space:]]*"XkbLayout"[[:space:]]*"\([^"]*\)".*/\1/p' /etc/X11/xorg.conf.d/00-keyboard.conf | head -n 1)"
    xkb_model="$(sed -n 's/^[[:space:]]*Option[[:space:]]*"XkbModel"[[:space:]]*"\([^"]*\)".*/\1/p' /etc/X11/xorg.conf.d/00-keyboard.conf | head -n 1)"
    xkb_variant="$(sed -n 's/^[[:space:]]*Option[[:space:]]*"XkbVariant"[[:space:]]*"\([^"]*\)".*/\1/p' /etc/X11/xorg.conf.d/00-keyboard.conf | head -n 1)"
  fi
  if [[ -z "$xkb_layout" && -f /etc/vconsole.conf ]]; then
    xkb_layout="$(sed -n 's/^KEYMAP=//p' /etc/vconsole.conf | tr -d '"' | head -n 1)"
  fi
  [[ -n "$xkb_layout" ]] || xkb_layout="us"
  [[ -n "$xkb_model" ]] || xkb_model="pc105"
  {
    printf '%s\n' '[Layout]'
    printf 'LayoutList=%s\n' "$xkb_layout"
    [[ -z "$xkb_variant" ]] || printf 'VariantList=%s\n' "$xkb_variant"
    printf 'Model=%s\n' "$xkb_model"
    printf 'Options=\n'
    printf 'ResetOldOptions=true\n'
    printf 'Use=true\n'
  } > "$config_dir/kxkbrc"
  chown "$FXROUTE_USER:$fxroute_group" "$config_dir/kxkbrc"
  chmod 600 "$config_dir/kxkbrc"

  # Display scale default: 125% is the FXRoute desktop default for better
  # readability on typical 13-14" Full-HD laptop panels and larger displays.
  # The global ScaleFactor covers any output; the per-output list pins the
  # common laptop/external connectors so the default survives KScreen
  # rewrites. kwriteconfig6 merges into kdeglobals without touching the
  # user's other keys (whole-file overwrite would clobber them).
  if command -v kwriteconfig6 >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      kwriteconfig6 --file kdeglobals --group KScreen --key ScaleFactor 1.25
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      kwriteconfig6 --file kdeglobals --group KScreen --key ScreenScaleFactors \
      "eDP-1=1.25;DP-1=1.25;HDMI-1=1.25;DP-2=1.25;HDMI-2=1.25;"
    chown "$FXROUTE_USER:$fxroute_group" "$config_dir/kdeglobals"
    chmod 600 "$config_dir/kdeglobals"
  fi

  # KWallet/keyring onboarding off. The wallet is disabled so no create or
  # unlock dialog interrupts the appliance login or later app use; Spotify
  # Desktop keeps its credentials in its own profile and is unaffected.
  cat > "$config_dir/kwalletrc" <<'EOF'
[Wallet]
Enabled=false
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$config_dir/kwalletrc"
  chmod 600 "$config_dir/kwalletrc"

  # No automatic screen locking, also not after resume.
  cat > "$config_dir/kscreenlockerrc" <<'EOF'
[Daemon]
Autolock=false
LockOnResume=false
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$config_dir/kscreenlockerrc"
  chmod 600 "$config_dir/kscreenlockerrc"

  # PowerDevil (Plasma 6): no dimming, no screen turn-off, no automatic
  # suspend for any power state. Settings live in the per-profile [AC]/
  # [Battery]/[LowBattery] subgroups; an AutoSuspendAction value of 0
  # disables auto-suspend and the idle-timeout keys are omitted so no idle
  # timers get armed.
  cat > "$config_dir/powerdevilrc" <<'EOF'
[AC][SuspendAndShutdown]
AutoSuspendAction=0
[AC][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
[Battery][SuspendAndShutdown]
AutoSuspendAction=0
[Battery][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
[LowBattery][SuspendAndShutdown]
AutoSuspendAction=0
[LowBattery][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$config_dir/powerdevilrc"
  chmod 600 "$config_dir/powerdevilrc"

  # "Welcome to openSUSE Leap" / first-run is suppressed: the welcome
  # launcher state marker is pre-set and its vendor autostart entry is
  # shadowed for the appliance user.
  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 "$autostart_dir"
  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 \
    "$fxroute_home/.local/share/opensuse-welcome"
  printf '%s\n' "1" > "$fxroute_home/.local/share/opensuse-welcome/launched"
  chown "$FXROUTE_USER:$fxroute_group" "$fxroute_home/.local/share/opensuse-welcome/launched"
  chmod 600 "$fxroute_home/.local/share/opensuse-welcome/launched"
  cat > "$autostart_dir/org.opensuse.opensuse_welcome_launcher.desktop" <<'EOF'
[Desktop Entry]
Hidden=true
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$autostart_dir/org.opensuse.opensuse_welcome_launcher.desktop"
  chmod 644 "$autostart_dir/org.opensuse.opensuse_welcome_launcher.desktop"

  # One-shot graphical-login helper: applies the FXRoute wallpaper and seeds
  # the Firefox start bookmark (best effort, first login only).
  install -d -m 755 /usr/local/libexec
  cp -- "$SOURCE_DIR/iso/scripts/fxroute-appliance-session-init.sh" "$session_init"
  chmod 755 "$session_init"

  cat > "$launcher" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
# Wait for the FXRoute backend, but never unbounded: a backend that never
# comes up must still produce a visible desktop state instead of leaving the
# user on a seemingly dead desktop with no kiosk and no clue. The budget is
# wall-clock 180s (not 180 potentially 30s-long requests), which covers the
# backend start plus several service restart attempts (RestartSec=10);
# healthy boots leave the loop in seconds.
status_url="http://127.0.0.1:8000/api/status"
backend_ready=0
deadline=$(( SECONDS + 180 ))
while (( SECONDS < deadline )); do
  remaining=$(( deadline - SECONDS ))
  # A request that hangs must not push the wait past the budget: the total
  # request time is capped by what is left of it.
  request_timeout=$(( remaining < 30 ? remaining : 30 ))
  if curl --fail --silent --show-error --connect-timeout 5 \
      --max-time "$request_timeout" "$status_url" >/dev/null; then
    backend_ready=1
    break
  fi
  sleep 1
done
if [[ "$backend_ready" != 1 ]]; then
  logger -t fxroute-desktop-launcher \
    "FXRoute backend not reachable after 180s; showing the error state" || true
  # Visible failure state: a note on the Desktop plus the kiosk window with
  # the browser's own "unable to connect" page instead of a bare desktop.
  mkdir -p "$HOME/Desktop" 2>/dev/null || true
  printf '%s\n' \
    'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
    'Diagnosis: journalctl --user -u fxroute -n 50' \
    > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
  exec firefox --kiosk http://127.0.0.1:8000/
fi
# First graphical login only: wallpaper + Firefox start bookmark.
if [[ ! -f "$HOME/.local/share/fxroute/appliance-ready" ]]; then
  if [[ -x /usr/local/libexec/fxroute-appliance-session-init.sh ]]; then
    /usr/local/libexec/fxroute-appliance-session-init.sh || true
  fi
  install -d -m 700 "$HOME/.local/share/fxroute"
  touch "$HOME/.local/share/fxroute/appliance-ready"
fi
# Fullscreen start of the FXRoute UI. The Plasma shell is not locked down;
# closing the window returns to the normal desktop.
exec firefox --kiosk http://127.0.0.1:8000/
EOF
  chmod 755 "$launcher"

  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 "$autostart_dir"
  cat > "$autostart_file" <<EOF
[Desktop Entry]
Type=Application
Name=FXRoute
Comment=Open the FXRoute control surface
Exec=$launcher
TryExec=firefox
OnlyShowIn=KDE;
X-GNOME-Autostart-enabled=true
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$autostart_file"
  chmod 644 "$autostart_file"

  systemctl set-default graphical.target
  # Agama desktop images run the display manager through
  # display-manager-legacy (display-manager.service is its alias): keep that
  # stack and only fall back to plain sddm.service when neither is enabled.
  # Enabling both would start two competing display managers.
  # The first-boot unit is ordered Before=display-manager.service (resolved
  # to display-manager-legacy.service): a synchronous restart/start here
  # would wait on the display-manager job, which in turn waits for this
  # service, deadlocking the boot until TimeoutStartSec. Queue the start
  # with --no-block so this service exits and the manager starts after it.
  if systemctl is-enabled display-manager-legacy.service >/dev/null 2>&1 \
      || systemctl is-enabled display-manager.service >/dev/null 2>&1; then
    systemctl enable display-manager-legacy.service >/dev/null 2>&1 || true
    systemctl restart --no-block display-manager-legacy.service || \
      systemctl start --no-block display-manager-legacy.service || true
  else
    systemctl enable --force sddm.service
    systemctl start --no-block sddm.service
  fi
}

if [[ "$profile" == "desktop" ]]; then
  install_desktop_stack
else
  systemctl set-default multi-user.target
fi

completed=1
