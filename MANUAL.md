# FXRoute Manual

FXRoute turns a small Linux audio PC into a browser-controlled music and DSP system. A phone, tablet, or laptop on the local network controls playback, switches DSP presets, compares filter banks, imports filters, routes outputs, and runs room measurements — the audio machine itself needs no screen.

This is the user manual. The [README](README.md) covers the feature overview, installation, and the web demo; [docs/INSTALLER.md](docs/INSTALLER.md) covers the installer and streaming providers in depth.

## 1. What FXRoute is for

FXRoute assumes one audio machine running a Linux user session with an active PipeWire stack (desktop, or headless with user services enabled).

On that machine, FXRoute provides:

- a web interface for local music, internet radio, Spotify, Qobuz, TIDAL, and Bluetooth or external line input
- a native DSP engine that all playback enters through (`fxroute_dsp_sink`)
- one output-routing model that assigns logical roles to hardware outputs, plus per-way crossover and bass management
- DSP presets per filter bank, A/B compare, filter imports, and correction workflows
- room and speaker measurement that feeds PEQ and FIR/convolver preset creation
- control of the whole setup from browsers on the local network

Spotify, Qobuz, and TIDAL integration is unofficial and builds on community backends and protocols.

FXRoute exposes its web UI on the LAN. Keep it on a trusted network.

## 2. Install and first start

Three supported ways to get FXRoute onto the audio machine:

- **Classic install.** `git clone https://github.com/CobbyCode/fxroute.git && cd fxroute && ./install.sh`. The installer prepares system packages, builds the native DSP engine, creates the Python virtualenv, and enables the `fxroute.service` user service. Run it as the audio user; from a root shell pass `--user <name>` when the host has several users. Supported package managers are apt, dnf, zypper, and pacman.
- **ARM64 Armbian image (Raspberry Pi 4 / Pi 5).** Write the image, boot, and finish the web onboarding; first boot installs FXRoute and enables the `.local` name and HTTPS. See [docs/INSTALL-ARMBIAN.md](docs/INSTALL-ARMBIAN.md).
- **x86_64 installation ISO.** Either the openSUSE Leap 16 ISO (`FXRoute Headless`, `FXRoute Desktop`, `Try FXRoute`) or the Ubuntu 26.04 ISO (`Install FXRoute Headless`, `Install FXRoute Desktop`, `Try FXRoute Live`). Write the ISO, boot, pick a profile, and complete the installer. See [docs/INSTALL-ISO.md](docs/INSTALL-ISO.md) for the Leap path.

Streaming providers are optional and are managed later in **Technical settings → Providers**; the installer flags select the same backends, e.g. `./install.sh --providers spotifyd,qobuz,tidal`. Provider installs and updates always resolve the current stable upstream release — no provider version is pinned.

Open FXRoute from any browser on the same network:

- `http://fxroute.local:8000` (default name; change it in **Technical settings → Device Name**; the downloadable images install a unique id, e.g. `http://fxroute-ab12cd.local:8000`)
- `http://<host-ip>:8000`
- `http://localhost:8000` on the audio PC itself

With the optional local HTTPS proxy enabled, `https://<host-ip>` and `https://<device-name>.local` also work; HTTP on port 8000 stays reachable.

The service is `fxroute.service`. Quick host commands:

```bash
systemctl --user status fxroute
systemctl --user restart fxroute
journalctl --user -u fxroute -f
```

On a volatile `Try FXRoute` live session the interface shows a **Live Mode** banner: changes and logins are not saved and are lost after reboot.

## 3. The interface

The main tabs are **Radio**, **Library**, and **DSP**, plus the provider tabs **Spotify**, **Qobuz**, and **TIDAL** once installed, enabled, and shown. The FXRoute logo in the top-left opens **Technical settings**; the power menu next to it can **Suspend** or **Shut down** the audio PC when the host supports it.

The playback bar at the bottom is always visible:

- play/pause, previous/next, seek, shuffle/repeat where the source supports them, and the current queue
- the **volume slider is the FXRoute master volume**, the main listening level
- the level badge shows the live peak against the DSP chain; radio and library rows show stream tech lines (codec, bitrate, sample rate)
- click the current cover to open the detail card with metadata, "Recently played" where the source provides it, and the queue

Source and app volumes (Spotify client, Qobuz app) stay separate from the FXRoute master volume. The compact source switcher next to the transport controls selects between app playback, an external input, and Bluetooth input.

## 4. Sources

### 4.1 Radio

Play from **My Stations** (your saved streams) or from the **Station Catalog** of curated stations. Radio lets you:

