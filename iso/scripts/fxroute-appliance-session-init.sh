#!/usr/bin/env bash
# FXRoute appliance one-shot session init.
# Runs once at the first graphical login as the FXRoute user (before the
# FXRoute fullscreen window opens). Applies the FXRoute desktop wallpaper
# and seeds the Firefox start bookmark. Every step is best effort: a failure
# here must never break the login or the FXRoute autostart.
set -Eeuo pipefail

state_dir="$HOME/.local/share/fxroute"
wallpaper="/usr/share/wallpapers/fxroute-wallpaper.png"
log_file="$state_dir/appliance-init.log"
profile_dir=""

mkdir -p "$state_dir"
: > "$log_file"

note() {
  printf '%s\n' "$*" >> "$log_file" || true
}

# Wallpaper: Plasma's supported per-desktop image setter, plus a config-file
# fallback. Autostart can race the plasmashell containment on the first
# login, so retry briefly; every attempt is time-boxed because a wedged
# plasmashell scripting interface would otherwise stall the fullscreen
# start for minutes. The config entry is always written as well so the
# wallpaper is correct at the latest after the next shell start.
write_wallpaper_config() {
  if command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file plasma-org.kde.plasma.desktop-appletsrc \
      --group Containments --group 1 --group Wallpaper --group org.kde.image --group General \
      --key Image "file://$wallpaper" >/dev/null 2>&1 || return 1
    return 0
  fi
  python3 - "$wallpaper" <<'PY' || return 1
import configparser
import os
import sys

appletsrc = os.path.join(
    os.environ["HOME"], ".config", "plasma-org.kde.plasma.desktop-appletsrc")
config = configparser.ConfigParser()
config.read(appletsrc)
section = "Containments][1][Wallpaper][org.kde.image][General"
if section not in config:
    config.add_section(section)
config[section]["Image"] = "file://" + sys.argv[1]
with open(appletsrc, "w") as handle:
    config.write(handle)
PY
}

apply_wallpaper() {
  local attempt=""
  if write_wallpaper_config; then
    note "wallpaper config written"
  else
    note "wallpaper config not written"
  fi
  command -v plasma-apply-wallpaperimage >/dev/null 2>&1 || {
    note "plasma-apply-wallpaperimage is unavailable; config applies at next shell start"
    return 0
  }
  for attempt in 1 2 3; do
    if timeout 20 plasma-apply-wallpaperimage "$wallpaper" >/dev/null 2>&1; then
      note "wallpaper applied"
      return 0
    fi
    sleep 2
  done
  note "wallpaper tool did not respond; config applies at next shell start"
}

find_firefox_profile_dir() {
  local profiles_ini="$HOME/.mozilla/firefox/profiles.ini"
  [[ -f "$profiles_ini" ]] || return 1
  python3 - "$profiles_ini" <<'PY'
import configparser
import os
import sys

path = sys.argv[1]
config = configparser.ConfigParser()
config.read(path)
default_section = ""
for section in config.sections():
    if config.getboolean(section, "Default", fallback=False):
        default_section = section
        break
if not default_section and config.sections():
    default_section = config.sections()[0]
if not default_section:
    raise SystemExit(1)
entry = config[default_section]
profile = entry["Path"]
if entry.getboolean("IsRelative", fallback=True):
    profile = os.path.join(os.path.dirname(path), profile)
print(profile)
PY
}

