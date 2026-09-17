#!/usr/bin/env bash
# FXRoute first-boot setup on Ubuntu 26.04 (installed system only).
# Installed by Subiquity late-commands, runs once as root via
# fxroute-first-boot.service. Mirrors the Leap first-boot flow, Ubuntu parts:
# apt instead of rpm/zypper, GDM3/GNOME instead of SDDM/Plasma, stock Firefox
# snap instead of the Leap Firefox RPM.
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

SOURCE_ARCHIVE="/opt/fxroute-iso/source.tar"
SOURCE_DIR="/opt/fxroute-iso/source"
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

# The account created interactively in the Ubuntu installer. Prefer the
# documented appliance account, else the single regular user.
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
  printf 'Create exactly one regular user in the installer (for example fxroute).\n' >&2
  exit 1
}

discover_fxroute_user

# Appliance SSH default: password login for the installer-created account,
# root login stays disabled. The image never switches to key-only SSH.
install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-fxroute-iso.conf <<'EOF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin no
PubkeyAuthentication yes
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-iso.conf
sshd -t
systemctl enable --now ssh.service
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
  printf 'The installer did not create the home directory for user %s\n' "$FXROUTE_USER" >&2
  exit 1
}

derive_fxroute_device_name() {
  local machine_id=""
  machine_id="$(cat /etc/machine-id 2>/dev/null || true)"
  machine_id="${machine_id//[!a-z0-9]/}"
  if [[ ${#machine_id} -ge 6 ]]; then
    printf 'fxroute-%s\n' "${machine_id:0:6}"
  else
    printf 'fxroute\n'
  fi
}

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

# Git-backed updates, same pattern as the Leap path (best effort).
enable_git_updates() {
  local target="$fxroute_home/fxroute"
  local remote_url="https://github.com/CobbyCode/fxroute.git"
  local build_commit="" attempt=""

  command -v git >/dev/null 2>&1 || return 0
  if runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  if [[ -f /opt/fxroute-iso/build-commit ]]; then
    build_commit="$(tr -d '[:space:]' < /opt/fxroute-iso/build-commit)"
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
    runuser -u "$FXROUTE_USER" -- env HOME="$fxroute_home" \
      git -C "$target" checkout -q -f -B main "$build_commit"
  else
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
  local gdm_config="/etc/gdm3/custom.conf"
  local launcher="/usr/local/bin/fxroute-desktop-launcher"
  local autostart_dir="$fxroute_home/.config/autostart"
  local autostart_file="$autostart_dir/fxroute.desktop"
  local fxroute_group=""
  local wallpaper_src="$SOURCE_DIR/assets/fxroute-wallpaper.png"
  local wallpaper_dst="/usr/share/backgrounds/fxroute-wallpaper.png"
  local pixmaps_dir="/usr/share/pixmaps"
  local firefox_policy_dir="/etc/firefox/policies"
  local dconf_dir="/etc/dconf/db/local.d"

  fxroute_group="$(id -gn "$FXROUTE_USER")"

  # Stock Ubuntu browser path: the Firefox snap. No PPA, no deb re-packaging.
  command -v firefox >/dev/null 2>&1 || {
    printf '%s\n' "firefox is missing from the installed desktop" >&2
    exit 1
  }

  # GDM autologin for the appliance account.
  if [[ -f "$gdm_config" ]]; then
    if grep -q '^AutomaticLogin=' "$gdm_config"; then
      sed -i "s|^AutomaticLogin=.*|AutomaticLogin=$FXROUTE_USER|" "$gdm_config"
    else
      sed -i "s|^\[daemon\]|[daemon]\nAutomaticLogin=$FXROUTE_USER|" "$gdm_config"
    fi
    if grep -q '^AutomaticLoginEnable' "$gdm_config"; then
      sed -i "s|^AutomaticLoginEnable.*|AutomaticLoginEnable=True|" "$gdm_config"
    else
      sed -i "s|^\[daemon\]|[daemon]\nAutomaticLoginEnable=True|" "$gdm_config"
    fi
  fi

  # The appliance never suspends or locks on its own: GNOME dconf overrides.
  install -d -m 755 "$dconf_dir"
  cat > "$dconf_dir/10-fxroute-appliance" <<'EOF'
[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false

[org/gnome/desktop/notifications]
show-banners=false
EOF
  chmod 644 "$dconf_dir/10-fxroute-appliance"
  dconf update || true

  # systemd-logind: lid/idle triggers ignored, power button powers off.
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

  if [[ -f "$wallpaper_src" && ! -L "$wallpaper_src" ]]; then
    install -d -m 755 /usr/share/backgrounds
    cp -- "$wallpaper_src" "$wallpaper_dst"
    chmod 644 "$wallpaper_dst"
    cat > "$dconf_dir/11-fxroute-wallpaper" <<EOF
[org/gnome/desktop/background]
picture-uri='file://$wallpaper_dst'
picture-uri-dark='file://$wallpaper_dst'
EOF
    chmod 644 "$dconf_dir/11-fxroute-wallpaper"
    dconf update || true
  fi

  # Firefox is the default browser and always opens the FXRoute control
  # surface first (homepage start target). The snap reads /etc/firefox.
  install -d -m 755 "$firefox_policy_dir"
  cat > "$firefox_policy_dir/policies.json" <<'EOF'
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
  chmod 644 "$firefox_policy_dir/policies.json"

  # Kiosk launcher: waits for the backend (max 180 s wall-clock), then opens
  # Firefox fullscreen. Never leaves a bare desktop without explanation.
  cat > "$launcher" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
status_url="http://127.0.0.1:8000/api/status"
backend_ready=0
deadline=$(( SECONDS + 180 ))
while (( SECONDS < deadline )); do
  remaining=$(( deadline - SECONDS ))
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
  mkdir -p "$HOME/Desktop" 2>/dev/null || true
  printf '%s\n' \
    'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
    'Diagnosis: journalctl --user -u fxroute -n 50' \
    > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
fi
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
OnlyShowIn=GNOME;
X-GNOME-Autostart-enabled=true
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$autostart_file"
  chmod 644 "$autostart_file"

  # Desktop links (localized Desktop dir honored).
  local desktop_dir="$fxroute_home/Desktop"
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

  systemctl set-default graphical.target
  # Queue the display-manager start: this unit runs Before=gdm.service, a
  # synchronous restart here would deadlock the boot transaction.
  systemctl enable gdm3.service >/dev/null 2>&1 || systemctl enable gdm.service >/dev/null 2>&1 || true
  systemctl restart --no-block gdm3.service 2>/dev/null \
    || systemctl start --no-block gdm3.service 2>/dev/null \
    || systemctl start --no-block gdm.service 2>/dev/null || true
}

install_desktop_stack

completed=1