- search Radio Browser by station, genre, or country; low-quality streams are filtered out and results are grouped into My Stations, Station Catalog, and Web Results
- add, edit, and delete personal stations, with custom artwork. SomaFM streams are added directly from the URL; other streams need a name
- export your station list (JSON) and import stations by dropping a JSON array onto the station URL field
- see live metadata and artwork for stations with dedicated providers (Radio Paradise, FIP, SomaFM, KEXP) in the current-track card; only SomaFM also supplies a "Recently played" list

Curated stations appear under **Station Catalog** until you add them to **My Stations**. Search-result and catalog cards add a station to My Stations; only My Stations cards start playback.

### 4.2 Library and network shares

**Library** plays music from the local folder or from a network share, in **Albums**, **Tracks**, **Folders**, or **Favorites** view, with a grid/list toggle for albums. The upload hint lists the accepted formats: *Upload MP3, FLAC, WAV, OGG/Opus/WebM, M4A, M3U/M3U8, or ZIP.* Multi-select per track, or **Select all** for everything currently visible, drives **Download selected** (single file or ZIP) and **Delete selected**.

- **Import** offers *Import from URL* (paste or drop a YouTube or direct media link) and *Upload audio file, playlist, or album ZIP*. Uploads land in the managed `incoming` folder under the music root.
- **Playlists** are created and updated with **Save as playlist…**: saving under an existing name replaces that playlist's tracks; **Delete** removes it. Playlists are shown as cards in Favorites and as rows in Tracks/Folders, and can be exported as M3U8 or imported from `.m3u`/`.m3u8` files (including playlists inside an album ZIP).
- **Favorites** collects favorited albums and your playlists, plus a **Top 40** smart mix of most-played and favorited tracks. Individual tracks and albums are favorited with the heart in their row/card; there is no bulk favorite action and no separate "favorite tracks" list.
- Album detail shows label, genres, an "About this album/artist" summary, and a **Discover similar music** panel with similar artist names.

FXRoute treats local tags and cover files as authoritative and enriches albums opportunistically with cached MusicBrainz release and artist ids, Cover Art Archive covers, artist/album summaries, and similar-artist suggestions. Enrichment runs a few albums per scan with long cooldowns, so scans stay fast; unchanged tracks are cached by path, mtime, and size.

The active library is selected in **Technical settings → Music Library**. FXRoute discovers accessible SMB shares (neighbour table plus a local-subnet sweep, keeping only listable disk shares); a share that is not found can be added manually as `smb://server/share` through *Add network share manually…*. A manually entered share is kept for the running session only — re-enter it after a service restart, or make sure discovery finds it. The host needs the installer-provided CIFS support to mount it.

### 4.3 Spotify

Spotify pairs through Spotify Connect, not a login inside FXRoute: install the backend in **Technical settings → Providers**, open the Spotify app on a phone or computer, and select the FXRoute device. Two backends are supported:

- **Spotify Desktop** — the official client on an x86_64 desktop session, controlled through MPRIS. A current client can also deliver Lossless streams for eligible Premium accounts; FXRoute only provides the remote control, not the stream.
- **spotifyd** — a headless player for sessions without a desktop. The installer pins its Zeroconf port so phones find the FXRoute player reliably. Lossless is not available through spotifyd.

Which backend is active is shown in the tab tooltip. The tab offers play/pause, next/previous, seek, shuffle, repeat, volume, cover art, and track metadata. Metadata follows the local player, so track changes update without a manual refresh. When the backend is idle the tab says **Ready for Spotify Connect.**; when it is not running it explains how to start it. Spotify may trigger a Linux keyring unlock prompt after login (on XFCE, install and set up `seahorse` first).

### 4.4 Qobuz

The Qobuz tab controls a **Qobuz Connect** player (`qbzd`) on the audio PC. Connect the account in **Technical settings → Providers**: press **Connect**, open the shown sign-in link, paste the redirect URL back, and confirm. If `qbzd` is present but never set up, the row offers **Complete setup…** plus **Restart**. Playback starts from the Qobuz app by selecting the FXRoute device; the tab then offers the same transport controls as the other sources. Stream quality follows your Qobuz account and app settings. The Qobuz client runs at unity gain, so volume changes are handled by the FXRoute master slider.

### 4.5 TIDAL

TIDAL is a full catalog browser inside FXRoute. After installing the backend you can browse tracks, albums, artists, and playlists, search, favorite, create or extend playlists, and play natively — TIDAL audio runs through FXRoute's DSP chain and playback bar like local files.

