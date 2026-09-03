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
FXROUTE_USER="fxroute"
staging_dir=""
chrome_key_file=""
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
  if [[ -n "$chrome_key_file" ]]; then
    rm -f -- "$chrome_key_file"
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

install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-fxroute-iso.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-iso.conf
install -d -m 755 /run/sshd
ssh-keygen -A
sshd -t
systemctl enable --now sshd.service
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
  printf '%s\n' "Agama did not create the FXRoute user home" >&2
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
  local chrome_repo="https://dl.google.com/linux/chrome/rpm/stable/x86_64"
  local chrome_key_url="https://dl.google.com/linux/linux_signing_key.pub"
  local chrome_key_sha256="54dea5f6c2a26091578cf52a999cebc6b64df478d37ad4dce96376b711e3b27c"
  local chrome_repo_file="/etc/zypp/repos.d/google-chrome.repo"
  local sddm_config="/etc/sddm.conf.d/10-fxroute-autologin.conf"
  local displaymanager_config="/etc/sysconfig/displaymanager"
  local launcher="/usr/local/bin/fxroute-desktop-launcher"
  local autostart_dir="$fxroute_home/.config/autostart"
  local autostart_file="$autostart_dir/fxroute.desktop"
  local fxroute_group=""

  chrome_key_file="$(mktemp /run/fxroute-google-linux-signing-key.XXXXXX)"
  curl --fail --location --retry 3 --connect-timeout 10 --max-time 120 \
    --output "$chrome_key_file" "$chrome_key_url"
  if ! printf '%s  %s\n' "$chrome_key_sha256" "$chrome_key_file" | sha256sum --check --status; then
    printf '%s\n' "Google Chrome signing key checksum mismatch" >&2
    exit 1
  fi
  rpm --import "$chrome_key_file"
  rm -f -- "$chrome_key_file"
  chrome_key_file=""

  if ! zypper --no-refresh lr -u | grep -Fq "$chrome_repo"; then
    zypper --non-interactive ar --refresh "$chrome_repo" google-chrome
  fi
  [[ -f "$chrome_repo_file" ]] || {
    printf '%s\n' "Google Chrome repository file was not created" >&2
    exit 1
  }
  if grep -q '^gpgkey=' "$chrome_repo_file"; then
    sed -i "s|^gpgkey=.*|gpgkey=$chrome_key_url|" "$chrome_repo_file"
  else
    printf 'gpgkey=%s\n' "$chrome_key_url" >> "$chrome_repo_file"
  fi
  zypper --non-interactive refresh google-chrome
  zypper --non-interactive install --no-recommends google-chrome-stable

  install -d -m 755 /etc/sddm.conf.d
  cat > "$sddm_config" <<EOF
[Autologin]
User=$FXROUTE_USER
Session=plasmawayland
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

  cat > "$launcher" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
until curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
    http://127.0.0.1:8000/api/status >/dev/null; do
  sleep 1
done
exec google-chrome-stable --new-window http://127.0.0.1:8000
EOF
  chmod 755 "$launcher"

  fxroute_group="$(id -gn "$FXROUTE_USER")"
  install -d -o "$FXROUTE_USER" -g "$fxroute_group" -m 700 "$autostart_dir"
  cat > "$autostart_file" <<EOF
[Desktop Entry]
Type=Application
Name=FXRoute
Comment=Open the FXRoute control surface
Exec=$launcher
TryExec=google-chrome-stable
OnlyShowIn=KDE;
X-GNOME-Autostart-enabled=true
EOF
  chown "$FXROUTE_USER:$fxroute_group" "$autostart_file"
  chmod 644 "$autostart_file"

  systemctl set-default graphical.target
  systemctl enable --force sddm.service
  systemctl start --no-block sddm.service
}

if [[ "$profile" == "desktop" ]]; then
  install_desktop_stack
else
  systemctl set-default multi-user.target
fi

completed=1