# Seed one Firefox toolbar bookmark for the FXRoute control surface. The
# URL entry is inserted directly into places.sqlite while Firefox is not
# running; the schema is probed so unknown/new columns are left untouched.
seed_firefox_bookmark() {
  local profiles_ini="$HOME/.mozilla/firefox/profiles.ini"
  if [[ ! -f "$profiles_ini" ]]; then
    # Create the default profile once (headless) so a real bookmark can be
    # stored before the first visible Firefox start.
    note "creating the Firefox profile (headless)"
    timeout 90 firefox --headless --no-remote about:blank >/dev/null 2>&1 || true
  fi
  profile_dir="$(find_firefox_profile_dir 2>/dev/null || true)"
  [[ -n "$profile_dir" && -d "$profile_dir" ]] || {
    note "no Firefox profile directory found; bookmark not seeded"
    return 0
  }
  python3 - "$profile_dir" <<'PY'
import hashlib
import os
import random
import sqlite3
import string
import sys
import time

profile_dir = sys.argv[1]
database = os.path.join(profile_dir, "places.sqlite")
if not os.path.exists(database):
    print("bookmark seed: no places.sqlite yet", flush=True)
    raise SystemExit(0)

URL = "http://127.0.0.1:8000/"
GUID_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "_-"


def new_guid():
    return "".join(random.choice(GUID_ALPHABET) for _ in range(12))


def moz_places_hash(url):
    # Firefox stores the first four bytes of the URL MD5 as a little-endian
    # unsigned integer.
    return int.from_bytes(hashlib.md5(url.encode("utf-8")).digest()[:4], "little")


def rev_host(url):
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    return ".".join(reversed(host.split("."))) + "."


def table_columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


connection = sqlite3.connect(database, timeout=10)
connection.execute("PRAGMA busy_timeout=10000")
try:
    existing = connection.execute(
        "SELECT 1 FROM moz_places WHERE url = ? LIMIT 1", (URL,)
    ).fetchone()
    if existing:
        print("bookmark seed: FXRoute bookmark already present", flush=True)
        raise SystemExit(0)
    toolbar = connection.execute(
        "SELECT id FROM moz_bookmarks WHERE guid = 'toolbar_____' LIMIT 1"
    ).fetchone()
    if not toolbar:
        print("bookmark seed: Firefox toolbar folder not found", flush=True)
        raise SystemExit(0)
    place_columns = table_columns(connection, "moz_places")
    bookmark_columns = table_columns(connection, "moz_bookmarks")
    now = int(time.time() * 1000)
    guid = new_guid()

    place_fields = {
        "url": URL,
        "url_hash": moz_places_hash(URL),
        "title": "FXRoute",
        "rev_host": rev_host(URL),
        "guid": guid,
        "hidden": 0,
        "typed": 0,
        "frecency": 0,
        "visit_count": 0,
        "last_visit_date": now,
    }
    if "foreign_count" in place_columns:
        place_fields["foreign_count"] = 1
    place_names = [name for name in place_fields if name in place_columns]
    place_cursor = connection.execute(
        f"INSERT INTO moz_places ({', '.join(place_names)}) "
        f"VALUES ({', '.join('?' for _ in place_names)})",
        [place_fields[name] for name in place_names],
    )
    place_id = place_cursor.lastrowid

    bookmark_fields = {
        "type": 1,
        "fk": place_id,
        "parent": toolbar[0],
        "position": 0,
        "title": "FXRoute",
        "dateAdded": now,
        "lastModified": now,
        "guid": new_guid(),
    }
    bookmark_names = [name for name in bookmark_fields if name in bookmark_columns]
    connection.execute(
        f"INSERT INTO moz_bookmarks ({', '.join(bookmark_names)}) "
        f"VALUES ({', '.join('?' for _ in bookmark_names)})",
        [bookmark_fields[name] for name in bookmark_names],
    )
    connection.commit()
    print("bookmark seed: FXRoute toolbar bookmark inserted", flush=True)
except sqlite3.Error as error:
    print(f"bookmark seed failed: {error}", flush=True)
finally:
    connection.close()
PY
}

# Visible entry points on the desktop. Resolved at login time so localized
# folder names (e.g. Schreibtisch on German systems) are honored.
ensure_desktop_links() {
  local desktop_dir=""
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  [[ -n "$desktop_dir" ]] || desktop_dir="$HOME/Desktop"
  mkdir -p "$desktop_dir" || {
    note "desktop directory not writable: $desktop_dir"
    return 0
  }
  cat > "$desktop_dir/FXRoute.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=FXRoute
Comment=Open the FXRoute control surface
URL=http://127.0.0.1:8000/
Icon=/usr/share/pixmaps/fxroute.svg
EOF
  cat > "$desktop_dir/Spotify Download.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=Spotify
Comment=Official Spotify download page for Linux
URL=https://www.spotify.com/download/linux/
Icon=internet-web-browser
EOF
  note "desktop links ensured in $desktop_dir"
}

note "FXRoute appliance session init start"
apply_wallpaper
ensure_desktop_links
seed_firefox_bookmark | tee -a "$log_file"
note "FXRoute appliance session init done"