Two login methods exist in the TIDAL tab:

- **Browser login (PKCE)** — copy the login link, sign in on any device, paste the redirect URL back. Only this method unlocks Lossless and Hi-Res playback.
- **Device login** — enter a code at link.tidal.com. Faster, but limited to AAC 320 kbps.

Disconnect in **Technical settings → Providers**; playback stops until you sign in again. Your TIDAL library and favorites stay on your TIDAL account.

### 4.6 Bluetooth and external input

**Technical settings → Source** selects the active input: **App playback** (the default), **External input: …** for a detected stereo line/loopback source, or **Bluetooth input** when the host stack supports it. The Bluetooth status line reports the adapter state, the connected device, and the active codec. Mono-only captures are not offered as external input. Leaving Bluetooth input mode also disconnects Bluetooth audio source devices.

## 5. DSP

Open **DSP** to shape the sound. The **Measure** button on this page opens the measurement assistant (section 6). The **Import** button opens the filter import panel.

FXRoute routes every source through one output-routing model. A **mode** decides which roles exist, **Output Routing** assigns those roles to hardware outputs, and the active assignments define the **topology** — which in turn defines the filter banks, the crossover card, the subwoofer card, and what a measurement sweep excites.

### 5.1 Presets, filter banks and A/B compare

- **Presets** are selectable chains. **Direct** bypasses the whole processing chain including the global helpers; **Neutral** is a clean chain that keeps the helpers active and is the default. Both are built in and cannot be deleted.
- **Filter banks** decide which part of the system a preset (and a measurement) applies to. Pure Stereo (only Main L and Main R routed) keeps the single **Global** bank and shows no bank selector. As soon as the topology has more than Main L/R, a **Bank** selector appears above the preset slots: **Global**, **All Banks**, each complete stereo pair (**Main L/R**, **Low L/R**, **Low-Mid L/R**, **Mid L/R**, **High L/R**, **Sub L/R**) and each routed mono sub (**Sub 1**, **Sub 2**). Preset selection, A/B compare, import, combine, and measurement all act on the selected bank; the per-role bindings behind a bank are kept, so an older installation with different left and right presets survives.
- **A/B compare** switches between two presets in the selected bank while you listen. Preset A and B choose the slots; the header shows which side is active. **Delete active** removes the listening preset.
- **Combine** builds one preset from up to three presets in order.
- **Import filter** loads stereo or separate left/right corrections: FXRoute preset JSON, a preset bundle ZIP, convolver `.irs`, WAV impulse responses, or pasted REW-style filter text (one text per side for the split form).
- **Create PEQ preset** builds paired left/right bands from scratch. Each band has a frequency, gain, Q, and a type (Bell, Low shelf, High shelf, Low pass, High pass, Notch, Gain); the **EQ mode** selects IIR, FIR, FFT, or SPM. The preset is written into the selected filter bank.

### 5.2 Output extras

**Output extras** apply automatically on top of every preset except **Direct**:

- **Protection limiter** — protects the final output from peaks
- **Headroom** — safety margin from −1 to −9 dB (default −3 dB)
- **Autogain** — drives the programme level toward −12, −15, −18, or −23 LUFS
- **Loudness** — calibrated contour that follows the playback level; **Strength** (1–10) and an **FFT** size (256–16384) set the contour. It works at its own DSP level and never moves the master volume; when Autogain is active, the contour accounts for the Autogain target too
- **Bass enhancer** — adjustable low-frequency enhancement (−20…+20 dB)
- **Tone effect** — **Crystalizer** or **Maximizer** modes

Autogain and Loudness can run together; the limiter stays the final stage.

### 5.3 Output mode, crossover switch and output routing

**Technical settings → Audio Output** holds the device, the sample-rate policy, and three routing controls. On a two-output device the **Mode** and **Crossover** rows are hidden and the system is internally fixed to Stereo; three or more outputs expose them.

**Mode** offers exactly two values:

- **Stereo** — mains only (Main L/R, or the speaker ways when Crossover is On)
- **Stereo + Sub** — the same mains plus the sub roles (Sub L, Sub R, Sub 1, Sub 2)

**Crossover** is an independent On/Off switch next to Mode, not a third mode. Turning it On replaces Main with the speaker ways in the same routing (Main L/R become Low L/R); turning it Off maps Low back to Main and switches the higher ways Off. Presets, levels, delays, and polarities of the dormant ways are kept, so you can compare a 2-way and a 3-way setup without rebuilding them.

