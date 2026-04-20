# FXRoute

FXRoute is a browser-based local audio player and DSP control surface for Linux audio PCs.

It brings local playback, radio, library import, EasyEffects preset control, and Spotify desktop control into one responsive web UI for desktop and mobile.

## Highlights

- Local playback with queue support
- SomaFM radio with live metadata
- Library import by URL or file upload
- EasyEffects preset switching and DSP helpers
- Spotify desktop control through MPRIS / `playerctl`
- Responsive web UI with live WebSocket sync

## Supported setup

FXRoute targets Linux desktop systems where audio runs inside the logged-in user session.

Validated so far:
- Ubuntu
- Fedora
- openSUSE Tumbleweed

## What FXRoute needs

- A Linux desktop session with working audio in the logged-in user context
- `systemd --user` support
- `mpv`, `ffmpeg`, Python 3, and `playerctl`
- EasyEffects for DSP preset features
- A browser on the same local network

## Scope and non-goals

FXRoute is intentionally aimed at:
- one personal audio PC on the local network
- browser control from desktop or mobile
- practical playback and DSP control, not a studio workflow

It is currently **not** trying to be:
- a cloud-hosted music server
- a multi-user system
- a headless server-first audio stack
- a DAW or deep DSP design environment

## Quick start

### Recommended install

Run the installer from the project root:

```bash
./install.sh
```

For a non-interactive run:

```bash
./install.sh -y
```

Default public install path:
- `~/fxroute`

Default user service:
- `fxroute.service`

The installer prepares:
- required system packages
- Python virtual environment
- user service
- EasyEffects bootstrap presets
- optional LAN comfort steps such as `.local` naming and port-80 reverse proxy

### First configuration

If you want to create or adjust the config manually:

```bash
cp .env.example .env
```

Minimum required setting:

```env
MUSIC_ROOT=~/Music
```

Useful optional settings:
- `DOWNLOADS_SUBDIR=incoming`
- `AUDIO_FORMAT=mp3`
- `LOG_LEVEL=INFO`
- `HOST=0.0.0.0`
- `PORT=8000`

Downloads are stored under `MUSIC_ROOT/incoming` by default.

## Running and service control

### Manual run

```bash
python3 main.py
```

### User service

FXRoute is designed to run as a **systemd user service** so playback stays tied to the desktop session.

| Action | Command |
|--------|--------|
| Status | `systemctl --user status fxroute` |
| Logs | `journalctl --user -u fxroute -f` |
| Restart | `systemctl --user restart fxroute` |
| Stop | `systemctl --user stop fxroute` |
| Disable | `systemctl --user disable fxroute` |

Minimal example unit file:
- `fxroute.service`

## Access URLs

Typical URLs are:
- `http://localhost:8000`
- `http://<host-ip>:8000`
- `http://fxroute.local` when Avahi/mDNS is enabled
- `http://<host-ip>` when the optional port-80 reverse proxy is enabled

## Current limitations and assumptions

- Spotify control uses the local desktop Spotify app through MPRIS / `playerctl`, not the Spotify Web API
- EasyEffects features expect EasyEffects to exist and run in the user session
- LAN comfort features such as `fxroute.local` and port-80 access depend on optional local network setup
- Import/download behavior depends on `yt-dlp`, and upstream sites can change over time
- The project is currently Linux-focused

## Deployment

Use the deploy script to copy a full project tree consistently:

```bash
./deploy.sh
```

Useful variants:

```bash
./deploy.sh --dry-run
./deploy.sh --restart
./deploy.sh --delete
```

Override target details as needed:

```bash
DEPLOY_HOST=user@host ./deploy.sh --restart
./deploy.sh --host user@host --dir /home/user/fxroute --service fxroute
```

If you keep project-specific defaults inside `deploy.sh`, treat them as local convenience values rather than public documentation.

## Optional watchdog for stale EasyEffects Flatpak sockets

If EasyEffects leaves a stale runtime socket behind, FXRoute ships an optional watchdog timer:

```bash
chmod +x ~/fxroute/scripts/easyeffects-stale-watchdog.sh
mkdir -p ~/.config/systemd/user
cp ~/fxroute/systemd-user/easyeffects-stale-watchdog.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now easyeffects-stale-watchdog.timer
```

