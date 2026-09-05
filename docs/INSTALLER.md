# FXRoute Installer

`install.sh` prepares FXRoute for a supported Linux user session. It installs
the native DSP dependencies, creates the Python virtual environment, and
enables the `fxroute.service` systemd user unit. Optional streaming providers
are selected independently and are not installed by default in a
non-interactive run.

## Headless User Session

FXRoute and its PipeWire graph must run as the same Unix user. Run the
installer as that user when possible. If installation is started from a root
shell, select the audio user explicitly when more than one normal account is
present:

```bash
./install.sh --user khadas
```

The installer enables `loginctl` lingering before starting PipeWire,
WirePlumber, PipeWire-Pulse, and FXRoute. On a headless system it also starts
the target user's session manager and session bus (`dbus.service`) explicitly
so the user session appears without a login, and adds the target user to the
`audio` group when ALSA hardware is present. It then validates the target
user's session bus and runtime sockets and runs `wpctl`, `pw-cli`, `pw-link`,
and `pactl` in that same user context. A failed PipeWire session check is an
installation failure, not an HTTP-only warning. The installer includes RTKit
so PipeWire can request realtime scheduling through its normal helper when the
native DSP child starts; the native engine leaves realtime thread policy to
PipeWire.

Custom `--target` paths must be dedicated directories whose final component is
`fxroute` (a `--local-project` checkout may keep its own directory name); this
prevents the root installer from changing ownership of a shared system
directory. To remove a root-installed target-user installation, select the
same user explicitly: `./uninstall.sh --user <name>`.

## Provider Selection

Use one or more explicit provider flags:

```bash
./install.sh --spotify-desktop
./install.sh --spotifyd --qobuz
./install.sh --providers spotify-desktop,spotifyd,qobuz,tidal
./install.sh --providers none
```

`--providers` accepts `spotify-desktop`, `spotifyd`, `qobuz`, `tidal`, and
`none`. Component flags can be combined with `--providers`. The choices are
independent, so Spotify Desktop and spotifyd can be installed together.

With no explicit provider selection, an interactive terminal offers each
supported choice. A non-interactive run selects none. Spotify Desktop is not
offered when the host is not x86_64 or when there is no X11/Wayland desktop
session.

## Support Matrix

| Provider | Release or package | Supported host | Installer result |
| --- | --- | --- | --- |
| Spotify Desktop | Native `spotify-client` on apt; `com.spotify.Client` Flatpak otherwise | x86_64 with X11 or Wayland desktop session | Installs the official client, keyring/Secret-Service support, and optional autostart integration |
| spotifyd | v0.4.2 full/MPRIS release; FXRoute ARM64 prebuilt v1 for aarch64 | x86_64 with a compatible runtime; aarch64 with the FXRoute artifact; armv7 where the release runtime is available | Installs `~/.local/bin/spotifyd`, a user service, and a minimal MPRIS/PipeWire-Pulse config with fixed Zeroconf TCP port 4444 |
| Qobuz/qbzd | v2.0.2 standalone build | amd64, aarch64 | Installs `~/.local/bin/qbzd`, Avahi/mDNS support, the ALSA→PipeWire bridge (`pipewire-alsa`, from trixie-backports where that stack is active), `qconnect.volume_mode=locked`, Qobuz Connect auto-connect (`qconnect.startup_mode=on`, device name untouched), audio output routing (`audio.backend=pipewire`, `audio.device=fxroute_dsp_sink`, `audio.skip_sink_switch=true`), and a `qbzd run` user service |
| TIDAL | `tidalapi==0.8.11` in the FXRoute venv | Any supported FXRoute Python host | Adds the optional dependency used by FXRoute's existing PKCE login flow |

The base installer supports apt, dnf, zypper, and pacman. Unsupported
architectures are reported without downloading or building replacement
provider binaries.

The v0.4.2 aarch64 release binary is not compatible with Debian 13/Trixie: it
requires the obsolete `libssl.so.1.1` and `libcrypto.so.1.1`, which Debian 13
does not provide. For aarch64 the installer downloads the versioned FXRoute
artifact from the `spotifyd-arm64-v1` release and verifies its pinned
SHA-256 before extracting it. The artifact uses the same full/MPRIS provider
features as the upstream release and is built against OpenSSL 3 and glibc
2.35; the build provenance and runtime dependencies are documented in
[`docs/SPOTIFYD-ARM64-BUILD.md`](SPOTIFYD-ARM64-BUILD.md). The installed binary
is checked with `ldd`, recorded like any FXRoute-owned binary (path + sha256 in
install state), and the service is enabled normally. If the artifact download,
checksum, or runtime check fails, spotifyd is recorded as unavailable and an
FXRoute-owned service is disabled, exactly like the release-binary failure
path. The installer never installs end-of-life OpenSSL 1.1 packages and never
builds spotifyd on the target host.