**Output Routing** is one row per hardware output (`Out 1`, `Out 2`, …), each with a role dropdown. The hint reads: *Assign a role to each hardware output. Off leaves an output silent.* Which roles are offered follows the mode and the crossover switch:

| Mode | Crossover | Roles offered per output |
| --- | --- | --- |
| Stereo | Off | `Off`, `Main L`, `Main R` |
| Stereo + Sub | Off | `Off`, `Main L`, `Main R`, `Sub L`, `Sub R`, `Sub 1`, `Sub 2` |
| Stereo / Stereo + Sub | On | `Off`, the ways `Low L`, `Low-Mid L`, `Mid L`, `High L` and their `R` counterparts, plus the sub roles in Stereo + Sub |

One role may feed several outputs (fan-out); only the roles that actually appear are active. The **Mode** hint reads `<Mode> · <N> hardware outputs`, and the line under the routing grid shows the derived summary, for example `Stereo · Mono sub`, `3-Way · Dual-mono subs` — and, when the routing is incomplete, the first problem:

- *Stereo routing requires Main L and Main R*
- *Crossover requires complete Low/High, Low/Mid/High, or Low/Low-Mid/Mid/High ways*
- *Left and Right crossover ways must match*
- *At most two distinct sub roles are supported*

An incomplete routing is saved but not activated; the interface says so instead of silently ignoring it. Assignments are stored per device and per mode. When a rate change makes a device expose fewer channels, the saved roles beyond the visible outputs are kept and come back with the tier. While a measurement is running the output state is locked.

**The sub configuration is derived, not selected.** The routed sub roles alone decide it, and the Subwoofer card badge names it: one sub role is a **Mono sub**, two distinct roles are **Dual-mono subs**, and the pair Sub L + Sub R is **Stereo subs**. Older FXRoute versions and parts of the interface still call these *2.1*, *2.2* and *2.2 Stereo Bass*; those are labels for the same three derived layouts, not modes you can pick.

### 5.4 Crossover and speaker ways

The **Crossover / Speaker** card on the DSP page appears when Crossover is On and at least one speaker way is routed. The number of ways (2, 3, or 4) is derived from the routing, not chosen: each side must carry Low/High, Low/Mid/High, or Low/Low-Mid/Mid/High, and both sides must match.

- **Way tabs** list `Low`, `Low-Mid`, `Mid`, `High`. With **Link L/R** on they read `L/R · Low` and so on; with the link off they read `Left · Low` and `Right · Low` so each side is edited on its own.
- Each way has three control groups: **High-pass** (Frequency 20–20000 Hz, Type, Slope dB/oct), **Low-pass** (same), and **Trim** (**Level** −80…+24 dB, **Align** −40…+40 ms, **Polarity** `N`/`I`).
- **Type** is `Off`, `Linkwitz-Riley` (12–72 dB/oct in steps of 12), `Butterworth`, or `Bessel` (both 6–72 dB/oct in steps of 6). The card and preview abbreviate these as `LR24`, `BW12`, `BS18`.
- **Type = Off clears that filter**: the way then runs open in that direction, the frequency and slope rows disappear, and the card summary names the open direction (`High-pass off` / `Low-pass off`). The preview draws that band the way it really runs, with a dashed curve. Re-selecting a real type restores a filter at the way's starter frequency.
- The first time a valid way set appears, FXRoute seeds Linkwitz-Riley 24 dB/oct starter values and says so: 2-way at 2000 Hz, 3-way at 300/2500 Hz, 4-way at 200/800/3000 Hz.
- While a subwoofer is routed, the **Low** way's high-pass is owned by the Subwoofer card. Its frequency is shown for reference with a `Set by the Subwoofer tile (…)` hint and cannot be edited here.
- The card header summarizes the system, for example `3-Way Stereo System · Sub HPF 80 Hz · High-pass off`.
- **Link L/R** in this card is a UI convenience: it merges the tabs and copies the crossover filter values to the mirrored side. Way trim (level, align, polarity) always stays per side.

### 5.5 Subwoofers

The **Subwoofer** card appears on the DSP page when at least one sub role is routed. Its header shows the routed configuration (`Crossover 80 Hz · LR24 · Main HPF on`, or the two sides separately for an unlinked stereo pair) and the derived badge (`Mono sub`, `Dual-mono subs`, `Stereo subs`).

