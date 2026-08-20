# FXRoute Installer Completion Implementation Plan

> **For agentic workers:** Execute this plan inline in the isolated
> `installer-completion` worktree. Keep the worktree limited to installer,
> packaging/setup, uninstaller, tests, and documentation files.

**Goal:** Extend the existing FXRoute installer so a fresh supported Linux
system can select and prepare Spotify Desktop, spotifyd, Qobuz/qbzd, and TIDAL
without changing playback or provider runtime code.

**Architecture:** Preserve the current monolithic Bash installer, its package
manager detection, install ordering, user-service model, Linger handling,
PipeWire setup, and install-state JSON. Add explicit provider selection and
provider-specific setup functions. Record only installation ownership and
paths for safe uninstall decisions; runtime discovery continues to use the
existing provider code and installed applications themselves.

**Tech Stack:** Bash, systemd user units, apt/dnf/zypper/pacman, Python venv,
Python `tidalapi`, pinned upstream spotifyd/qbzd release binaries, unittest
static/behavior checks.

**Spec:** User request “FXRoute Installer fertigstellen” from 2026-08-20.

## Global Constraints

- Work only in the isolated `installer-completion` branch/worktree.
- Do not edit playback, provider, DSP, volume, or frontend runtime code.
- Reuse the existing package-manager and architecture detection.
- Spotify Desktop and spotifyd remain independent selectable components.
- Do not write Spotify, Qobuz, or TIDAL credentials or session data.
- Do not add playback-volume configuration for spotifyd. For qbzd, establish
  the existing FXRoute bridge contract with `qconnect.volume_mode=locked` and
  record the prior mode for safe restoration.
- Preserve existing files/configuration, migrate only the required qbzd volume
  mode, and record ownership before removing anything.
- Do not commit or push during this task.

---

### Task 1: Add failing installer contract tests

**Files:**
- Create: `scripts/test_installer_streaming.py`
- Test: `install.sh`, `uninstall.sh`, `requirements.txt`,
  `requirements-tidal.txt`, `docs/INSTALLER.md`

**Interfaces:**
- Consumes: shell function source extracted from `install.sh` and
  `uninstall.sh`.
- Produces: executable tests covering selection, architecture/release
  matrices, keyring dependencies, optional TIDAL isolation, systemd user
  services, ownership state, and credential/volume safety.

- [ ] Write tests that assert:
  - all four provider selection names are accepted and independently stored;
  - no optional provider is selected by default in non-interactive mode;
  - Spotify Desktop is gated to `x86_64` plus a desktop session;
  - spotifyd selects v0.4.2 `full` assets for x86_64, aarch64, and armv7;
  - the spotifyd setup contains `device_name = "FXRoute"`, MPRIS, session D-Bus,
    and PipeWire-Pulse, but no username, password, or volume setting;
  - qbzd selects the v2.0.2 amd64/aarch64 assets and documents OAuth first run;
  - TIDAL is installed from `requirements-tidal.txt`, not base
    `requirements.txt`;
  - the generated state contains provider ownership fields;
  - the uninstaller removes only owned provider binaries/services and keeps
    provider config/session/cache paths by default.

- [ ] Run `python3 scripts/test_installer_streaming.py` and confirm it fails
  because the new installer contracts are not present.

### Task 2: Implement provider selection and optional dependency isolation

**Files:**
- Modify: `install.sh`
- Modify: `requirements.txt`
- Create: `requirements-tidal.txt`

**Interfaces:**
- Consumes: existing `PACKAGE_MANAGER`, `SUDO_CMD`, `uname -m`, package helper,
  venv setup, and user-service setup.
- Produces: `SELECT_SPOTIFY_DESKTOP`, `SELECT_SPOTIFYD`, `SELECT_QOBUZ`,
  `SELECT_TIDAL`; explicit `--providers` plus component flags; interactive
  selection only on a TTY; base venv without `tidalapi`; selected TIDAL install.

- [ ] Add CLI parsing for `--providers <comma-list>`,
  `--spotify-desktop`, `--spotifyd`, `--qobuz`, and `--tidal`; reject unknown
  provider names and keep non-interactive default selection empty.
- [ ] Add TIDAL-specific requirement installation after base venv setup and
  report an already importable package without reinstalling the exact pinned
  version.
- [ ] Keep the existing native audio/DSP/SMB/LV2/package-manager flow intact.
- [ ] Re-run the focused tests and the existing installer package-manager tests.