The watchdog only intervenes in a narrow stale-socket case and is not required for normal installs.

## Usage

1. Open FXRoute in a browser on your local network.
   - Preferred: `http://fxroute.local`
   - Fallbacks: `http://fxroute.local:8000` or `http://<host-ip>:8000`
2. Use **Radio** for SomaFM streaming.
3. Use **Library** for local playback, playlists, and import.
4. Use **Effects** for EasyEffects preset switching and DSP helpers.
5. Use **Spotify** to control a locally running Spotify desktop client.

### Playback and sync notes
- The footer keeps transport, seek, and volume together for the active source.
- Spotify control uses the local desktop Spotify app through `playerctl` / MPRIS, not the Spotify Web API.
- The app uses WebSocket updates so connected browsers stay in sync.
- Shuffle and loop are intentionally treated as mutually exclusive in the Spotify UX.

## Architecture

```
fxroute/
├── main.py           # FastAPI app with REST + WebSocket endpoints
├── player.py         # MPV wrapper using JSON IPC
├── stations.py       # SomaFM station fetcher with cache
├── library.py        # Local music scanner (mutagen for metadata)
├── downloader.py     # yt-dlp integration with progress tracking
├── config.py         # Configuration from .env
├── models.py         # Data models
├── requirements.txt  # Python dependencies
├── .env.example      # Example config
├── fxroute.service   # example systemd unit file
├── README.md         # This file
└── static/
    ├── index.html    # Single-page app
    ├── style.css     # Dark theme, responsive
    └── app.js        # Vanilla JS, WebSocket client
```

### Data Flow
- Frontend connects via WebSocket for real-time state
- REST endpoints for explicit actions (play, pause, import, effects)
- MPV runs as a subprocess with JSON IPC at `/tmp/mpv.sock`
- Station data cached in SQLite at `/tmp/fxroute-cache/stations.db`
- Downloads go to `MUSIC_ROOT/incoming/` (single queue, V1)

## Troubleshooting

### "mpv is not installed"
Install mpv: `sudo apt install mpv`

### "MUSIC_ROOT is not set"
Make sure your `.env` file exists and contains a valid `MUSIC_ROOT`, for example `MUSIC_ROOT=~/Music`

### WebSocket connection fails
- Preferred LAN setup: Avahi/mDNS for `fxroute.local` plus Caddy on port 80
- Fallback direct app port: `http://<host-ip>:8000`
- Check firewall: `sudo ufw allow 8000`
- Verify the backend is running: `curl http://localhost:8000/api/status`
- Verify the reverse proxy is running: `curl http://localhost/api/status`

### Downloads fail
- Ensure yt-dlp is installed: `yt-dlp --version`
- YouTube changes frequently; if downloads consistently fail, update yt-dlp: `pip install -U yt-dlp`
- Some YouTube videos have restrictions; try a different URL

### Effects do not apply
- Ensure EasyEffects is installed and running in the desktop session
- Refresh the Effects tab after creating or deleting presets
- If preset loading fails, verify the target output chain is still EasyEffects -> DAC and not temporarily bypassed

### Spotify tab is empty or controls do not work
- Ensure Spotify is installed and currently running in the desktop session
- Ensure `playerctl` is installed: `playerctl --version`
- Check whether Spotify is visible to MPRIS/playerctl: `playerctl --list-all | grep spotify`
- If nothing shows up, start Spotify locally on the host and try again
- If metadata/control is flaky, test directly:
  - `playerctl --player=spotify metadata`
  - `playerctl --player=spotify status`
  - `playerctl --player=spotify volume`
- If Spotify transport works but the footer looks stale, hard-refresh once to ensure the newest frontend assets are loaded

### Stations not loading
- The app falls back to a minimal station list if SomaFM API is unreachable
- Check network connectivity
- Stations may be temporarily unavailable

### No sound
- Check that mpv can output audio: `mpv --no-video https://ice1.somafm.com/groovesalad`
- Ensure volume is not muted in the app or system

## Contributing

FXRoute is still in an early, tightly curated stage.

Please discuss larger changes before opening a pull request.
Contribution terms are documented in:
- `CONTRIBUTING.md`
- `CONTRIBUTOR-LICENSE-GRANT.md`

## License

GNU AGPLv3