- **Global** (or the selected side) group: **Crossover** 40–200 Hz, **Type**, **Slope dB/oct**, and **Main highpass** `On`/`Off`.
- One control group per routed sub role, labelled with the role name (`Sub 1`, `Sub 2`, `Sub L`, `Sub R`), each with **Level** (−24…+12 dB), **Align** (−40…+40 ms), and **Polarity** `N`/`I`.
- **Derived delays** are shown for reference in a two-sub layout: `Main: … ms · Sub 1: … ms · Sub 2: … ms`.
- The preview draws the sub low-pass and the main high-pass with a cutoff marker, and can be dragged to set the crossover frequency directly.
- A **Mono sub**, a **Dual-mono** pair, and a **linked stereo pair** all share one crossover, so their subs cannot drift apart. Only a true Sub L + Sub R pair offers **Link L/R**; with the link off the card shows a `Sub L` / `Sub R` tab pair and one side at a time, each with its own crossover frequency, type, and slope. The **Main highpass** switch is shared in every layout.
- Mono and dual-mono subs are fed by both channels summed at half gain, so they are excited from either side; a true Sub L + Sub R pair is side-fed. A dual-mono or mono sub is also a **mono** filter bank: paired PEQ bands must match, and a convolver impulse response must be mono.
- Subwoofer routing needs a selected multichannel output with at least three channels; subs summed from both sides need a channel count that covers the mode.

### 5.6 Sample rate

**Sample Rate** defaults to **Auto**: the PipeWire graph and DAC follow the effective playback rate of the current source (local files, radio, TIDAL, Spotify, Bluetooth can differ). A fixed rate forces one clock; FXRoute rejects rates the selected output does not support and caps processing at 384 kHz. Switching policy can restart the audio path, so stop playback first and re-check the output afterwards. Use Auto when sources with different native rates play together; use a fixed rate when the DAC, DSP chain, or external hardware needs one clock.

Some USB interfaces offer fewer hardware channels at higher rates. For known profiles (Scarlett 16i16, 18i16, and 18i20 4th Gen), FXRoute offers every supported rate — for example 18 channels at 44.1/48 kHz, 14 at 88.2/96 kHz, 10 at 176.4/192 kHz on the 16i16 — and switches the channel inventory internally to the tier that carries the rate. There is no separate tier control: Fixed and Auto both select a rate, and the rate determines the tier. Switching tiers re-probes the device (audible relay clicks), so keep track changes within one band when that matters. **Auto** follows the source rate and switches tiers with it; it never resamples to stay inside an inventory.

## 6. Measurement assistant

Open **Measure** on the DSP page. The assistant plays sweeps through the active DSP chain, records the response from a host microphone, and turns saved measurements into PEQ or FIR/convolver corrections. A microphone input is selected in **Setup**.

The workflow column holds: **Measurements** (Setup, Start Sweep → Run Single Sweep / Start LR Repeat / Advanced), **Subwoofer** (Auto Sub Optimize, only when subs are routed), **Speaker Auto Alignment** (only for a clean 2-, 3-, or 4-way crossover), and **Calibration** (SPL Calibration). Only one measurement runs at a time; while Auto Sub Optimize or a sweep is running, the competing start controls are refused rather than queued silently.

### 6.1 Setup: microphone, electrical reference, and files

**Setup** holds the capture input and the reference material:

- **Host capture input** — the microphone (or interface) the sweeps are recorded from, plus **Mic input channel**.
- **Electrical reference input** — a second channel on the same capture interface that records the sweep itself (a loopback of the output signal) next to the microphone. On an input with three or more channels FXRoute offers **Electrical Ref L** and **Electrical Ref R** instead of the shared field. A reference that equals the microphone channel is disabled with the note *Electrical reference disabled: mic and reference must use different input channels.*
- **Calibration file (optional)** — a microphone calibration file, uploadable, selectable, exportable, and deletable.
- **House curve / target file (optional)** — a REW-style target curve, with the same upload/export/delete handling.

The electrical reference is what makes the timing analysis trustworthy: the direct-arrival and L/R timing values FXRoute reports come from the electrical signal when one is configured, and are measurably more precise than acoustic-only captures. Where no reference is configured, FXRoute still measures and still works — it simply reports lower-confidence timing, which matters most for the aligned FIR modes and for Speaker Auto Alignment.

Each saved run shows how its timing was obtained: **Electrical reference active**, **Electrical reference fallback** (the configured reference was clipped, too quiet, not sharply detected, or not confident enough, so a host-monitor reference was used), or **Acoustic-only timing**. The saved-run line also reports the measured delay and, for L/R Repeat, the spread across the accepted repeats. A tooltip explains that the L/R delta is calculated as Right minus Left, so a positive value means the right channel arrives later.

### 6.2 Measuring area and single sweeps