### Task 3: Add Spotify Desktop and spotifyd setup

**Files:**
- Modify: `install.sh`
- Modify: `uninstall.sh`
- Modify: `scripts/test_installer_streaming.py`

**Interfaces:**
- Consumes: selected provider flags, package helpers, `flatpak_app_installed`,
  `enable_user_session_persistence`, and generated FXRoute service PATH.
- Produces: idempotent Spotify Desktop installation; pinned spotifyd v0.4.2
  full binary installation; user config/service; first-run output; ownership
  state for safe uninstall.

- [ ] Add existing-app detection for native Spotify and Flatpak Spotify.
- [ ] Add distro-specific keyring/Secret-Service packages before selected
  Spotify Desktop installation; use the official Spotify apt repository on
  apt systems and Flatpak/Flathub on the other supported managers.
- [ ] Add pinned spotifyd v0.4.2 full release URLs/checksums for x86_64,
  aarch64, and armv7; install to the user bin directory only when absent.
- [ ] Create a minimal new spotifyd config with `FXRoute`, PipeWire-Pulse,
  MPRIS, and the normal user D-Bus; preserve an existing config unchanged and
  never write account credentials or volume settings.
- [ ] Create and enable only an FXRoute-owned `spotifyd.service`; preserve an
  existing unit and binary; use the existing Linger/user-bus infrastructure.
- [ ] Add pairing/OAuth instructions without invoking authentication or
  writing sessions from the installer.
- [ ] Add uninstaller prompts for FXRoute-owned Spotify Desktop/spotifyd
  binaries/services, while retaining config/cache/session data by default.
- [ ] Run focused tests and `bash -n install.sh uninstall.sh`.

### Task 4: Add Qobuz/qbzd setup

**Files:**
- Modify: `install.sh`
- Modify: `uninstall.sh`
- Modify: `scripts/test_installer_streaming.py`

**Interfaces:**
- Consumes: selected Qobuz flag, existing PipeWire/D-Bus/Linger setup,
  `curl`, `tar`, `sha256sum`, and user service helpers.
- Produces: pinned QBZ v2.0.2 qbzd binary/service substrate for amd64 and
  aarch64, required audio/mDNS runtime packages, OAuth first-run guidance,
  and ownership-aware uninstall.

- [ ] Detect existing `qbzd` in system and user paths and never overwrite it.
- [ ] Install only the required Qobuz runtime libraries and mDNS support for
  the selected package manager; reuse the existing Avahi baseline/state.
- [ ] Download and verify the v2.0.2 standalone qbzd tarball for x86_64 or
  aarch64; report armv7/other unsupported architectures without building a
  replacement.
- [ ] Install a user service using `qbzd run`, preserve existing service/config,
  set `qconnect.volume_mode=locked` without replacing other settings, and do
  not add hard-coded credential values.
- [ ] Document `qbzd setup`/OAuth and Qobuz Connect first run, including the
  existing FXRoute/qbzd control-plane expectations.
- [ ] Remove only FXRoute-owned qbzd binary/service on explicit confirmation;
  retain qbzd config, OAuth token, data, and cache.
- [ ] Run focused tests and shell syntax checks.

### Task 5: Record ownership, document the matrix, and verify

**Files:**
- Modify: `install.sh`
- Modify: `uninstall.sh`
- Create: `docs/INSTALLER.md`
- Modify: `README.md`
- Modify: `scripts/test_installer_streaming.py`

**Interfaces:**
- Consumes: provider setup result flags and existing install-state JSON.
- Produces: provider-aware state, summary output, distro/architecture matrix,
  first-run documentation, and regression coverage.

- [ ] Extend `write_install_state` with provider selection, detected presence,
  install method, and ownership fields without changing runtime detection.
- [ ] Make reruns preserve ownership decisions and existing provider files.
- [ ] Add the provider matrix and clean-install commands to installer docs and
  concise README quick-start guidance.
- [ ] Run `git diff --check`, `bash -n`, focused tests, existing installer
  tests, and the full `scripts/run_tests.sh` once.
- [ ] Inspect the final diff to ensure no playback/provider/DSP/volume/frontend
  runtime file changed.
- [ ] Report test-machine limitations and the required final comparison with
  the parallel spotifyd/Qobuz runtime worktree; leave changes uncommitted.