## First Run

### Spotify Desktop

Launch Spotify Desktop, sign in, and enable any required account features in
Spotify itself. FXRoute controls the local client through MPRIS/playerctl; it
does not receive or store Spotify credentials.

### spotifyd

The installer creates `~/.config/spotifyd/spotifyd.conf` only when that file
does not already exist. The generated config names the device with a short
unique Spotify Connect name derived from the system hostname (read-only):
a deliberate name such as `fxroute-wohnzimmer` becomes `FXRoute Wohnzimmer`,
an image auto name such as `fxroute-1af688` becomes `FXRoute 1AF6`, and a
bare `fxroute`/`localhost` falls back to a stable machine-id suffix
(`FXRoute XXXX`). The name never contains `.local` and never changes the
hostname, Avahi, Caddy, or DNS setup. On reruns, an existing
FXRoute-managed name (`FXRoute` or `FXRoute …`) is aligned with the current
hostname while a user-customized name is preserved. The config uses
the PipeWire-Pulse backend, enables MPRIS, and uses the session D-Bus.
The generated config and FXRoute-owned user service both pin the Spotify
Zeroconf TCP port to `4444`; the installer opens TCP 4444 and UDP 5353 only
when it creates/configures the spotifyd service and the corresponding UFW or
firewalld backend is active. Existing firewall rules are preserved.

If spotifyd requests authentication, run the user-driven flow:

```bash
systemctl --user stop spotifyd
"$HOME/.local/bin/spotifyd" authenticate --config-path "$HOME/.config/spotifyd/spotifyd.conf"
systemctl --user start spotifyd
```

The installer never writes account credentials, tokens, sessions, or playback
volume levels. It writes `volume_controller = "none"` intentionally so
spotifyd stays at unity and the FXRoute master remains the volume control. The
fixed Zeroconf port is transport configuration, not a playback volume setting.

### Qobuz/qbzd

Complete the browser-based setup once:

```bash
"$HOME/.local/bin/qbzd" setup
```

Complete the OAuth login, enable Qobuz Connect in qbzd, and select the FXRoute
device in the Qobuz app. FXRoute uses qbzd's local control plane at
`127.0.0.1:8182`. The installer does not write Qobuz credentials, OAuth data,
or playback credentials. It does set `qconnect.volume_mode=locked` through the
qbzd settings command so qbzd remains at unity and the existing FXRoute phone
volume bridge controls the FXRoute master. It also runs the native
`qbzd qconnect enable` step so the Qobuz Connect renderer auto-connects at
daemon start; an already enabled renderer is left untouched and the qbzd
device name is never replaced. Other qbzd settings are not
replaced. If the installer changed a prior volume mode, uninstall offers to
restore that recorded mode before removing the owned qbzd binary/service.
A Qobuz Connect enablement owned by the installer is restored the same way
(disabled again) when its ownership record is complete.

The nftables mDNS guard is retained only for the Spotify Desktop-only case.
It is not installed when spotifyd is selected or already present, because
`meta skuid` cannot distinguish spotifyd from another process of the same
user. This keeps Qobuz Connect and spotifyd Zeroconf discovery compatible.

### TIDAL

Selecting TIDAL installs `tidalapi` into the FXRoute virtual environment. Use
FXRoute's existing PKCE login flow from the application. The installer does
not invoke login or create credentials. The TIDAL session file at
`~/.config/fxroute/tidal-session.json` is preserved during uninstall.

## Reruns and Uninstall

Installer reruns preserve existing provider binaries, services, and config
files. FXRoute records ownership and managed paths, including each concrete
UFW/firewalld rule, in
`~/.config/fxroute/install-state.json` so the uninstaller can distinguish its
components from pre-existing installations.

The home-directory install record represents one active FXRoute target. A
second target is rejected until the recorded installation is removed, avoiding
cross-target ownership cleanup. Project-directory removal also refuses the
home/config/cache roots and Git checkouts; if any cleanup is deferred, the
project directory is retained for a later retry.

The default uninstall is cautious:

```bash
./uninstall.sh
```

It prompts before removing FXRoute-owned Spotify Desktop, spotifyd, qbzd, or
TIDAL components. Provider profiles, credentials, OAuth data, session files,
and caches are preserved by default, including:

- Spotify and spotifyd data
- qbzd/Qobuz data and caches
- `~/.config/fxroute/tidal-session.json`

`--remove-project-dir` additionally removes the FXRoute installation tree and
its virtual environment. Use it only when that is intended. Provider data in
the home-directory config/cache paths remains outside that removal.