The **Start Sweep** menu shows the **Measuring area** before you run anything. The area is the DSP filter bank currently selected on the DSP page:

- **Global** — *Whole system; all area banks active.* The sweep plays every active role.
- a specific bank (for example `Sub 1`, `Low L/R`, `Main L/R`) — *Only <bank> stays audible; every other output is muted for this sweep.* The sweep plays that bank alone, so the result is the response of that way without the rest of the system.

**All Banks** is a switch-all convenience for A/B compare and is not measurable: selecting it disables the sweep with *Select a filter bank for measurement.* and points the import at the same restriction. Pick a real bank to measure or import.

With **Global** or a stereo pair bank you can narrow the sweep to one side with the `Left` / `Stereo` / `Right` chips; a mono bank always sweeps both inputs at once and hides the chips. A mono area also disables **Start LR Repeat** (there is no second side to compare) and explains why. The button becomes **Run Single Sweep**; the status line reports the input level (`Peak … dBFS`, `Peak < -90 dBFS`, or `CLIP`).

### 6.3 LR Repeat

**Start LR Repeat** measures left and right three times each at one fixed microphone position and shows one combined result. Its label becomes **Cancel measurement** while it runs. **Save current** stores the pair as `<name> · L` and `<name> · R`; the internal repeats are never added to the saved list. The saved line reports how many repeats were accepted and how much their timing spread. Use the mode when you want a more dependable L/R pair for correction or the aligned FIR modes.

### 6.4 Advanced: speaker and room measurement

**Advanced** guides a multi-position measurement and is labelled *Combined Speaker and Room Measurement*:

1. Direct response · Left — microphone about 1 m from the left speaker on its listening axis
2. Direct response · Right — same for the right speaker
3. Main listening position — move the microphone to ear height at the main listening position; left and right are measured automatically
4. Left listening position — move the microphone 20–30 cm left of the main listening position
5. Right listening position — move it 20–30 cm right

When subwoofer routing is active, a final **Subwoofer alignment check** step returns the microphone to the main listening position and measures the summed L+R response with the subs in circuit. The wizard label reflects the derived sub layout (`Stereo`, `2.1`, `2.2 Mono`, `2.2 Stereo`).

FXRoute validates position, L/R timing, and the summed response and asks you to repeat a step after correcting it (**Repeat measurement**). Keep the microphone at ear height and move it only when a step asks. Use the electrical reference input when available.

When the run finishes, the dialog lists what was captured, compares left and right, reports the gated direct lower limit, and offers **Open filter generator**. That hands the combined speaker and room model to the **Convolver** assistant as a left/right pair (`Advanced … L` and `· R`) so you can turn it straight into a FIR preset.

### 6.5 Speaker Auto Alignment

**Speaker Auto Alignment** appears only for a clean crossover topology (2, 3, or 4 ways, no routing issues, and the **Global** bank selected) — it aligns the whole speaker, so it refuses to run against a single bank. **Align Left** and **Align Right** run one side at a time with the microphone fixed, and a configured electrical reference is required: the verification take cannot run on the microphone alone.

The workflow measures every way of that speaker in one shared planning take, measures each way on its own for level and provenance evidence, proposes an added delay (and, where the evidence supports it, an added gain trim) per way relative to the latest way, then takes one shared *verification* measurement and only commits when the verification passes. Delay and gain are written into the committed output state for those ways; polarity, crossover, routing, subs, and extras are never touched. The result table lists each way (`Low`, `Low-Mid`, `Mid`, `High`) with its added delay, added gain, and how far its arrival stood above its neighbours in the planning take and in the verification; the line under it reports the timing spread before and after against the verification limit. The status line states the outcome:

- *Speaker Align Left verified and applied.* (with the committed revision)
- *Speaker Align Left not verified: … Previous delays kept.*
- *Speaker Align Left trial verified; nothing applied.*

The planning and verification measurements are named `Speaker Align <Side> · Before (planning)` and `· After (verification)`, land on the graph, and can be saved like any other run. **Cancel Alignment** stops a running job.

Keep the setup fixed while it runs. FXRoute refuses to continue — and says so — if the output state moved after the job started, if another measurement or alignment is already running, if the ways cannot be separated by at least 10 dB in the planning take, if the microphone or reference input changed between takes, or if the microphone gain was raised mid-run.

### 6.6 Auto Sub Optimize

**Auto Sub Optimize** scans subwoofer alignment candidates around the currently configured values, then verifies and applies the winner for the *current derived sub layout*. There is no mode to pick — the routing decides which optimizer runs:

- **Mono sub** — one shared alignment for a single sub, checked against both mains
- **Dual-mono subs** — Sub 1/Sub 2 alignment combinations evaluated as one dual-sub system
- **Stereo subs** — left and right sub/main branches optimized separately

The button becomes **Cancel Auto Sub** while a run is in progress, and the subwoofer controls are locked during it; manual sweeps are refused until it finishes. The progress line names the current stage (level check, main reference, coarse scan, fine scan, per-sub scans, combined matrix, polarity check, level match, deep-bass check, final check). Delay candidates stay within ±40 ms of the current value, gain candidates within ±6 dB, and every winner is verified against the selected target curve and checked against the full-scale limit before it sticks; a candidate that is not clearly better keeps the existing alignment, and the result line says so. The baseline and confirmation measurements are kept as normal saved runs.

If you know a sensible starting delay (for example from a subwoofer manual), set it in the Subwoofer card first — the scan centers on it. Keep the microphone fixed and stay quiet during the run. The result names the derived layout it optimized (`2.1`, `2.2`, `2.2 Stereo Bass`).

**Recommended order with EQ or convolver correction:** set crossover and levels roughly, run Auto Sub Optimize, verify with a normal measurement, create and enable the correction from that state, then run a final verification measurement. Repeat the optimizer only if a new correction materially changes phase or delay around the crossover.

### 6.7 SPL Calibration

**SPL Calibration** plays **−23-LUFS pink noise** (83 dB SPL target, C-weighted / Slow) to level-tune the output profile. Press **Start noise**, then enter the **Measured SPL** reading and press **Save / Apply**. With a UMIK-1, UMIK-2, or Dayton Audio UMM-6, FXRoute measures the SPL automatically and fills the field, but only when the selected input, the selected calibration file, and the capture gain all match that microphone's requirements; otherwise it says why and falls back to manual entry. Read the meter about a second after the noise starts and average over a few seconds. Autogain and Loudness are temporarily neutralized during the run and restored exactly when it stops, saves, or fails. A saved calibration only affects playback with Loudness on, and the status line reminds you of that.

### 6.8 Graph, target curves and the correction assistants

- The **Measurement graph** shows the frequency response from 20 Hz to 20 kHz with smoothing (**Raw**, **1/6**, **1/3**, **1/1**); the **IR** toggle shows a compact impulse-response preview where available. The preview is a timing/reflection sanity check, not a full IR export.
- The assist selector switches between the **PEQ** and **Convolver** assistants, and the target-curve selector offers **Neutral**, **Bass Shelf**, **Harman-style**, **Bruel & Kjaer-style**, plus any uploaded house-curve file and **Create Custom House Curve…**.
- **PEQ assistant** — sketch up to 12 temporary filters (F1–F12, or click the graph near the 0 dB line), edit frequency, gain, Q, and type, and use **Take L / Take R / Take Both** to build a new PEQ preset in the selected filter bank.
- **Custom House Curve** — create up to 8 frequency/gain points (P1–P8) and save it as a target curve.
- **Convolver assistant** — choose the visible saved runs (exactly one Left and one Right for stereo), then set target curve, correction range, **Max Boost** / **Max Cut**, **Dip Guard**, sample rate, tap length, and the phase mode:
  - **Linear phase** — symmetric FIR
  - **Min. phase** — default for broad room/speaker correction
  - **Min. phase aligned** — minimum-phase correction with measured L/R direct-arrival alignment
  - **Hybrid aligned FIR** — minimum phase below 180 Hz, crossfading to a delay-compensated linear correction above 550 Hz

  The aligned modes need separate saved L/R measurements with valid direct-arrival timing. FXRoute blocks filter creation when the measured L/R timing offset is unsafe; the graph shows the arrival relation (for example `L arrives 5.27 ms later than R`).

### 6.9 Saved runs

**Save current** keeps a run under a name of your choice. Saved runs appear under **Saved runs**; the list can be collapsed with **Open saved (N)**. Each row shows the measured area as a badge (for example `Low L/R · Stereo + Sub`), the capture input and channel with its mic and reference input channels, the timing line, and a quality summary. A badge turns stale when the routing changed after the run, so a band-limited run is never mistaken for a whole-system one; the measurement name field shows the same area in its tooltip.

Checkboxes show/hide runs on the graph (a hidden run is still available as a dashed compare trace), a run's name links to its stored file, and the toolbar offers **Select all**, **Delete selected**, **Merge selected**, and **Close**. Merge combines the checked runs into one new measurement under a name you choose; FXRoute refuses to merge runs that captured different areas or processing, so merge one area at a time. There is no rename action — a name is fixed when the run is saved or merged.

## 7. Technical settings

The FXRoute logo opens **Technical settings**:

- **Providers** — install, update, or remove each streaming backend, show/hide its tab, and connect or disconnect accounts. *Installed* is a fact about the machine; the checkbox only decides whether the tab is shown.
- **Audio Output** — output device, **Sample Rate** policy, **Mode** (Stereo, Stereo + Sub), **Crossover** (Off/On), and **Output Routing** (on devices with more than two outputs)
- **Source** — active input mode and Bluetooth status
- **Music Library** — local folder or a discovered/entered SMB share
- **Device Name** — the `<name>.local` address on the LAN (lowercase letters, digits and hyphens)
- **Maintenance** — installed version, update check, update, and update log
- **HTTPS certificate** — download link when the local HTTPS proxy is enabled

## 8. Maintenance

**Technical settings → Maintenance** shows the installed version, checks for updates, and runs them. Updates are blocked while the checkout contains uncommitted changes; **Restore to Public Release** then saves tracked changes as a patch and untracked files as an archive (both in `backups/`) and resets the checkout to the clean public release — use it only when you no longer need the local changes. User data, music, config, and the runtime cache are not affected. A restart is scheduled after a successful update or restore.

## 9. Local HTTPS certificate

When the optional HTTPS proxy is enabled, FXRoute runs a local certificate authority for the audio PC. Download its certificate from **Technical settings → HTTPS certificate** and import it as a trusted CA only on devices you control on your own LAN. If the CA is regenerated, client devices need the new certificate again.

## 10. Home Assistant / external automation

FXRoute exposes `GET /api/power/state` as a read-only power hint. The payload has four fields: `amp_should_be_on` (boolean), `reason` (`playback`, `measurement_window`, `bluetooth`, `external-input`, or `idle`), `playback_active`, and `measurement_window_open`. `amp_should_be_on` is true while local, TIDAL, Spotify, or Qobuz playback is active, while the measurement assistant is open, while Bluetooth audio is streaming, or while an external input is routed. A Home Assistant or similar automation can use it to switch an amplifier smart plug; FXRoute needs no MQTT broker and never controls the plug itself.

A minimal configuration polls the endpoint as a binary sensor and switches the plug on when playback starts and off after an idle period:

```yaml
rest:
  - resource: "http://fxroute.local:8000/api/power/state"  # Adapt host/port if needed.
    scan_interval: 5
    binary_sensor:
      - name: "FXRoute amp should be on"
        value_template: "{{ value_json.amp_should_be_on }}"

automation:
  - alias: "FXRoute amp on"
    trigger:
      - platform: state
        entity_id: binary_sensor.fxroute_amp_should_be_on
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug.

  - alias: "FXRoute amp off after idle"
    trigger:
      - platform: state
        entity_id: binary_sensor.fxroute_amp_should_be_on
        to: "off"
        for:
          minutes: 20
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug.
```

## 11. If something fails

1. Start with **Radio** — it is the simplest source and proves output and DSP routing.
2. Check the playback bar: does it show a track and a level?
3. Open **Technical settings** and confirm the output device. The line under **Output Routing** names the derived topology and, when the routing is incomplete, the first problem it found.
4. Restart the service and watch its log:

```bash
systemctl --user restart fxroute
journalctl --user -u fxroute -f
```

5. If the native DSP graph is the suspected problem:

```bash
wpctl status
pw-cli ls Node | grep fxroute_dsp
```

6. Reload the browser when it reports a disconnect after a service restart.
7. If a measurement refuses to start, check the **Measuring area** and the microphone in **Setup**: a mono measuring area disables LR Repeat, and a busy or stale output state blocks the sweep.

## 12. What FXRoute expects

- a Linux user-session audio machine (desktop, or headless with user services enabled)
- PipeWire running in that session, with the FXRoute DSP engine in the same graph
- browsers on the local network as the control surface
- a DAC, amp, active speakers, headphones, or similar listening setup
- for more than stereo: an output device with at least three channels, and roles assigned to it in **Output Routing**
- optionally a Spotify client/spotifyd, qbzd, or TIDAL backend for provider playback
- a host microphone input for measurement; a calibrated USB measurement mic enables automatic SPL calibration, and an electrical reference channel on the same input enables precise timing, Speaker Auto Alignment, and the aligned FIR modes

FXRoute does not run as a system daemon and does not replace Spotify Connect or Qobuz Connect — it controls the local players through the user session.
