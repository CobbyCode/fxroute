# Changelog

## 0.4.338 (2026-04-27)
- When deleting the currently active EasyEffects preset, FXRoute now falls back to `Neutral` instead of `Direct`.
- This keeps global helpers like headroom effective after deletion, while `Direct` remains helper-free by design.

## 0.4.337 (2026-04-27)
- Added a second compare-row line below `Listening:` that shows the preset origin chain in English: combined presets render `Chain: Preset A → Preset B → ...`, while regular presets stay calm and consistent with `Chain: Single preset`.
- Combined presets created by FXRoute now persist their `source_presets` provenance in preset metadata so the UI can keep showing the chain later instead of only immediately after creation.

## 0.4.336 (2026-04-27)
- Tightened the EasyEffects `pw-record` monitor reconnect path so it reattaches faster after preset/helper graph changes: discovery polling, link retries, and restart settle waits are all shorter now.
- This keeps the stable reconnect behavior Paul already liked, but should cut the "few seconds until it is attached again" delay noticeably during live use.

## 0.4.335 (2026-04-27)
- Removed the duplicate footer `Peak` badge next to the new dB readout, so clipping is now only indicated by the playback wave visual while the slow dB/VU badge stays readable and calm.

## 0.4.334 (2026-04-27)
- Added a slow, readable post-EasyEffects dB readout next to the playback wave/peak area, driven by the existing `pw-record` monitor but smoothed like a simple VU meter instead of mirroring fast peaks.
- The backend now emits a gently averaged RMS-based `vu_db` level, and the frontend renders it as a compact badge such as `-10 dB` while keeping the existing fast peak warning behavior intact.

## 0.4.333 (2026-04-27)
- Reordered the helper plugin chain slightly so `bass_enhancer#0` now sits before `delay#0`, with `autogain#0` still after both and `limiter#0` still last.
- This matches Paul’s preferred visual/processing order better without changing the compact helper UI or the checkbox/dropdown logic from `0.4.332`.

## 0.4.332 (2026-04-26)
- Refined the new Tone helper UI so it follows the same logic as the other extras: a dedicated checkbox now enables/disables the tone effect, while the dropdown only chooses between `Crystalizer` and `Maximizer`.
- Kept the selected Tone flavor persistent even while the checkbox is off, so re-enabling the helper restores the previous choice instead of forcing `Off` as a separate dropdown mode.

## 0.4.331 (2026-04-26)
- Added the new compact EasyEffects helper pass Paul requested: Autogain now lives under Protection with a target-dB dropdown, and Tone now offers a mutually exclusive `Off` / `Crystalizer` / `Maximizer` selector.
- Matched the new helper plugin payloads to the real EasyEffects preset data on `.104`, while keeping `autogain`, `crystalizer`, and `maximizer` permanently present in the graph and toggled via `bypass` so live switching stays consistent with the earlier helper-toggle work.

## 0.4.330 (2026-04-26)
- When rewriting presets for the global headroom helper, FXRoute now clears helper-managed `output-gain` on all non-helper plugins before assigning headroom to the selected target plugin.
- This prevents stale old headroom values from lingering on a previous plugin after the headroom target selection changes, which was visible on mixed presets like `3plusc`.

## 0.4.329 (2026-04-26)
- Changed headroom helper placement logic to target the first non-helper plugin in `plugins_order` instead of the last one, so mixed chains like `3plusc` apply headroom at the front of the real processing chain.
- This matches the intended safety behavior better for combined presets where early EQ/convolver stages should receive the headroom before later filters.

## 0.4.328 (2026-04-26)
- Changed EasyEffects helper handling so limiter, delay, and bass-enhancer helper plugins stay in the output chain and toggle via plugin `bypass` instead of being physically added/removed from the preset graph on every UI toggle.
- This is intended to reduce the short volume jump/click when enabling or disabling those helpers during active playback, since the graph shape stays stable and only helper state changes.

## 0.4.327 (2026-04-26)
- Refreshed the EasyEffects `pw-record` peak monitor after global helper/extras changes, so toggling limiter, delay, or bass helper plugins during active playback rebinds the PipeWire capture links instead of leaving clipping detection disconnected.
- Headroom-only changes still behave as before, but helper plugins that add/remove nodes now get the same peak-monitor refresh path as preset loads and other effect graph changes.

## 0.4.326 (2026-04-26)
- Reduced helper-toggle audio disruption by avoiding unnecessary PipeWire/WirePlumber restarts when the samplerate drop-in is already unchanged.
- Kept the no-op samplerate apply path resetting `clock.force-rate` back to `0`, so helper-only reapply still clears a stuck forced rate without bouncing the whole user audio stack.
- Fixed the frontend cache-busting tags in `static/index.html` to match the released version again.

## 0.4.325 (2026-04-26)
- Fixed installer BlueZ SPA plugin detection for Debian/Ubuntu multiarch hosts so `libspa-bluez5.so` under paths like `/usr/lib/x86_64-linux-gnu/...` is recognized correctly instead of triggering a false missing-plugin warning.
- Made `uninstall.sh` preserve and restore preexisting EasyEffects autostart/watchdog files instead of deleting them unconditionally.
- Re-validated the installer/uninstaller flow on real Ubuntu 24.04 and Fedora 43 hosts, including helper opt-in paths.

## 0.4.324 (2026-04-26)
- Fixed Bluetooth-input samplerate detection for the live `.104` receiver case where the active BlueZ stream only exposes its rate via `wpctl inspect` (`node.rate` / `node.latency`) instead of `pactl list sources`.
- This makes the compact Bluetooth status actually show details like `AAC · 48 kHz` during active Windows Bluetooth playback.

## 0.4.323 (2026-04-26)
- Added Bluetooth-input samplerate to the compact Settings status so active receiver sessions can show details like `AAC · 48 kHz` alongside the connected device label.

## 0.4.322 (2026-04-26)
- Fixed Bluetooth-input loopback port matching to accept BlueZ stream nodes that expose `output_FL/FR` instead of `capture_FL/FR`, so `bluetooth-input` no longer throws `failed to link ports: No such file or directory` on `.104`.
- This lets the Bluetooth monitor loop stay alive and start the EasyEffects `pw-record` peak detector during active Bluetooth playback.

## 0.4.321 (2026-04-26)
- Started/stopped the EasyEffects peak monitor for active Bluetooth input streaming, so `pw-record`-based clipping detection follows Bluetooth playback too.
- When leaving `bluetooth-input` mode, FXRoute now actively disconnects connected Bluetooth audio-source devices so stale 48 kHz Bluetooth streams do not keep the PipeWire/EasyEffects path pinned during later app-playback samplerate switching.

## 0.4.320 (2026-04-26)
- Fixed Bluetooth status reporting in Settings by detecting active BlueZ receiver streams via `wpctl`, so connected device name and codec (for example AAC) can surface even when `pactl list short sources` is empty.
- Removed the duplicate Bluetooth status wording in the Settings source section and added lightweight polling while the Settings dialog is open so connection state updates live.

## 0.4.319 (2026-04-26)
- Fixed Bluetooth receiver pairing confirmation by registering the BlueZ audio agent with `DisplayYesNo` capability instead of `NoInputNoOutput`, so Windows confirmation requests are accepted instead of being auto-rejected.

## 0.4.318 (2026-04-26)
- Added a persistent BlueZ audio agent for Bluetooth input mode so paired/trusted devices can get service authorization while FXRoute is acting as a Bluetooth audio receiver.
- Kept the agent alive while Bluetooth input mode is active even before an actual BlueZ audio source appears, instead of tearing it down too early.

## 0.4.317 (2026-04-26)
- Relaxed Bluetooth receiver availability detection so hosts with BlueZ + WirePlumber + the PipeWire BlueZ SPA plugin installed no longer show `Bluetooth receiver mode is not currently available` just because no active BlueZ audio node exists yet.
- This should make the new Bluetooth input mode on `.104` become selectable when the local Bluetooth audio stack is present but idle.

## 0.4.316 (2026-04-26)
- Added a compact live `Bluetooth input` source option in Settings, with an honest status line that shows receiver availability, discoverable/waiting state, and the connected device/codec when one is present.
- Enabled a conservative Bluetooth receiver-mode path for `bluetooth-input`: FXRoute now toggles pairable/discoverable mode only while that source mode is active and disables it again on exit.
- Added conservative Bluetooth-input monitoring into `easyeffects_sink` so an active BlueZ input source follows the accepted DSP path without changing the existing audio-output selection behavior.
- Fixed `POST /api/audio/source-mode` to return the updated overview for every source mode and bumped frontend cache-busting to `0.4.316`.

## 0.4.315 (2026-04-26)
- Added a first conservative Bluetooth read-only inventory pass with `GET /api/audio/bluetooth`, reporting adapter/stack readiness, role availability, known devices, and any detected receiver session without changing live routing behavior.
- Extended `GET /api/audio/source-mode` with Bluetooth status metadata and a non-selectable `bluetooth-input` mode placeholder so the backend can advertise the next feature slice safely before activation exists.
- Extended `GET /api/audio/outputs` with Bluetooth-oriented metadata fields (`transport`, `device_class`, `profile`, `active_codec`) for detected BT sinks while keeping output switching behavior unchanged.

## 0.4.309 (2026-04-25)
- Added a compact `Source Mode` write path backed by `GET/POST /api/audio/source-mode`, with persisted mode/input selection and real PipeWire input inventory rendered with human-readable labels.
- Filtered monitor sources out of the external-input list, kept the UI dropdown-based, and automatically fallback to `App playback` when no real inputs are currently available.
- When `External input` is selected, the frontend hides Radio/Spotify/Library tabs and the backend conservatively quiesces app playback so the DSP/output baseline is not competing with local sources.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.309`.

## 0.4.308 (2026-04-25)
- Turned the Settings `Audio Output` inventory into a conservative writable selector backed by `POST /api/audio/outputs`, with `System Default` kept first and explicit outputs shown with human-readable labels.
- Added persisted output-selection state under the user config directory so explicit overrides can be re-applied on startup and `System Default` can restore the previously captured PipeWire default when available.
- Kept the EasyEffects virtual sink non-selectable in the UI to reduce routing-regression risk while still showing active/selected output state separately in the compact settings panel.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.308`.

## 0.4.307 (2026-04-25)
- Turned the existing FXRoute header logo into a compact technical-settings entry point instead of adding a new dedicated settings button.
- Added a first read-only Audio Output inventory via `/api/audio/outputs`, showing `System Default` first and explicit detected outputs after it.
- Added a documented placeholder `Source Mode` section in settings while keeping current playback/routing behavior unchanged.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.307`.

## 0.4.306 (2026-04-24)
- Nudged the collapsed `Create PEQ preset` header block a touch further downward so it aligns more closely with the neighboring DSP cards.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.306`.

## 0.4.305 (2026-04-24)
- Shortened the `Combine` save action label from `Save combined preset` to `Save`.
- Nudged the collapsed `Create PEQ preset` card heading/subtext styling closer to the other DSP cards for a more even top-row appearance.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.305`.

## 0.4.304 (2026-04-24)
- Polished the linked `Gain` UI in the PEQ editor so the mirrored side now also updates its visible dB value immediately.
- Added a tiny `L/R linked` hint on `Gain` bands so the stereo-coupled behavior reads as intentional instead of looking like a bug.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.304`.

## 0.4.303 (2026-04-24)
- Tightened the PEQ editor so `Gain` behaves as a linked stereo band in the UI: switching one side to `Gain` now mirrors the paired band type and dB value to the other side.
- This prevents confusing mixed states like `Gain` on one side and `Bell` on the other while keeping the shared-stereo `Gain` model honest.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.303`.

## 0.4.302 (2026-04-24)
- Reworked the new `Gain` filter type so it no longer exports through an internal Delay helper block.
- `Gain` now behaves as a shared stereo trim on the EasyEffects EQ block, making the EE UI cleaner and keeping Headroom behavior consistent.
- Removed the extra `Gain` explanatory subtitle in the PEQ editor and now reject mismatched dual left/right Gain totals with a clear validation error instead of exporting confusing behavior.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.302`.

## 0.4.301 (2026-04-24)
- Fixed `Gain` PEQ export for dual-channel presets where only one side contains real EQ bands: FXRoute now pads the shorter EasyEffects EQ side with neutral muted bands instead of emitting an invalid empty side.
- Stopped global headroom targeting from landing on the internal neutral `Gain` delay helper stage.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.301`.

## 0.4.300 (2026-04-24)
- Added a dedicated `Gain` filter type to the PEQ editor while keeping the existing channel model.
- `Gain` now hides the frequency/Q-style controls in the editor and exports as a neutral per-channel trim stage instead of pretending to be a tonal bell filter.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.300`.

## 0.4.299 (2026-04-24)
- Shortened the `Combine` helper text so the tile reads more cleanly.
- Fixed the mobile `Combine` preset-name field so it behaves like a normal compact action-row input instead of rendering oversized/square.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.299`.

## 0.4.298 (2026-04-24)
- Refined the first `Compare & Combine` DSP pass: stereo IR imports now auto-import directly without an extra redundant button, while the channel-specific Left/Right create flow stays explicit.
- Tightened the `Combine` tile layout so the preset name uses a normal single-line field, and expanded the combine flow to support up to three source presets while keeping mobile stacked and desktop compact.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.298`.

## 0.4.297 (2026-04-23)
- Reworked the DSP preset area for the first `Compare & Combine` v1 pass: the permanent DSP import tile is replaced by a lower-row `Combine` tile, while filter import moves to a compact top-right `Import…` entry in the DSP area.
- Added a small 2-preset combine workflow that creates a new preset from `Preset 1` and `Preset 2` without modifying the originals, while preserving source order and keeping the compare area focused on `Preset A`, `Preset B`, `Compare A/B`, and `Delete active`.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.297`.

## 0.4.296 (2026-04-23)
- Changed newly created convolver presets to default to `autogain: false` instead of `true`, so FXRoute no longer hides IR level differences behind automatic convolver gain compensation by default.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.296`.

## 0.4.295 (2026-04-23)
- Fixed FXRoute PEQ preset generation so EasyEffects-compatible filter type strings are now written for non-bell bands (`Lo-pass`, `Hi-pass`, `Lo-shelf`, `Hi-shelf`) instead of the previously mismatched labels that left those type fields blank in EasyEffects.
- Fixed the built-in `Direct` preset so it is now a true helper-free empty chain instead of carrying a baked-in limiter. Existing `Direct.json` presets are automatically rewritten to the helper-free form when FXRoute lists EasyEffects presets.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.295`.

## 0.4.294 (2026-04-22)
- Improved URL download error handling for common YouTube / outdated `yt-dlp` failures. FXRoute now shows a friendlier hint to check/update `yt-dlp` instead of surfacing only a low-level 403/Forbidden-style error.
- Updated the README troubleshooting section with the exact `yt-dlp` update command: `cd ~/fxroute && .venv/bin/pip install -U yt-dlp`.

## 0.4.293 (2026-04-22)
- Tightened the yt-dlp format selector for URL imports from `bestaudio/best` to strict `bestaudio`, so FXRoute no longer falls back to combined video formats like MP4 when an audio-only stream is unavailable.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.293`.

## 0.4.292 (2026-04-22)
- Fixed library scanning and upload/import acceptance for original-format URL downloads such as `.webm`, `.weba`, `.opus`, and `.oga`, so successful yt-dlp imports now appear in the library instead of being silently skipped.
- Updated the Library import UI copy to reflect WebM/Opus support.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.292`.

## 0.4.291 (2026-04-22)
- Changed URL downloads to preserve the source audio format whenever possible instead of forcing a lossy MP3 transcode by default.
- Added a clearer optional `DOWNLOAD_TRANSCODE_FORMAT` setting for users who explicitly want yt-dlp downloads converted, while treating the old `AUDIO_FORMAT=mp3` installer default as deprecated legacy config instead of a forced transcode.
- Updated installer and docs so new setups no longer suggest MP3 as the default URL download format.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.291`.

## 0.4.290 (2026-04-21)
- Fixed EasyEffects global helper behavior so limiter, headroom, delay, and bass enhancer are now pushed into all non-Direct presets when changed, instead of only updating the currently active preset.
- Hardened preset loading so FXRoute also syncs the saved global helper state into the target preset before loading it, which keeps A/B compare preset switches from drifting away from the helper checkboxes.
- Bumped the cache-busted frontend asset references in `static/index.html` so deployed systems stay version-synced with `0.4.290`.

## 0.4.289 (2026-04-21)
- Hardened the library import panel state so opening or closing the panel also resets the selected upload filename, preventing stale local file selections from lingering in the UI even when no upload is started.
- Bumped the cache-busted frontend asset references in `static/index.html` so the import-panel hardening reaches deployed systems immediately.

## 0.4.288 (2026-04-21)
- Fixed the library upload UI so the selected filename is cleared again after both successful and failed uploads, instead of leaving a stale filename visible in the import area.
- Bumped the cache-busted frontend asset references in `static/index.html` so the upload UI fix reaches deployed systems immediately.

## 0.4.287 (2026-04-21)
- Fixed the Spotify tab frontend so Shuffle and Loop no longer preemptively disable each other before sending the real Spotify command. FXRoute now lets Spotify keep both active when the desktop app supports it.
- Bumped the cache-busted frontend asset references in `static/index.html` so the updated Spotify control logic reaches deployed systems immediately.
- Tightened `deploy.sh` so routine remote deploys do not copy `media/raw/`, `media/reference/`, or the local-only `scripts/prepare-public-export.sh` helper onto the playback host.

## 0.4.286 (2026-04-20)
- Extended the installer/uninstaller bookkeeping for the optional LAN comfort layer. `install.sh` now records hostname, Avahi, and Caddy baseline/ownership state in `~/.config/fxroute/install-state.json`, so future uninstall runs can distinguish FXRoute-owned LAN changes from pre-existing user setup.
- `uninstall.sh` now restores a previous hostname only when FXRoute had changed it and the hostname still matches the FXRoute-set value, only removes or disables Avahi when the recorded state says FXRoute introduced that Avahi state, and restores a previously active default `caddy.service` when FXRoute had disabled it for the FXRoute-owned reverse proxy.

## 0.4.285 (2026-04-20)
- Fixed the new optional Caddy comfort step in `install.sh`. The temporary-file cleanup no longer trips over unset local variables on function return, and the setup now disables the package-default `caddy.service` before enabling the FXRoute-owned reverse proxy so the port-80 health check can hit the intended config instead of Caddy's stock 404 site.

## 0.4.284 (2026-04-20)
- Added the second optional LAN comfort step to `install.sh`: after the basic install succeeds, interactive runs can now optionally enable a small FXRoute-owned Caddy reverse proxy on port 80 so the chosen `.local` name works without `:8000`. The `.local` chooser was also refined so existing Avahi setups can be kept or switched to a cleaner dedicated hostname such as `fxroute.local`.
- `uninstall.sh` now removes the optional FXRoute Caddy reverse proxy service/config if that comfort layer was enabled, while still leaving the core FXRoute uninstall path independent from Avahi/Caddy.

## 0.4.283 (2026-04-20)
- Added an optional post-install Avahi mDNS comfort step at the end of `install.sh`. After the basic install summary, interactive runs can now offer a `.local` LAN name such as `fxroute.local`, `fxroute-test.local`, or `fxroute-fedora.local` without making the core FXRoute install depend on that comfort layer.

## 0.4.282 (2026-04-20)
- Removed the generic `.local`/mDNS hint from the installer summary again. The current installer does not yet provision Avahi/`fxroute.local`, so the end text now sticks to the guaranteed local and LAN-IP URLs until LAN discovery is automated for real.

## 0.4.281 (2026-04-20)
- Improved the installer end-of-run summary. It now calls out the local URL, LAN IP URL, and a `.local` mDNS/hostname hint more explicitly, reminds the user to check the firewall for the app port, and gives a clearer next-step hint for launching EasyEffects when the socket is still missing after install.

## 0.4.280 (2026-04-20)
- Hardened installer EasyEffects detection after the `.70` validation round. `install.sh` now treats Flatpak EasyEffects as installed only when it actually appears in `flatpak list`, instead of trusting the broader `flatpak info` probe that could mis-detect the first reinstall immediately after a full uninstall.

## 0.4.279 (2026-04-20)
- Added a small uninstall comfort feature. FXRoute now records whether it installed EasyEffects itself, and `uninstall.sh` only offers to remove EasyEffects when that marker says the package originally came from the FXRoute installer. Existing user-managed EasyEffects installs are left alone.

## 0.4.278 (2026-04-20)
- Fixed a radio-only samplerate renegotiation quirk seen on a real Ubuntu validation target. If a just-started radio stream briefly leaves the PipeWire sink at the wrong rate, FXRoute now does one narrow one-time EasyEffects preset bounce to re-negotiate the graph instead of touching normal local playback.
- Fixed EasyEffects A/B compare persistence on reloads and cache resets. The frontend now respects the server-saved compare state (`presetA`/`presetB`) instead of accidentally dropping slot B when rebuilding state after refresh.

## 0.4.277 (2026-04-19)
- Added the first real Linux installer/uninstaller pass for FXRoute with Ubuntu/Fedora/Tumbleweed packaging, user-service setup, EasyEffects bootstrap presets, Flatpak-focused EasyEffects setup, helper scripts, and safer uninstall defaults.
- Hardened the EasyEffects runtime path to handle native vs Flatpak sockets more explicitly, including the Flatpak `tmp` socket path, active-preset-only helper/extras handling, and Python 3.14-compatible dependency pins.
- Fixed several playback/runtime issues found during distro validation: local queue end-state handling, shuffle-off restore, lighter `/api/status` payloads during playback, and a small frontend Spotify-tab visibility bug on refresh when Spotify is not installed.

## 0.4.276 (2026-04-19)
- Applied a second very narrow FXRoute header/logo polish pass. The monogram tile is a touch more balanced and quieter, the mark and wordmark sit closer as one unit, the desktop subtitle is weaker again instead of louder, and mobile spacing was tightened slightly without changing the identity.

## 0.4.275 (2026-04-19)
- Applied a narrow FXRoute logo/header refinement pass. The header lockup stays recognizably the same, but the brand mark is flatter and calmer, the mark/title/subtitle spacing is tighter and more coherent, the subtitle is quieter, and the overall header feels a little more product-like without turning into a rebrand.

## 0.4.274 (2026-04-19)
- Fixed a stale Library import status issue. Closing and reopening the Library import panel now clears old terminal upload/download feedback (`complete`, `error`, `cancelled`) instead of leaving it stuck in the hint area indefinitely, while active uploads/downloads still remain visible.

## 0.4.273 (2026-04-19)
- Added pass-1 Library ZIP album import through the existing upload flow. `/api/library/upload` now accepts `.zip`, extracts safely under the library incoming folder with traversal/junk-path filtering and predictable folder/file suffixing, refreshes the recursive library scan after import, and returns a more useful import summary while keeping normal single-file audio uploads working. The Library import copy and file accept list now mention ZIP album support without redesigning the UI.

## 0.4.272 (2026-04-19)
- Applied a narrow footer CSS-only polish pass on the playback bar. The shell spacing and visual treatment are a little calmer, the center song info reads more clearly, footer chips are slightly tidier, controls/seek spacing are more balanced, and mobile spacing was tightened carefully, all without changing footer logic, source ownership, seek/volume behavior, or DOM structure.

## 0.4.271 (2026-04-19)
- Small DSP GUI calm-down pass. Delay and Bass helper value controls now stay hidden until their toggle is enabled, the A/B compare badge/button wording leans a little more toward listening (`Listening`, `A/B`), the import area now marks the stereo path as the main path and Left/Right as the channel-specific path, and the PEQ disclosure pill now shows live `L/R` band counts plus expand/collapse state.

## 0.4.270 (2026-04-19)
- Slimmed the new Headroom selector so it only offers the currently useful range `-2 dB` through `-6 dB`. `Off` still covers the no-headroom case, the default remains `-3 dB`, and out-of-range older values now fall back quietly to `-3 dB` in the UI instead of bloating the list.

## 0.4.269 (2026-04-19)
- Added variable EasyEffects headroom selection to Output extras. Headroom still defaults to `-3 dB`, but can now be set in whole-dB steps from `0 dB` to `-9 dB`, persists in the existing global extras file, and now flows through preset create/import paths so the selected value is stamped consistently instead of reverting to `-3 dB`.

## 0.4.268 (2026-04-18)
- Tightened the DSP import wording a little further without changing the layout. The section subtitle now reads `Stereo convolver or Left/Right REW.`, and the drop areas now say `Drop stereo file`, `Drop Left file`, and `Drop Right file` for quicker scanning.

## 0.4.267 (2026-04-18)
- Tightened the DSP import copy slightly. The convolver import hint now says `Stereo .irs or .wav for convolver.` instead of `Choose stereo...`, removing the extra browser-like wording.

## 0.4.266 (2026-04-18)
- Tightened the first DSP polish pass copy and badge layout. The A/B card now uses shorter, clearer wording again (`Active` instead of `Now listening`), the active badge is always centered consistently, and the extra explanatory subtexts under A/B and the helper groups were removed to avoid redundant or internal-feeling wording.

## 0.4.265 (2026-04-18)
- Narrow DSP polish pass 1 on the FXRoute DSP tab. The top card now reads as an A/B listening compare with a stronger active-listening badge and clearer A/B slots, while delete is still available but visually quieter. Output extras are now grouped into Protection, Timing, and Tone clusters without changing helper save behavior. The Create PEQ workshop is now progressively disclosed behind a collapsible panel to keep the tab lighter by default.

## 0.4.264 (2026-04-18)
- Reduced unnecessary save spam from the Output extras numeric helper fields. Delay `ms` and Bass Enhancer `dB` value changes now debounce for about 2 seconds while you adjust them, then still save immediately on blur. Toggle-style helper switches keep their shorter debounce.

## 0.4.263 (2026-04-18)
- Added a minimal frontend busy-lock for EasyEffects A/B preset switching. The A/B selects and Toggle button now disable immediately while a preset load is in flight, repeated clicks are ignored, and the controls re-enable only after success or failure. No peak-monitor, `pw-record`, PipeWire, or routing logic was changed.

## 0.4.262 (2026-04-18)
- Fixed the Radio tab mobile Manage overlay so opening `Manage` no longer auto-focuses the station URL field and pop open the on-screen keyboard immediately. The dialog now lands on the Close button first, and the URL input is only focused once you actually interact with it.

## 0.4.261 (2026-04-18)
- Restored delete protection for the built-in EasyEffects presets. `Direct` and `Neutral` are now protected again, alongside legacy `Pure`, and cannot be removed from the preset list.

## 0.4.260 (2026-04-18)
- Pinned `Direct` and `Neutral` to the top of the EasyEffects preset list when those preset files exist, ahead of the legacy `Pure` fallback and the normal alphabetical presets.
- Updated the Output extras helper text to `Global helpers. Applied automatically, except Direct.`

## 0.4.248 (2026-04-18)
- Restored Spotify Shuffle/Loop controls in the tab. After re-checking on a real validation system, both playerctl paths became writable again in the current Spotify context, so the temporary workaround that hid those controls has been reverted.

## 0.4.247 (2026-04-18)
- Hid the non-working Spotify Shuffle/Loop buttons on the Linux playerctl/MPRIS path. Transport/seek still work, but shuffle/loop write attempts on this setup do not actually change Spotify state, so the UI no longer exposes dead controls.

## 0.4.246 (2026-04-18)
- Excluded the Delay helper from automatic headroom targeting as well. The fixed `-3 dB` headroom reduction is no longer stamped onto `delay#0`, keeping helper stages free of duplicate attenuation just like the limiter and Bass Enhancer helpers.

## 0.4.245 (2026-04-18)
- Excluded the Bass Enhancer helper from automatic headroom targeting. The fixed `-3 dB` headroom reduction is no longer stamped onto `bass_enhancer#0`, avoiding an unintended double reduction when Bass Enhancer is enabled alongside the normal headroom flow.

## 0.4.244 (2026-04-18)
- Tightened the frontend samplerate burst polling cadence after local track changes so the footer rate indicator catches up faster again during natural queue transitions, without changing the backend audio handoff logic.

## 0.4.243 (2026-04-18)
- Fixed the natural EOF mixed-rate handoff gap more directly. When a local track ends naturally, the fallback queue path now still treats the finished track URL as the previous source context before loading the next one, so the explicit stop-plus-settle handoff is preserved even after mpv has already cleared `current_file`. The frontend now also starts samplerate burst polling on server-pushed local track changes.

## 0.4.242 (2026-04-18)
- Fixed another local queue race in the mixed-rate fallback path: after queueing the next track, stale end-of-track events could still re-enter auto-advance and jump from track 1 to track 3. The backend now suppresses extra queue advancement while a queued track transition is still being confirmed.

## 0.4.241 (2026-04-18)
- Fixed the remaining mixed-samplerate local queue handoff gap in the conservative queue path. Auto-advance and manual Next/Previous for non-native local queues now use the same explicit stop-plus-settle handoff as direct local track changes, reducing skipped tracks and stuck samplerate carryover in mixed-kHz playlists.

## 0.4.240 (2026-04-18)
- Normalized Library selection and playlist loading back to library order instead of preserving checkbox click order. This avoids broken reverse-built local playlists where autoplay could start on the chosen track but queue continuation/track-end behavior no longer matched the saved selection.

## 0.4.239 (2026-04-18)
- Added a conservative hybrid local-queue path for smoother playback: homogeneous local queues with known, matching sample rates now use a native mpv playlist path, while mixed-rate or uncertain transitions stay on the existing explicit handoff path to preserve samplerate and EasyEffects stability.

## 0.4.238 (2026-04-17)
- Playlist clicks in the Library tab now behave like track clicks: selecting a playlist also starts playback immediately, using the playlist tracks as the active local queue.

## 0.4.237 (2026-04-17)
- Final comparison pass for the footer peak-warning flicker: raised hold time slightly from 20 ms to 30 ms to compare whether a tiny bit more persistence feels closer to EasyEffects without looking latched.

## 0.4.236 (2026-04-17)
- Test tuning for the footer peak-warning flicker: reduced hold time further from 75 ms to 20 ms so borderline limiter activity should flicker more like EasyEffects instead of appearing latched.

## 0.4.235 (2026-04-17)
- Tuned the footer peak-warning behavior to feel slightly closer to EasyEffects' own limiter flicker: the warning now uses a shorter hold (75 ms instead of 100 ms) and requires 2 consecutive over-threshold hits before latching.

## 0.4.234 (2026-04-17)
- Added a little more top breathing room for the centered artist line in the playback footer so it no longer feels visually pressed against the active green border/glow.

## 0.4.233 (2026-04-17)
- Disabled browser autocomplete/history suggestions for generic station names and DSP preset name inputs. This avoids duplicate and ever-growing suggestion popups like repeated `bigFM` entries or long preset-name histories.

## 0.4.232 (2026-04-17)
- Hid the empty Library import status container when there is no active download/upload state, removing the stray blank outlined row at the bottom of the Import panel.

## 0.4.231 (2026-04-17)
- Further spacing polish for the combined saved-station management area. Increased vertical rhythm between the `Station URL` and `Cover image URL (optional)` rows, and added a little more separation above the `Save changes` / `Delete station` button row so the layout better matches the DSP tab cards.

## 0.4.230 (2026-04-17)
- Small spacing polish for the combined saved-station management area. The `Save changes` and `Delete station` buttons now sit with a little more breathing room below the cover image URL field.

## 0.4.229 (2026-04-17)
- Reworked the lower Manage Stations section from delete-only into a combined saved-station management area. You can now select an existing station, edit its stream URL and optional custom cover URL, save changes, or delete it from the same panel. Also cleaned up the Add station helper copy so the lower hint no longer repeats `Paste or drop a URL`.

## 0.4.228 (2026-04-17)
- Added an optional custom cover image URL for manually added radio stations. The Manage Stations dialog now offers a cover URL field for non-Soma streams, the backend stores it as `custom_image_url`, and the radio grid prefers that user-supplied artwork before built-in station art or the generated fallback tile.

## 0.4.227 (2026-04-17)
- Fixed generic station fallback artwork selection. The frontend no longer guesses `/static/station-art/<station-id>.*` for arbitrary non-Soma stations like `bigFM`, and instead only infers local artwork candidates for real/known SomaFM stations. Generic custom stations now render the generated fallback cover immediately instead of starting on a broken image path.

## 0.4.226 (2026-04-17)
- Fixed the generic Add Station button path in the Manage Stations dialog. The click handler now calls `saveStation()` correctly instead of accidentally passing the browser click event as a pseudo-URL, which prevented any POST request for non-Soma entries. The dialog now also resets back to a fresh create state on close/open, and the manage buttons are explicitly `type="button"`.

## 0.4.225 (2026-04-17)
- Hardened Radio grid artwork loading for SomaFM stations. The frontend now tries the explicit image from station data first, then falls back through inferred local `/static/station-art/<slug>.*` candidates before using the generic generated placeholder. This restores known SomaFM covers even if station image metadata is temporarily empty or stale in the client.

## 0.4.224 (2026-04-17)
- Improved the Manage Stations dialog for generic stream URLs. Station form status now clears when the URL or name changes and when the dialog is reopened, and pressing Enter in the station name field now submits the add action when the form is valid. This prevents the UI from feeling stuck after the “enter a station name” prompt on non-SomaFM links.

## 0.4.223 (2026-04-17)
- Added a short footer content freeze during play-triggered local/radio handoffs and queue next/previous actions. Instead of rendering transient intermediate footer states during the stop/start window, FXRoute now briefly keeps the last stable footer content on screen, then re-renders once the new state settles. This is a frontend-only stability tweak aimed at the remaining tiny footer twitch without changing audio behavior.

## 0.4.222 (2026-04-17)
- Fixed a brief footer twitch during local Library track switches. The root cause was a short stopped-state during hard handoff where paused Spotify metadata could momentarily reclaim footer ownership and expand the artist row before local playback resumed. A short local-footer hold now keeps the footer on the requested local/radio context during that transition window.

## 0.4.221 (2026-04-17)
- Made the samplerate footer more responsive after play-triggered transitions by adding a short frontend burst-poll sequence on successful local/radio play and queue next/previous actions. This does not force any samplerate or change backend audio logic, it just refreshes the read-only samplerate status quickly for the first few seconds so footer updates better track the real renegotiation.

## 0.4.220 (2026-04-17)
- Added explicit hard handoff for real play-triggered source changes between local Library and Radio, while keeping the queue path untouched and avoiding any stop/start on mere tab navigation. Manual local-to-local track switches still use the same targeted stop-plus-settle path, but local<->radio play requests now also stop and briefly settle before `loadfile` so mpv/PipeWire renegotiate more cleanly at source boundaries.

## 0.4.219 (2026-04-17)
- Hardened local Library metadata extraction by falling back to a non-`easy` Mutagen read for audio info when needed, so `sample_rate_hz` and duration are picked up more reliably for formats like MP3/M4A as well as FLAC.

## 0.4.218 (2026-04-17)
- Extended local Library track metadata with `sample_rate_hz`, extracted during the normal scan/import path via Mutagen. This lays the groundwork for cleaner samplerate-aware transition policy later, without relying on filename guesses or ad-hoc probing during playback.

## 0.4.217 (2026-04-17)
- Added a targeted clean-transition path for manual local Library track switches. When switching directly from one local file to another different local file, FXRoute now performs an explicit stop plus short settle delay before the next `loadfile`, because reproducible 44.1↔48 kHz tests showed direct replace-switches could leave PipeWire/EasyEffects stuck on the previous rate while stop/start restored correct negotiation.

## 0.4.216 (2026-04-17)
- Switched FXRoute's shared volume read/write target back to the real PipeWire default output sink. After removing the hidden polling-side volume writes, live tests showed changing the hardware/system sink no longer immediately disturbed samplerate negotiation, while targeting only `easyeffects_sink` left FXRoute reporting one volume and the actual audible output following another.

## 0.4.215 (2026-04-17)
- Removed hidden volume write side-effects from status/render paths introduced by the loudness fix. `build_playback_payload()` and `get_spotify_ui_state()` are read-only again instead of repeatedly forcing MPV/Spotify source volume back to 100 during ordinary polling, websocket broadcasts, and UI refreshes. This targets the likely regression where the graph became unstable even though `pw-record` plus native samplerate switching had previously coexisted.

## 0.4.214 (2026-04-17)
- Reverted the experimental file-samplerate heuristic for peak-monitor control. We do not want product behavior to branch on ad-hoc per-file probing without understanding the real bug, because that kind of workaround already caused later regressions and confusion.

## 0.4.213 (2026-04-17)
- Refined the peak-monitor rollback: instead of disabling `pw-record` for all local Library playback, FXRoute now probes the local file samplerate and only suspends the peak monitor for high-rate local tracks above 48 kHz. Normal local playback keeps peak monitoring, while native-kHz switching for high-rate content stays protected.

## 0.4.212 (2026-04-17)
- Restored the earlier samplerate-protection rule for local Library playback: the `pw-record` peak monitor is now explicitly suspended while local tracks are actively playing, instead of being re-armed for all sources during diagnostics. This should bring back the better native-kHz switching behavior that regressed after the diagnostic rollback.

## 0.4.211 (2026-04-17)
- Adjusted the new shared volume control to target `easyeffects_sink` when available instead of the raw hardware default sink, because FXRoute's actual audio path runs through EasyEffects and the direct hardware target risked interfering with graph behavior

## 0.4.210 (2026-04-17)
- Switched FXRoute's shared volume control from per-source app volume to PipeWire's default output-sink volume, so the UI no longer leaves MPV or Spotify internally stuck below 100% while still showing 100% in FXRoute
- Added source-volume pinning that forces MPV and Spotify back to 100% if they drift, while exposing the output/device volume to the UI instead
- Reordered DSP extras so headroom is applied to the last non-limiter plugin and the limiter remains the final stage in the EasyEffects chain

## 0.4.209 (2026-04-17)
- Clamped the mobile DSP tab layout to the viewport width and removed tiny horizontal overflow so Android Chrome is less likely to do the slight rescale when opening DSP

## 0.4.208 (2026-04-17)
- Forced browser text-size adjustment to stay at 100% so Android Chrome is less likely to apply a slight automatic rescale when switching into the DSP tab

## 0.4.207 (2026-04-17)
- Increased mobile DSP form control font sizes to 16px to avoid the slight Safari/iPhone auto-zoom effect when entering the DSP tab or interacting with its inputs

## 0.4.206 (2026-04-17)
- Moved the import-filter convolver hint into the main drop area itself so the stale-looking filename line becomes useful helper text when empty and the extra help row below can be removed for a little more space

## 0.4.205 (2026-04-17)
- Simplified the Output extras helper text from "Global helpers for ALL new presets. Applied automatically." to "Global helpers. Applied automatically." to avoid the misleading "new presets" wording

## 0.4.204 (2026-04-17)
- Removed the sticky success text from the Manage presets card after imports and PEQ creation, so transient toast notifications handle success while the inline area stays free for progress and error messages

## 0.4.203 (2026-04-17)
- Fixed WAV/IRS convolver import crashing after a successful drop/import because the frontend still tried to write to a removed preset-select element
- Made file drop areas behave more explicitly with multiple dropped files by using the first file and showing that choice in the filename/status feedback

## 0.4.202 (2026-04-17)
- Added a little more vertical spacing between the active preset badge and the Toggle/Delete button row in Manage presets for a less cramped look

## 0.4.201 (2026-04-17)
- Simplified the Manage presets card structure again so the active preset badge sits on its own full row and the Toggle/Delete buttons sit below it with more breathing room, which also fixes the broken mobile layout more cleanly

## 0.4.200 (2026-04-17)
- Improved the Manage presets card spacing by separating the active badge and action buttons into a cleaner meta row with more room around Toggle A/B and Delete
- Added a centered inner panel to Output extras so the controls sit inside a subtle darker-gray card-within-card that matches the surrounding DSP card language better

## 0.4.199 (2026-04-17)
- Removed the redundant Reset button from the manual PEQ card and aligned its action row more closely with the import-filter card, using just preset name plus Create

## 0.4.198 (2026-04-17)
- Matched the desktop Create PEQ action row to the new mobile style so preset name sits on its own row there too, with Reset/Create below for a more consistent layout across screen sizes

## 0.4.197 (2026-04-17)
- Moved the Create PEQ mobile bottom row to a cleaner two-line layout: preset name on its own row, Reset/Create below, because keeping all three controls on one line was too cramped

## 0.4.196 (2026-04-17)
- Tuned the mobile bottom action rows again so the preset-name input and action buttons feel more proportional to the rest of the UI, with slightly larger controls and a bit more spacing between the field and the Create button

## 0.4.195 (2026-04-17)
- Rebuilt the mobile bottom action rows for the filter-import and Create PEQ cards as explicit small grid layouts instead of stacking/flex overrides, so the preset-name field can stay wide and low while the action buttons sit beside it cleanly
- Removed the accidentally deployed temporary CSS helper file from the project

## 0.4.194 (2026-04-17)
- Added a final end-of-file mobile override for the import/filter and Create PEQ bottom action rows after earlier fixes were being overwritten by a later generic mobile effects rule; preset fields and buttons should now stay compact, low-height, and inline instead of turning into tall stacked blocks

## 0.4.193 (2026-04-17)
- Fixed the real mobile layout regression in the filter-import and Create PEQ action rows by overriding the generic stacked 100%-width mobile button/input rule for these rows, shrinking the preset-name field and keeping the bottom controls compact and properly placed

## 0.4.192 (2026-04-17)
- Reduced the height of the preset-name input used in the filter import and Create PEQ action rows after the previous mobile tweak made the field narrow enough but still visually too tall

## 0.4.191 (2026-04-17)
- Applied the same compact mobile preset-name sizing to the Create PEQ card so its bottom row no longer lets the preset input dominate small screens

## 0.4.190 (2026-04-17)
- Tightened the mobile width of the preset-name input in the import filter card so it no longer dominates the bottom action row on smaller screens

## 0.4.189 (2026-04-17)
- Fixed the PEQ builder's broken visual layout by removing the inherited 200px minimum input width inside PEQ band fields and collapsing the dual Left/Right PEQ columns to a single column earlier on narrower screens

## 0.4.188 (2026-04-17)
- Reworked the manual PEQ builder into a compact dual Left/Right layout to match the new dual-filter import flow, with separate Left and Right band lists instead of one stereo-linked list
- Removed the per-band Enabled checkbox, moved the preset-name field to the bottom action row, shortened the create button to `Create`, and tightened the PEQ band field sizing for a denser layout

## 0.4.187 (2026-04-17)
- Raised the PEQ band ceiling from 16 to 20 so FXRoute matches REW's normal 20 filter slots
- Added the same 20-band limit to the frontend PEQ builder so it stops adding bands past the supported maximum instead of failing later at create time

## 0.4.186 (2026-04-17)
- Expanded the dual filter import so Left/Right now accepts either formatted REW text or separate Left/Right convolver `.wav` / `.irs` files, with the backend merging dual IR uploads into one stereo EasyEffects kernel before creating the preset
- Simplified the Left/Right import labels to generic `Left filter` / `Right filter` so the same UI works for both dual PEQ text and dual convolver files

## 0.4.185 (2026-04-17)
- Fixed the filter-import help copy being silently overwritten by `updateEffectsImportUi()` in JavaScript: the idle help text now stays on the shorter convolver-only wording, and the detected-file messages were shortened to match the cleaned-up import UI

## 0.4.184 (2026-04-17)
- Forced a fresh asset-bump deploy after confirming the filter-import help copy was already corrected in HTML, to flush stale cached UI text on clients that still showed the older mixed convolver/REW wording

## 0.4.183 (2026-04-17)
- Shortened the dual-REW paste placeholders one step further to just `Paste Left filter` / `Paste Right filter` for a cleaner, quieter import UI

## 0.4.182 (2026-04-17)
- Tightened the new filter-import copy again: removed extra REW wording where it was repetitive, made the Left/Right drop-zone text more symmetrical, and simplified the convolver hint to only mention stereo `.irs` / `.wav`

## 0.4.181 (2026-04-17)
- Rearranged filter import UI into one tighter card: convolver and dual REW import now live together, labels/text are shorter, REW drop zones are smaller, the preset-name field moved down next to a shorter Create button, and the Left/Right paste fields clear themselves after a successful dual-PEQ import

## 0.4.180 (2026-04-17)
- Added a real REW dual-PEQ workflow: separate Left/Right upload areas and paste boxes plus a Create dual PEQ preset action, backed by new dual-channel EasyEffects preset generation so L and R REW text can now be imported into separate channels instead of being forced through stereo-linked import

## 0.4.179 (2026-04-17)
- REW PEQ import now supports the structured/formatted Generic text or clipboard export in addition to the earlier Configurable PEQ text format, skipping `None` lines and importing real PEQ rows into the existing stereo-linked workflow

## 0.4.178 (2026-04-16)
- Final tiny Output extras layout nudge: kept the restored stable grid untouched and only reduced the card's desktop side padding a little more so the section can use slightly more width without changing the internal alignment

## 0.4.177 (2026-04-16)
- Reverted the Output extras grid back to the last stable geometry after 0.4.176 over-stretched the internal layout, then only reduced the card's horizontal padding slightly so the section can breathe wider without disturbing the established grid alignment

## 0.4.176 (2026-04-16)
- Adjusted the Output extras desktop grid to use more of the card width without changing the established Delay/Bass relationships, so the section feels less left-packed and more in line with the way the PEQ card fills its space

## 0.4.175 (2026-04-16)
- EasyEffects preset ordering now treats only `Pure` as special: it is listed first as the built-in empty/bypass fallback, while all other presets are shown in normal alphabetical order and the delete button stays disabled when `Pure` is active

## 0.4.174 (2026-04-16)
- Added frontend PEQ band validation before preset creation, with clearer per-band error messages and safer numeric parsing so invalid or blank band values do not fall through as raw backend validation errors

## 0.4.173 (2026-04-16)
- Clarified the new PEQ create/import hint text so it explicitly tells the user to select the preset as A or B, matching the current Manage presets A/B workflow more closely

## 0.4.172 (2026-04-16)
- Applied the same no-autoload rule to REW PEQ import as to PEQ preset creation: imported presets are now saved and listed without becoming active automatically, and the status hint tells the user to select them manually in Manage presets

## 0.4.171 (2026-04-16)
- Stopped auto-loading newly created PEQ presets because it conflicted with the newer A/B preset logic and caused active-state display mismatches; creation now just saves the preset and shows a short hint to select it manually in Manage presets

## 0.4.170 (2026-04-16)
- Fixed footer transport/volume routing across tabs: play/pause, previous/next, and volume now follow the effective active playback owner (Spotify vs local/radio) instead of the currently visible tab, preventing cross-tab footer actions from falling back to stale library queue state or sending volume changes to the wrong backend

## 0.4.169 (2026-04-16)
- Restored a tighter explicit desktop grid for Output extras after 0.4.168 over-shifted the right-hand controls: Delay R now sits slightly further left again, and Bass amount is positioned directly beneath that right-hand block instead of being spread too far across the card

## 0.4.168 (2026-04-16)
- Let the Output extras desktop grid use more of the card width while preserving the calmer alignment from 0.4.167, reducing the overly left-biased feel without breaking the Delay/Bass visual relationship

## 0.4.167 (2026-04-16)
- Simplified Output extras desktop alignment into a calmer three-column layout so Delay stays grouped while the right delay block can sit more naturally above Bass amount, with slightly softer spacing and less grid micromanagement

## 0.4.166 (2026-04-16)
- Refined Output extras spacing and alignment again: Delay and Bass rows now use a light desktop grid so the right delay block can visually sit above the Bass amount field while keeping the card slightly airier than the ultra-compact revision

## 0.4.165 (2026-04-16)
- Reworked Output extras layout back toward a denser, more cohesive structure after the previous visual revision regressed: reduced row spacing, kept Delay and Bass controls as compact connected units, and removed the over-pushed right alignment that made Bass feel detached

## 0.4.164 (2026-04-16)
- Tightened Output extras layout again so the Bass enhancer amount block is placed in the right-side grid position instead of merely nudged, making it visibly line up closer to the right delay field on desktop layouts

## 0.4.163 (2026-04-16)
- Polished Output extras field alignment so Bass enhancer `Amount` uses the same compact field width as the delay inputs and sits visually closer under the right-side delay controls instead of floating as a wider detached field

## 0.4.162 (2026-04-16)
- Cleaned up Manage presets A/B compare logic into one consistent flow: shared helpers now compute the effective active slot, selection-change autoload behavior, and toggle target selection from the same state rules, reducing accumulated patchy compare code and making first-load / active-slot-change behavior consistent

## 0.4.161 (2026-04-16)
- Unified Manage presets A/B autoload behavior: changing the currently active slot now auto-loads the newly selected preset for both A and B, and when no compare slot is effectively active yet, the first chosen slot becomes active automatically regardless of whether it was selected in A or B

## 0.4.160 (2026-04-16)
- Improved initial Manage presets A/B behavior: choosing the first preset in slot A now auto-loads it immediately when no B preset is configured yet, so the first real compare target becomes active without needing an extra toggle press

## 0.4.159 (2026-04-16)
- Fixed A/B toggle direction after compare-state persistence moved server-side: the UI and backend now infer the effective active side from the currently active preset, so toggling still switches to the opposite slot even when `activeSide` was unset or stale after resets/refreshes

## 0.4.158 (2026-04-16)
- Moved Manage presets A/B compare persistence from browser-only storage to server-side EasyEffects state, so preset A/B selections survive cache resets and browser changes; preset load now also updates the stored active side when it matches A or B

## 0.4.157 (2026-04-16)
- Fixed Manage presets A/B compare state so preset B no longer silently falls back: compare selections are now persisted in local browser storage, restored across effects refreshes, and B shows an explicit empty placeholder instead of visually snapping to the first preset when nothing is saved yet

## 0.4.156 (2026-04-16)
- Re-arm and relink the `pw-record` peak-monitor path after EasyEffects graph changes, so preset loads, output-extras updates, and other DSP mutations actively rebuild the peak capture wiring instead of leaving `pw-record` alive but detached after a filter switch

## 0.4.155 (2026-04-16)
- Added a compact Output extras headroom helper (`Headroom (-3 dB)`) next to the protection limiter, wired through the backend so new presets and global extras application can stamp a fixed `-3 dB` output-gain reduction onto the managed output chain
- Tightened the Output extras layout so limiter + headroom share one row and the delay controls use smaller inline L/R ms inputs for a denser single-row presentation without adding vertical space

## 0.4.154 (2026-04-15)
- Diagnostic MPV -> Spotify handoff change: when Spotify takes over, FXRoute now stops MPV playback instead of only pausing it, so the old MPV stream is removed from the PipeWire/EasyEffects graph before Spotify starts. This is intended to test the hypothesis that paused MPV was still keeping the previous samplerate context alive during Spotify handoff.

## 0.4.153 (2026-04-15)
- Added a shared source-transition lock around MPV/Spotify handoffs so local playback pause/broadcast, Spotify start/toggle, and local play requests no longer overlap each other; the Spotify-start path also now waits a short moment after pausing MPV before continuing, to make PipeWire/EasyEffects samplerate renegotiation less race-prone during MPV -> Spotify transitions

## 0.4.152 (2026-04-15)
- Fixed a footer ownership regression where pausing Spotify could let a previously paused local Library/Radio track reclaim the footer and show its stale title; when Spotify already owns the footer and transitions to paused, paused local playback no longer steals footer context back immediately

## 0.4.151 (2026-04-15)
- Fixed a detached peak-capture state where `pw-record` could stay running but lose its PipeWire links after source/track changes, leaving clipping detection dead even though the capture process still existed; peak-monitor restarts are now keyed to playback context changes so the capture path is rebuilt when the active player/track changes

## 0.4.150 (2026-04-15)
- Switched local Library playback back onto the same peak-monitor lifecycle as Radio and Spotify for diagnosis, removing the temporary Library-specific suspension so `pw-record` can appear for all three sources while we trace the remaining global samplerate drift/upscaling behavior

## 0.4.149 (2026-04-15)
- Fixed a Spotify-specific peak-monitor race where the normal MPV/player callback immediately stopped `pw-record` again with `Stopping peak monitor while playback is inactive` right after Spotify had started it; the player-side stop path now first checks whether Spotify is actively playing before tearing peak capture down

## 0.4.148 (2026-04-15)
- Fixed two peak-monitor lifecycle bugs behind the flaky `pw-record` behavior: stop now clears stale availability/target state so the UI/API no longer claim peak capture is still present after it vanished from the graph, and peak start/stop transitions are now serialized through a shared async lock so rapid overlapping playback/Spotify callbacks stop double-arming and triple-suspending the monitor

## 0.4.147 (2026-04-15)
- Wired peak-monitor lifecycle into Spotify state changes: local Library playback still keeps peak capture suspended to protect samplerate switching, but active Spotify playback now explicitly starts the `pw-record` peak path instead of silently missing it because only MPV/player callbacks previously drove peak-monitor arming

## 0.4.146 (2026-04-15)
- Broke the peak-vs-samplerate loop by no longer running the `pw-record` peak monitor continuously: it is now initialized idle, only armed during active playback, explicitly suspended during active local Library playback, and stopped again when playback goes inactive so local high-rate playback keeps ownership of the PipeWire graph instead of being pinned back to 48 kHz

## 0.4.145 (2026-04-15)
- Re-calibrated the footer peak-warning threshold back to strict full-scale `1.0` after a controlled `-12 dBFS` tone plus EasyEffects clipping test showed the live `ee_soe_output_level` path reaching about `1.0115`, confirming true clip detection works and that the earlier low reading came from test conditions, not a dead monitor path

## 0.4.144 (2026-04-15)
- Relaxed the footer peak-warning threshold from strict full-scale `1.0` to pragmatic near-full-scale `0.8`, after confirming the current `ee_soe_output_level` capture path is alive and linked but tops out around `0.81` in real playback, so the warning can become useful again without touching the now-working samplerate switching

## 0.4.143 (2026-04-15)
- Fixed Spotify status logic so the Spotify tab refreshes and keeps polling while the tab is visible, instead of only polling when Spotify owned the footer, which could leave the tab stuck on stale states like “Spotify is not running.” after switching to local sources

## 0.4.142 (2026-04-15)
- Hardened the `pw-record` peak-monitor restart path with a short settle delay after stop, cleanup of stale process/task references, and earlier detection/reporting when the capture process dies before its ports fully appear, to prevent sample-rate/playback transitions from leaving the clip indicator dead

## 0.4.141 (2026-04-15)
- Hardened peak-monitor target discovery by replacing fragile `pw-dump` JSON parsing with `pw-cli ls Node` parsing, and made monitor restart/stop tolerate transient discovery errors instead of leaving the footer stuck with a dead capture process

## 0.4.140 (2026-04-15)
- Cut footer peak-warning hold time to 100 ms for debugging, to distinguish plain hold-time tuning from any other lag source keeping the warning visible too long

## 0.4.139 (2026-04-15)
- Reduced footer peak-warning hold time further from 1.0 s to 0.5 s so the warning tracks EasyEffects clipping more tightly and feels less sticky

## 0.4.138 (2026-04-15)
- Reduced footer peak-warning hold time from 2.0 s to 1.0 s so the warning follows EasyEffects' clipping activity more naturally instead of feeling too sticky

## 0.4.137 (2026-04-15)
- Re-arm the peak monitor on actual playback start so the explicit PipeWire capture/link path is rebuilt under active signal, avoiding the idle-start case where the app-owned peak capture could stay unlinked even though manual live probes worked

## 0.4.136 (2026-04-15)
- Relaxed peak-warning latch behavior now that the PipeWire capture path is correct: hold time increased to 2.0 s and consecutive-hit requirement reduced to 1, so real clipping remains visible in the footer instead of disappearing too quickly

## 0.4.135 (2026-04-15)
- Fixed manual peak-capture port discovery to read actual PipeWire port aliases from `pw-cli ls Port`, so `fxroute_peak_capture:input_FL/FR` can be linked reliably instead of being missed by the previous `pw-link -lI` scan

## 0.4.134 (2026-04-15)
- Fixed the new explicit PipeWire peak-capture wiring to use the actual unconnected `pw-record:input_FL/FR` ports, after the first manual-link attempt accidentally forced the capture node into the wrong media class and hid the expected stereo inputs

## 0.4.133 (2026-04-15)
- Changed peak monitoring to use an unconnected PipeWire capture node with explicit `pw-link` wiring into `ee_soe_output_level`, avoiding both default-input fallback and hardware-monitor samplerate pinning

## 0.4.132 (2026-04-15)
- Reworked peak monitoring to capture from EasyEffects' internal `ee_soe_output_level` PipeWire node via `pw-record` instead of the hardware monitor path, so peak warning can run again without pinning the DAC samplerate during local high-rate playback

## 0.4.131 (2026-04-15)
- Fixed samplerate reporting priority to prefer the running default hardware sink over `easyeffects_sink`, so the footer reflects the real DAC rate when EasyEffects and the final output differ, including high-rate edge cases like 768 kHz

## 0.4.130 (2026-04-15)
- Cleaned up peak-warning state during local playback suspension: the UI now gets an explicit unavailable/suspended peak state instead of a stale `parec exited -15` error after the monitor is stopped to protect high-rate playback

## 0.4.129 (2026-04-15)
- Suspended the output peak monitor during local Library playback, because the monitor capture path was pinning the PipeWire graph rate and preventing true high-rate playback from reaching the DAC
- Local high-rate playback now takes priority over peak monitoring; peak warning resumes automatically when local playback ends

## 0.4.128 (2026-04-15)
- Fixed the peak-monitor samplerate pin properly: the monitor now uses `parec --fix-rate` so it follows the connected sink rate instead of forcing 48 kHz before or PulseAudio's 44.1 kHz default afterward

## 0.4.127 (2026-04-15)
- Samplerate backend now prefers the relevant active running sink, especially `easyeffects_sink`, instead of always reading the default hardware sink, so the UI reflects the active processing path during playback

## 0.4.126 (2026-04-15)
- Removed the fixed `--rate=48000` constraint from the peak monitor `parec` capture path so the monitor no longer pins PipeWire to 48 kHz and the samplerate display can reflect the real active graph rate again

## 0.4.125 (2026-04-15)
- Fixed footer fallback after a finished Library track: an ended local track now keeps local footer ownership instead of restoring stale paused Spotify metadata
- Local ended playback is now treated as a valid footer context until a genuinely new source takes over

## 0.4.124 (2026-04-15)
- Hardened frontend WebSocket lifecycle: prevent duplicate concurrent connections, ignore stale socket callbacks, and serialize reconnect timers so old disconnect events cannot trigger extra reconnect churn
- WebSocket disconnect logs now include close code, reason, and cleanliness to make future disconnects diagnosable without re-enabling heavy debug instrumentation

## 0.4.123 (2026-04-15)
- Footer debug logging is now disabled by default and only activates when `localStorage['fx-debug-footer'] = '1'`, reducing the chance that the temporary instrumentation build destabilizes the browser tab during testing

## 0.4.122 (2026-04-15)
- Added targeted footer ownership debug logging in `static/app.js` to trace when the footer switches between `spotify` and `local`, and which WebSocket or Spotify update triggered the change
- This build is for diagnosis of the remaining stale-local-override bug during Spotify transitions

## 0.4.121 (2026-04-15)
- **Spotify-playing now outranks paused local playback for footer ownership**: A paused Radio/Library track with stale metadata no longer steals the footer back while Spotify is actively playing
- **Footer ownership sync no longer downgrades live Spotify to paused on MPV pause broadcasts**: This prevents the exact state split where the Spotify tab is correct but the footer falls back to the previous Radio/Library song

## 0.4.120 (2026-04-15)
- **MPV pause now broadcasts on Spotify takeover**: Starting Spotify now also broadcasts the newly paused MPV playback state, so clients stop treating the old Radio/Library session as active while Spotify is already playing
- **Source truth aligned on Spotify activation**: Spotify start/toggle paths now push both sides of the source switch, reducing stale previous-track footer data during Radio/Library → Spotify transitions

## 0.4.119 (2026-04-15)
- **Fixed Spotify command self-invalidation race**: Starting the Spotify poll too early was bumping the poll generation before the initiating client's own command response arrived, causing that fresh Spotify state to be discarded and leaving the footer stuck on the previous source
- **Spotify takeover sequencing corrected**: Spotify commands now arm takeover immediately but only start polling after the command response has been accepted, so the initiating client can adopt the new Spotify footer state deterministically

## 0.4.118 (2026-04-15)
- **Spotify takeover made more deterministic**: Starting Spotify now arms a short takeover window plus a burst of follow-up status refreshes so the footer can switch even when the first Spotify status response arrives late or stale
- **Local takeover clears pending Spotify takeover**: Switching back to Radio/Library now cancels any in-flight Spotify takeover window immediately, preventing the footer from snapping back to Spotify during rapid source changes

## 0.4.117 (2026-04-15)
- **Initiating-client footer refresh tightened**: Local Radio/Library playback now immediately downgrades any stale in-memory `Spotify: Playing` state on the initiating client so the local footer can repaint without waiting for a later Spotify update
- **Active card highlights aligned with source**: When Spotify owns the footer, stale local station/track highlights are cleared instead of lingering from the previous MPV state

## 0.4.116 (2026-04-15)
- **Hotfix for local playback 500 error**: Repaired a server-side regression in the Spotify pause-broadcast helper so `/api/play` works normally again while keeping the new Spotify state broadcast on local takeover

## 0.4.115 (2026-04-15)
- **Spotify pause state now broadcasts on local takeover**: Switching to Radio/Library playback now actively broadcasts the updated paused Spotify state to all clients instead of leaving stale `Spotify: Playing` state cached client-side
- **Local replay path aligned**: MPV replay/toggle flows now also pause-and-broadcast Spotify before resuming local playback, keeping footer ownership decisions consistent after source transitions

## 0.4.114 (2026-04-15)
- **Footer ownership centralized**: Replaced the drifting Spotify/local guard checks with a single priority rule: active Spotify playback wins, otherwise active local/radio playback wins, otherwise paused Spotify can keep the footer
- **Spotify takeover made deterministic**: Footer rendering and polling now reconcile against the same shared source-priority logic instead of multiple conflicting ad-hoc guards

## 0.4.113 (2026-04-15)
- **Footer source model corrected**: Spotify takeover now clears stale local footer/playback remnants instead of letting old local state keep blocking legitimate Spotify ownership
- **Spotify adoption logic rebalanced**: Active Spotify playback can claim the footer again, while stale paused Spotify data still stays blocked when authoritative local/radio playback owns the footer

## 0.4.112 (2026-04-15)
- **Footer ownership race tightened again**: Local/radio playback now force-stops Spotify footer polling directly inside the main footer render path, preventing oscillation between old Spotify state and current local state on remote clients
- **Local footer ownership now requires active playback**: Local takeover logic now keys off active local/radio playback state instead of merely seeing an old stored local track reference

## 0.4.111 (2026-04-15)
- **Spotify footer race hardened further**: Stale Spotify responses are now blocked from reclaiming footer ownership or repainting the footer when authoritative local/radio playback is already active
- **Spotify poll invalidation on local takeover**: When local playback regains footer ownership, the active Spotify poll generation is invalidated and stopped immediately to prevent late stale updates from repainting old Spotify song data

## 0.4.110 (2026-04-15)
- **Cross-client footer source sync fixed**: Remote clients now explicitly relinquish Spotify footer ownership as soon as authoritative local/radio playback state arrives, preventing stuck Spotify footers after Spotify → Library/Radio transitions
- **Playback ownership sync hardened**: Initial load, websocket playback updates, and status refreshes now all reconcile footer ownership from shared playback truth instead of leaving stale client-local Spotify state behind

## 0.4.109 (2026-04-15)
- **Peak monitor moved to final post-effects output**: The warning now watches the real hardware output monitor after the EasyEffects chain instead of the pre-effects `easyeffects_sink.monitor` source
- **Real over threshold restored**: With the corrected tap point, the footer warning can again use a true full-scale over threshold instead of workaround tuning against the wrong monitor source

## 0.4.108 (2026-04-15)
- **Peak warning confirmation added**: The footer warning now requires two consecutive over-threshold hits before latching, reducing brief false positives and better matching the EasyEffects meter behavior

## 0.4.107 (2026-04-15)
- **Peak warning sensitivity trimmed slightly**: Raised the practical post-effects warning threshold from `0.80` to `0.82` as a small tuning step after the footer sync fix made the live behavior easier to judge

## 0.4.106 (2026-04-15)
- **Peak footer sync hardened**: Added a lightweight status poll fallback so the footer warning state stays in sync even when the WebSocket delivery order or client state gets out of step
- **Radio footer refresh tightened**: Periodic status refresh now also reinforces local footer ownership and peak-warning state while radio playback is active

## 0.4.105 (2026-04-15)
- **Peak warning retuned**: Lowered the practical post-effects warning threshold to `0.80` after live measurement showed the restored `0.86` threshold missed real clipping cases on the active monitor path

## 0.4.104 (2026-04-15)
- **Peak warning threshold restored**: Rolled the practical post-effects warning threshold back to the more stable value after real comparison against EasyEffects showed rare false positives at 0 dB

## 0.4.103 (2026-04-15)
- **Peak warning sensitivity nudged earlier**: Lowered the post-effects warning threshold slightly so the footer catches near-clip conditions a touch sooner without reintroducing the sticky warning behavior

## 0.4.102 (2026-04-15)
- **Peak warning debounced**: Raised the live post-effects warning threshold again and shortened the hold window so the footer no longer sticks on `PEAK` during ordinary hot material
- **Footer warning timing refined**: The `PEAK` indicator now clears much faster once the signal falls back below the practical warning range

## 0.4.101 (2026-04-15)
- **Peak warning threshold corrected**: The post-EasyEffects footer warning now triggers at a practical live-monitor threshold instead of waiting for unreachable `1.0` full-scale samples on this PipeWire path
- **Live tuning from real signal**: Threshold adjusted after measuring the actual `easyeffects_sink.monitor` output during audible clipping tests

## 0.4.100 (2026-04-15)
- **Paused Spotify footer race fixed**: A paused Spotify session with stale metadata no longer steals footer ownership from an already active local/radio stream on reload
- **Startup footer ownership tightened**: Spotify footer adoption now respects active local playback instead of blindly claiming the footer during boot

## 0.4.99 (2026-04-15)
- **Footer reload ownership fix**: Spotify no longer steals the footer on startup from stale metadata when local radio playback is already active
- **Playback status boot sync**: Initial `/api/status` local playback state now explicitly restores local footer ownership after reloads

## 0.4.98 (2026-04-15)
- **Peak monitor target corrected**: The post-effects warning path now taps the real `easyeffects_sink.monitor` source instead of the sink node itself
- **Footer warning simplified**: Peak detection now replaces the green footer wave with a compact `PEAK` warning pill during the hold window
- **Asset/version bump**: Updated post-effects peak detection and bumped frontend assets to `0.4.98`

## 0.4.97 (2026-04-15)
- **Post-EasyEffects peak warning added**: Added a PipeWire-based monitor that watches the final EasyEffects output/monitor path for real full-scale peaks and exposes a held warning state to the footer
- **Footer warning badge**: Added a compact `Peak` badge that stays hidden normally and appears briefly after a real detected post-effects peak
- **Asset/version bump**: Updated backend/frontend peak warning wiring and bumped frontend assets to `0.4.97`

## 0.4.96 (2026-04-15)
- **Library import auto-close**: The Library import panel now closes automatically when leaving the Library tab, which avoids stale open UI when switching sections
- **Asset/version bump**: Updated tab-switch behavior and bumped frontend assets to `0.4.96`

## 0.4.95 (2026-04-15)
- **Effects import wording fixed**: The preset import dropzone no longer incorrectly says `IR`; it now uses the broader `Drop file here or browse` wording to match `.irs`, `.wav`, and `.txt` support
- **Effects dropzone styling aligned**: The Effects import dropzone now uses the same stronger primary text styling as the other upload cards

## 0.4.94 (2026-04-15)
- **Station dropzone alignment matched**: The station add dropzone now uses the same text geometry as the Library import cards, so the helper line sits on the same visual baseline

## 0.4.93 (2026-04-15)
- **Station dropzone styling aligned**: The station add dropzone now matches the Library import cards, with stronger white primary action text and lighter helper copy

## 0.4.92 (2026-04-15)
- **Import card primary emphasis matched**: The main action line on both Library import cards now uses the same color and weight so the pair reads symmetrically

## 0.4.91 (2026-04-15)
- **Import card typography normalized**: Helper line sizing, color, and line-height were tightened so the two Library import cards feel more visually homogeneous

## 0.4.90 (2026-04-15)
- **Library import layout corrected**: Reverted the overbuilt three-line import card hierarchy to a compact matched two-line structure
- **Asset/version bump**: Tightened Library import card copy and bumped frontend assets to `0.4.90`

## 0.4.89 (2026-04-15)
- **Library import hierarchy tightened**: URL and file import cards now use matching helper text roles, with technical details on a separate lighter line
- **Asset/version bump**: Updated compact import-card hierarchy and bumped frontend assets to `0.4.89`

## 0.4.86 (2026-04-15)
- **Version snapshot**: Captured the current polished FXRoute state as a stable checkpoint for further bug hunting
- **Deploy workflow added**: New `deploy.sh` now syncs the whole project root in one pass and verifies the remote version/assets after upload
- **Radio and library UI refined**: Station cards, helper copy, search placeholder, empty states, and action/button states were tightened to feel more concise and finished
- **DSP copy updated**: DSP subtitle now reflects filter import, not just impulse responses

## 0.4.84 (2026-04-15)
- **Library import copy tightened**: Replaced chatty helper text with short, direct UI labels for URL import and audio upload
- **Asset/version bump**: Updated concise import copy and bumped frontend assets to `0.4.84`

## 0.4.82 (2026-04-15)
- **Radio manager copy tightened**: Shortened the station-add helper text so it says only what matters, with no misleading "only when needed" wording for generic streams
- **Asset/version bump**: Updated concise UI copy and bumped frontend assets to `0.4.82`

## 0.4.81 (2026-04-15)
- **UI polish pass**: Empty states and helper texts were clarified so missing stations, empty libraries, and URL import hints feel more intentional
- **Safer button states**: Station add/delete actions now stay disabled until they are actually valid, which reduces ambiguous clicks in the Manage Stations flow
- **Small bug cleanup**: Library import panel now hides the correct selection toolbar, and upload/download UI code is null-safe against removed legacy buttons
- **Asset/version bump**: Updated frontend polish and bumped frontend assets to `0.4.81`

## 0.4.80 (2026-04-14)
- **Radio cards simplified**: Removed the redundant per-card `Radio` label so the station grid reads cleaner and less repetitive
- **Artwork scaling improved**: Station artwork now gets more square, calmer cards with `contain`-style fitting so logos and covers feel less awkwardly cropped
- **Card proportions refined**: Tightened padding, spacing, and responsive sizing so the station grid looks more balanced on desktop and mobile
- **Asset/version bump**: Updated radio card presentation and bumped frontend assets to `0.4.80`

## 0.4.79 (2026-04-14)
- **SomaFM link detection expanded**: Soma slug extraction now also understands SomaFM page URLs and image/logo URLs such as `/logos/512/u80s512.png`, which improves drop/paste recognition for real-world SomaFM browsing flows
- **Version bump**: Backend/app updated to `0.4.79`

## 0.4.78 (2026-04-14)
- **Manage Stations add flow reduced further**: Removed the redundant station URL row so the radio manager now uses the station URL tile as the single input surface
- **Conditional name prompt**: Generic streams reveal the station-name field only after a non-SomaFM URL is detected, while SomaFM URLs are added immediately without extra form steps
- **Asset/version bump**: Updated station-management UX and bumped frontend assets to `0.4.78`

## 0.4.77 (2026-04-14)
- **Library import made symmetrical**: URL import and file upload now use two equally sized import tiles, so the library import panel no longer feels lopsided
- **URL tile now supports paste directly**: The URL tile accepts right-click paste / Ctrl+V inside the tile itself, while dropped links still import immediately
- **Browse stays first-class**: File import keeps the full browse-capable upload tile so the common file-picker path remains prominent
- **Asset/version bump**: Updated library import layout and bumped frontend assets to `0.4.77`

## 0.4.76 (2026-04-14)
- **Library URL import simplified**: Removed the duplicate old text-entry row so the large URL dropzone is now the single primary import path for URL-based downloads
- **Dropzone now acts directly**: Dropping a link imports immediately, clicking the dropzone pastes from clipboard and can start the import without the extra form row
- **Asset/version bump**: Updated library import flow and bumped frontend assets to `0.4.76`

## 0.4.75 (2026-04-14)
- **Station add flow simplified**: Manage Stations now has a clear dedicated drop/paste URL zone for station links, with stronger SomaFM guidance directly in the modal
- **SomaFM name auto-fill backend**: Adding a SomaFM URL no longer requires a manual station name, the backend derives a proper display name automatically and still auto-fetches artwork
- **Generic stream handling kept explicit**: Non-SomaFM streams still require a manual station name so arbitrary stations stay user-controlled
- **Asset/version bump**: Updated station-management flow and bumped assets to `0.4.75`

## 0.4.74 (2026-04-14)
- **Manage stations modal polished**: Reworked the station manager into clearer Add/Delete sections, tightened spacing, improved hierarchy, and made the destructive area read more intentionally
- **Radio card typography refined**: Station titles and metadata now read a bit cleaner and more deliberate in the grid
- **URL dropzone made more obvious**: The library URL import drop area now has stronger visual treatment so it reads as an actual drop target instead of disappearing into the form
- **Asset/version bump**: Updated frontend UI polish and bumped assets to `0.4.74`

## 0.4.73 (2026-04-14)
- **URL import made more uniform**: Library URL import now gets a drag-and-drop/paste dropzone plus a dedicated Paste button, so it feels consistent with the existing upload-based import flows
- **Import UX polish**: Dropped or pasted URLs populate the field directly, Enter triggers import, and the helper text resets once the download starts
- **Asset/version bump**: Updated library import UI and bumped frontend assets to `0.4.73`

## 0.4.72 (2026-04-14)
- **Station art presentation refined**: Real station artwork now renders with cleaner cover-style cropping and subtle panel treatment, while generated fallback tiles keep their contained layout for better visual consistency
- **Asset/version bump**: Updated radio station card rendering/styles and bumped frontend assets to `0.4.72`

## 0.4.71 (2026-04-14)
- **Radio fallback artwork refined**: Generic non-SomaFM station tiles now use a cleaner premium-looking fallback with stronger gradients, subtle radio-wave motif, and a clearer genre chip instead of the rough placeholder feel
- **Asset/version bump**: Frontend fallback art updated in `app.js`, version bumped to `0.4.71`

## 0.4.70 (2026-04-14)
- **Automatic SomaFM station artwork**: Manually added/edited SomaFM stations now auto-detect their channel slug and use local station art when available, otherwise import artwork from SomaFM into local `static/station-art/`
- **Backfill for existing stations**: Stations missing artwork are now backfilled on load, so existing manually added SomaFM entries can pick up art without being recreated
- **Version bump**: Backend/app updated to `0.4.70`

## 0.4.69 (2026-04-14)
- **Static path handling hardened**: Root icon/manifest routes and the static mount now use absolute paths derived from `main.py`, avoiding transient/favicon errors caused by working-directory differences
- **Version bump**: Backend/app updated to `0.4.69`

## 0.4.68 (2026-04-14)
- **Root favicon aliases added**: Added FastAPI routes for `/favicon.ico`, `/apple-touch-icon.png`, and `/site.webmanifest` so browsers requesting root-level icon paths no longer get 404s
- **Version bump**: Backend/app updated to `0.4.68`

## 0.4.67 (2026-04-14)
- **FXRoute favicon added**: Generated a dedicated FX monogram favicon set for browser tabs, app icons, and shortcuts
- **PWA icon metadata added**: Added `site.webmanifest`, theme color, and linked favicon assets in `index.html`
- **Asset/version bump**: `index.html` now references favicon assets and `style.css?v=0.4.67`

## 0.4.66 (2026-04-14)
- **Playback bar spacing refined**: Lifted the fixed bottom playback bar a few pixels off the viewport edge and gave it rounded outer corners so it no longer feels glued to the screen border
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.66`

## 0.4.65 (2026-04-14)
- **Monogram header lockup refined**: The temporary FXRoute header branding now uses a more deliberate monogram-tile + wordmark treatment with stronger contrast, cleaner weight distribution, and a simpler mobile collapse
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.65`

## 0.4.64 (2026-04-14)
- **Header branding simplified temporarily**: Replaced the weak draft-logo header placement with a stronger text-first FXRoute lockup (`FX` mark + `FXRoute` wordmark + `local audio control`) for better readability in normal app use
- **Branding next step**: A dedicated designer pass is the recommended follow-up for proper long-term FXRoute header/logo variants
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.64`

## 0.4.63 (2026-04-14)
- **Header logo sizing fixed**: Cropped the transparent logo asset to its real visible bounds and increased header logo sizing so the FXRoute mark reads clearly in normal app use
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.63`

## 0.4.62 (2026-04-14)
- **Header logo transparency fix**: Replaced the temporary white-background JPEG with a transparency-preserving PNG derived from the provided logo asset
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.62`

## 0.4.61 (2026-04-14)
- **Header logo added**: The provided FXRoute logo is now used in the main header as the primary brand mark
- **Responsive brand lockup styling**: Header logo sizing now adapts for desktop and mobile without crowding the connection badge
- **Asset/version bump**: `index.html` now references `style.css?v=0.4.61`

## 0.4.60 (2026-04-14)
- **Full app rebrand to FXRoute**: Updated visible frontend branding, browser title, service descriptions, comments, and project docs from the old Audio Mini-PC naming to FXRoute
- **Compatibility note**: Technical identifiers such as the project directory, deployment path, cache path, and `audio-pc` service/unit name remain unchanged for compatibility and to avoid breaking existing installs/scripts
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.60` and `style.css?v=0.4.60`

## 0.4.59 (2026-04-14)
- **Library shuffle availability corrected**: The shared Library shuffle button is now only enabled when a real local queue with more than one track is active, preventing confusing `409 Conflict` responses when shuffle is pressed during single-track playback
- **Library mode button affordance improved**: Shuffle/loop buttons now expose clearer availability tooltips based on the active playback context
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.59` and `style.css?v=0.4.59`

## 0.4.58 (2026-04-14)
- **Library shuffle/loop switched back to shared playback state**: The Library buttons now call new backend playback-mode endpoints instead of acting as client-local preselection, so mode changes broadcast immediately across all clients and stay aligned with the actual shared player state
- **Shared queue-mode actions added**: Backend now exposes `/api/playback/shuffle` and `/api/playback/loop`, supports mutating the active local playback context directly, keeps shuffle/loop mutually exclusive, and shuffles the remaining queue while preserving already-played/current context
- **Library mode buttons now reflect only actual shared local playback**: The frontend derives button state from the synced playback payload, disables the buttons when local playback is not active, and resyncs Library selection/modes together from the active playback context
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.58` and `style.css?v=0.4.58`

## 0.4.57 (2026-04-14)
- **Library playback-context ownership tightened**: The frontend now tracks a stable signature of the active local playback context and only resyncs Library selection/shuffle/loop when that context actually changes, avoiding stale selection + foreign mode combinations after device switching while also avoiding constant overwrite during ordinary playback updates
- **Library UI now resyncs as a bundle**: On real playback-context changes the Library selection, shuffle, and loop are updated together from backend state, and clearing the queue also clears local selection so the UI does not keep an orphaned queue selection around
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.57` and `style.css?v=0.4.57`

## 0.4.56 (2026-04-14)
- **Library selection queue hardened across reconnects**: The frontend now keeps selected track ids more defensively instead of dropping them during transient reload states, so clicking a selected track is less likely to silently degrade into single-track playback
- **Library mode adoption tightened further**: After a local play request, remote queue modes are only adopted back into the Library buttons when the server response actually represents an active queue or explicit loop/shuffle mode, preventing false unchecks from inert responses
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.56` and `style.css?v=0.4.56`

## 0.4.55 (2026-04-14)
- **Library mode preselection decoupled from passive remote updates**: Library shuffle/loop now stay client-local during normal WS/init/status churn and are only re-synced from server queue state after this client starts local playback or clears the queue, which should stop PC/mobile reconnects from wiping the toggles
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.55` and `style.css?v=0.4.55`

## 0.4.54 (2026-04-14)
- **Single-track local mode reset bug fixed**: Starting a new local playback with only one selected track now clears any stale prior multi-track queue state first, so loop/shuffle state reflects the new request instead of leaking from the previous queue
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.54` and `style.css?v=0.4.54`

## 0.4.53 (2026-04-14)
- **Single-track library loop added**: Local Library loop now also works when only one track is being played. In that case the current track is loaded again on end instead of loop only applying to multi-track queues
- **Queue payload loop flag unified**: Frontend loop state now reflects either queue-loop or single-track-loop through the same `queue.loop` flag
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.53` and `style.css?v=0.4.53`

## 0.4.52 (2026-04-14)
- **Removed redundant Library `Play selected` button**: Selection now simply defines the queue context, and clicking any selected track already starts playback from that selection, so the extra button was removed to reduce clutter and duplicated behavior
- **Library shuffle/loop reset logic corrected**: The new Library mode toggles no longer get cleared immediately just because there is not yet an active multi-track queue. Existing local single-track playback keeps the chosen mode toggles visible until a real queue/playback state supersedes them
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.52` and `style.css?v=0.4.52`

## 0.4.51 (2026-04-14)
- **Library shuffle/loop active-state strengthened again**: The toolbar toggle active state is now forced more explicitly inside the Library toolbar with `!important` styling so enabled modes are visually obvious instead of only barely tinted
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.51` and `style.css?v=0.4.51`

## 0.4.50 (2026-04-14)
- **Library mode-toggle visual fix**: Strengthened the active-state CSS selector for the new Library shuffle/loop toolbar buttons so they reliably render green when enabled
- **Cache-bust bump after stale 0.4.48 client**: `index.html` now references `app.js?v=0.4.50` and `style.css?v=0.4.50` to force clients off the older cached asset set

## 0.4.49 (2026-04-14)
- **Library shuffle/loop made mutually exclusive**: The new local Library toolbar toggles now follow the same simplified rule as Spotify, so enabling shuffle clears loop and enabling loop clears shuffle
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.49` and `style.css?v=0.4.49`

## 0.4.48 (2026-04-14)
- **Library shuffle/loop toggles added**: The Library toolbar now has compact shuffle (`🔀`) and loop (`🔁`) icon toggles so local track queues can be started with those modes without overloading the footer
- **Local queue metadata extended**: Local playback queue payloads now include `shuffle` and `loop` state, the frontend reflects those modes in the toolbar, shuffle randomizes the queued track order while keeping the chosen start track first, and loop restarts the local queue from the beginning when it reaches the end
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.48` and `style.css?v=0.4.48`

## 0.4.47 (2026-04-14)
- **Spotify loop button visual behavior simplified**: The loop button no longer changes its visible label between `Loop off` / `Loop playlist` / `Loop track`. It now behaves like shuffle visually, keeping a stable `Loop` label while active state still shows through styling and the exact mode remains available via tooltip/status line
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.47` and `style.css?v=0.4.47`

## 0.4.46 (2026-04-14)
- **Spotify footer left-side final trim**: Removed the extra grey `Spotify` label again, keeping only the small EQ/playing indicator on the left so the footer stays visually consistent without adding dead text
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.46` and `style.css?v=0.4.46`

## 0.4.45 (2026-04-14)
- **Spotify footer left-side indicator restored**: The earlier cleanup hid too much. When Spotify owns the footer, the left side now keeps the familiar small EQ/playing indicator and a neutral `Spotify` label, while duplicated track/artist metadata stays removed from that area
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.45` and `style.css?v=0.4.45`

## 0.4.44 (2026-04-14)
- **Spotify footer cleanup**: When Spotify owns the footer, the left-side title/artist block is now hidden so the track metadata is shown only once in the centered footer area instead of appearing duplicated
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.44` and `style.css?v=0.4.44`

## 0.4.43 (2026-04-14)
- **Spotify volume control wired into the shared footer slider**: When Spotify is the visible tab or active footer source, the global volume slider now talks to Spotify via a dedicated `/api/spotify/volume` backend path instead of incorrectly changing only the local MPV path
- **Spotify status now carries real volume**: `spotify.py` now reads `playerctl --player=spotify volume`, exposes a normalized `volume` value in Spotify status responses, and refreshes it after volume changes so the UI can stay in sync
- **Debounced Spotify volume updates**: Spotify volume changes use their own lightweight debounced sender and feed back into the same footer UI, keeping the existing volume UX but making it actually work for Spotify
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.43` and `style.css?v=0.4.43`

## 0.4.42 (2026-04-14)
- **Spotify shuffle/loop made mutually exclusive in the UI flow**: Turning shuffle on now first clears any active loop mode, and activating loop now first disables shuffle, so the controls match the intended simplified mental model instead of allowing conflicting combined states
- **No layout changes**: This is purely interaction logic on top of the clearer `0.4.41` Spotify controls
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.42` and `style.css?v=0.4.42`

## 0.4.41 (2026-04-14)
- **Spotify loop/shuffle clarity pass**: Secondary Spotify controls now use compact icon+label buttons instead of icon-only pills, so loop state is readable at a glance (`Loop off`, `Loop track`, `Loop playlist`) and shuffle is visually clearer without changing the overall layout
- **Status line now explains the modes**: The Spotify status line now shows playback state plus current shuffle/loop state, which makes it much easier to tell whether loop is actually off, looping the track, or looping the playlist
- **Accessibility/state hints improved**: Shuffle and loop buttons now expose pressed-state semantics so their active state is more explicit and consistent
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.41` and `style.css?v=0.4.41`

## 0.4.40 (2026-04-14)
- **Spotify command/state sync hardened**: Spotify transport, shuffle, loop, seek, WebSocket updates, and poll updates now flow through a shared `handleIncomingSpotifyState(...)` path so footer and Spotify tab consume the same fresh state instead of diverging
- **No more UI-only shuffle/loop illusions**: Shuffle and loop actions now return the backend-confirmed playerctl state immediately, and the backend also tags whether the real state actually changed, making the controls state-confirmed instead of just optimistic styling
- **Track transition refresh improved**: Natural track-end, manual next/previous, and seek-near-end transitions now refresh metadata, footer text, and progress from the latest Spotify status instead of letting stale pre-transition state linger
- **Seek-near-end recovery pass**: Spotify seek now performs an immediate refresh plus a short delayed follow-up refresh so auto-advance after late-track seeks is much more likely to land on the correct next-track metadata and progress
- **Footer ownership stays source-correct**: Incoming Spotify events and poll responses can refresh the footer when Spotify is the active source, while local playback paths still keep Spotify responses out when local owns the footer
- **Spotify shuffle/loop spacing polished**: Secondary Spotify controls now have slightly cleaner spacing and larger touch targets without changing the rest of the UI
- **Asset/version bump**: `index.html` now references `app.js?v=0.4.40` and `style.css?v=0.4.40`

## 0.4.38 (2026-04-14)
- **Volume curve improved**: The volume slider no longer sends the raw slider percentage linearly to MPV. It now maps slider position through a nonlinear curve (`Math.pow(x, 0.6)`) so low and mid positions rise more audibly while the top end gets finer control
- **Same UI, better feel**: Slider layout and range stay unchanged, but the displayed percentage now reflects slider position while backend/player volume uses the curved mapping
- **Asset version bump**: `index.html` now references `app.js?v=0.4.38` and `style.css?v=0.4.38`

## 0.4.37 (2026-04-14)
- **Cross-Client Spotify Footer Sync**: Spotify actions now broadcast a dedicated WebSocket `spotify` state to all connected clients, so remote devices immediately switch the footer to Spotify truth instead of staying on stale Radio/Library metadata
- **WebSocket init now includes Spotify state**: New clients receive current Spotify status during initial websocket handshake and can render the correct global footer source immediately
- **Footer source no longer depends on local tab**: On incoming WebSocket Spotify state, clients set `__footerSource = 'spotify'`, refresh `window.__spotifyLastData`, and re-render the footer from Spotify truth
- **Asset version bump**: `index.html` now references `app.js?v=0.4.37` and `style.css?v=0.4.37`

## 0.4.36 (2026-04-14)
- **Local Station Artwork**: Added local mirrored station artwork under `static/station-art/` for current SomaFM stations instead of relying on external hotlinked logos
- **Stable Radio Art Rendering**: Radio cards now resolve artwork from local files first and fall back to generated art only when needed, making the UI resilient against broken remote SomaFM image URLs
- **Centered Card Artwork Layout**: Station cards now use a taller artwork slot with `object-fit: contain`, so the real station art is visible with much less cropping
- **Asset Cache Busting**: Updated `index.html` asset query version to `0.4.36` so browsers pull the new radio-card image/render logic immediately
- **Documentation / Version Sync**: Project `VERSION` now aligned to `0.4.36`

## 0.4.35 (2026-04-14)
- **Footer Source-Authority Fix**: When Spotify is the active footer source, `updatePlaybackUI()` now does an early return after refreshing from Spotify truth — no local code path can overwrite the Spotify footer anymore
- **Removed broken source-sync block**: The previous source-sync in `updatePlaybackUI()` reset `__footerSource` to `'local'` whenever `current_track.source !== 'spotify'`, which was ALWAYS true since MPV tracks never have `source: 'spotify'`. This caused Spotify footer data to be immediately overwritten by stale local state
- **`updateGlobalControlsForSource()` added**: Was referenced but never defined — caused ReferenceError in `renderSpotify()`, `initSpotify()`, `switchTab()`, `spotifyCommand()`
- **Spotify poll generation guard**: `_spotifyPollGeneration` counter bumped when source switches away from Spotify, invalidating in-flight poll responses
- **`playRadio`/`playLocal`**: Set `__footerSource = 'local'` and bump `_spotifyPollGeneration` before optimistic UI update
- **`spotifyCommand()`**: Sets `__footerSource = 'spotify'` and restarts poll when Spotify starts playing; generation-gated to discard stale responses
- **`startSpotifyPoll()`**: Generation-gated — poll responses are dropped if the generation changed during the request
- **`initSpotify()`**: Only starts Spotify poll if `__footerSource === 'spotify'`

## 0.4.23 (2026-04-13)
- **Bass Enhancer in Extras Flow**: Added `bassEnabled` and `bassAmount` to `collectEffectsExtras()` — was missing from the helper, causing bass settings to be ignored on PEQ create, REW import, and IR import
- **Backend**: Added `bass_enabled`/`bass_amount` Form parameters to `/api/easyeffects/presets/create-with-ir` and `/api/easyeffects/presets/import-rew-peq`
- **UI Cleanup**: Removed redundant filter-type subtitle from PEQ band headers ("Band 1 Bell" → "Band 1")
- **PEQ Placeholder**: Changed from "PEQ preset name" to "New PEQ Preset"
- **Mobile Layout**: DSP tab now responsive — single column on mobile, 2 columns at 768px+; reduced padding/gaps; no horizontal overflow; inputs use font-size: 1rem to prevent iOS zoom
- **collectEffectsExtras Fix**: Restored missing function that caused "collectEffectsExtras is not defined" error on PEQ preset creation
- **Import Text**: Shortened to "Upload preset files (.irs, .wav, .txt) - type auto-detected."

## 0.4.14 (2026-04-13)
- **Toggle A/B Button Fix**: Corrected toggle logic — was switching to wrong preset or same preset. Now correctly switches between dropdown A and B selections.
- **UI Layout Redesign**: "Active:" badge moved to own prominent centered row. Toggle and Delete buttons now sit side-by-side below the badge. Delete button no longer shares row with dropdowns.
- **Active Badge Styling**: New `.effects-compare-active-badge` class with larger font (1rem), bold weight, centered padding, and subtle background — makes the active preset clearly visible.
- **State Preservation Fix**: `compare` state (presetA/presetB/activeSide) now survives across WebSocket and `fetchEffects()` updates — was being reset to empty on each server push.
- **Null-Safety Cleanup**: Removed all legacy `effectsPresetSelect` references and added optional chaining (`?.`) to all `setupEffectsActions()` event listeners that reference potentially missing DOM elements.
- **Removed Duplicate Active Label**: Deleted legacy `effectsPresetStatus` element and made `renderEffectsPresetStatus()` a true no-op.

## 0.4.13
- Added A/B preset compare feature in "Manage presets" card: two preset dropdowns (A/B) + "Toggle A/B" button with live active preset indicator
- Compare state auto-updates when user switches presets via the normal dropdown
- Added dark scrollbar styling for all `select.url-input` and `.compare-select` dropdowns (dark track, subdued thumb, hover accent)

## 0.4.12
- homogenized Output extras card header alignment with other 3 cards (removed extra padding-top that was pushing the title lower)
- simplified delay checkbox styling: extracted from field-group wrapper into dedicated effects-delay-header div with min-height unset
- all 4 DSP card titles now sit at the same vertical offset from card top edge

## 0.4.11
- removed "Load after create" checkbox from Create PEQ card (auto-load now default for all preset types)
- simplified Create PEQ card: removed long explanatory text in header and body, replaced with "Parametric EQ with stereo-linked bands."
- removed second explanatory note below preset name input
- removed effectsImportSubmitBtn (no longer needed, IR upload triggers automatically on file select)
- added effectsImportInFlight guard to prevent double-POST on IR upload
- added peqCreateInFlight guard to prevent double-click on PEQ preset creation
- all event listener registrations for PEQ and effects controls now use optional chaining (?.) to handle missing DOM elements gracefully
- updatePeqBand now guards against undefined peqDraft state
- resetPeqDraft now initializes peqDraft if missing (null-safe)
- createPeqPreset now initializes peqDraft if missing before proceeding
- WebSocket 'easyeffects' handler now preserves peqDraft when updating state (was clobbering it)
- addPeqBand and removePeqBand now initialize peqDraft if undefined
- optimisticVolume variable was accidentally dropped during prior edit → restored to fix broken playback WebSocket updates

## 0.4.10
- fixed effectsImportSubmitBtn TypeError: button was removed from HTML but JS still referenced it, causing crashes on IR import
- fixed effectsStatus innerHTML error: status div missing from HTML → guarded all status writes with null-checks
- fixed IR import double-call: upload area callback fires twice on some browsers → added effectsImportInFlight guard
- fixed WebSocket 'easyeffects' clobbering peqDraft: broadcast now preserves state.easyeffects.peqDraft when updating
- fixed addPeqBand/removePeqBand crashing on mobile: both functions now initialize peqDraft if undefined
- fixed optimisticVolume ReferenceError: variable was dropped during a prior edit → playback state broken for all WebSocket volume updates

## 0.4.9
- added Bass Enhancer support: checkbox + amount (dB) in Output extras, saves to global_extras and injects into all presets
- fixed numeric input UX: change event only (no input), focus tracking prevents mid-edit overwrites, blur commits value
- fixed checkbox click target: checkbox-row width set to fit-content, no more inflated click area on delay enable
- unified Output extras card background with other cards (was rgba(255,255,255,0.01) vs var(--bg-surface))
- increased number input spin buttons to 26px height for easier clicking
- fixed Bass Enhancer value reset on disable: params are now preserved when bass is toggled off (like delay behavior)
- synced local workspace and Mini-PC to same source-of-truth state

## 0.4.8
- redesigned FXRoute preset management: instant dropdown switching (no Apply button), clear active/selected state display
- added preset deletion safety: Pure is protected from deletion, deleting the active preset auto-switches to Pure
- autosave for Output extras (limiter/delay) with debounced requests and inline Saving/Saved/Failed feedback
- removed empty status footer from FXRoute page, polished secondary text brightness and line-height
- synced docs and project files between workspace (.100/pbclaw) and Mini-PC (.64)

## 0.4.7
- moved Manage presets status block into the card, distinguishing active vs selected preset with Apply button disabled when they match
- added a lightweight queue clear control in the playback footer that removes only the temporary queue context while the current track keeps playing
- hid the clear action unless a queue is actually active, so the footer stays compact in normal playback
- deployed the queue-clear update to the Mini-PC and kept live user data files untouched during sync (`.env`, `playlists.json`, `stations.json`)

## 0.4.5
- rebuilt playlist handling after the earlier delete bug so playlist delete now removes only playlists instead of accidentally deleting tracks
- added persistent saved playlists via `playlists.json`, including save, load-to-selection, and delete actions in the Library tab
- kept playlists visually lightweight by rendering them at the top of the Library with a compact `📋` entry style and always-visible delete control
- extended radio station management so custom stations can be added and deleted directly from the UI and API alongside the built-in SomaFM set
- synced Mini-PC and local workspace back to the same source-of-truth state before continuing from this version

## 0.3.41
- added next/previous queue controls plus compact queue status in the playback bar, then simplified the UI again by removing the redundant queue list
- refined queue behavior so explicit playback changes replace the temporary queue instead of dragging old selection context along
- kept the selection/start flow lightweight so queue stays temporary and future persistence can move into playlists

## 0.3.40
- added library search plus clearer multi-select actions with a single select/clear toggle button
- added backend queue/autoplay for selected local tracks so chosen library items can continue automatically in library order
- hardened queue start UX with `Play selected` and made the main play button selection-aware when playback is stopped

## 0.3.12
- moved download/import into the Library tab so the extra top-level tab is freed up and import now sits next to a compact refresh control
- added library import options for both URL-based downloads and direct audio file upload into the music library
- introduced checkbox selection for library tracks plus bulk delete, laying the groundwork for later top-to-bottom selected playback flows

## 0.3.11
- added cache-busting version query strings for the main radio frontend assets so desktop browsers do not stay stuck on stale CSS or JS after UI changes

## 0.3.10
- refined the radio station management UX so station cards remain the visual focus and management reads as a secondary utility
- restyled the station cards with stronger depth, larger touch targets, and clearer active feedback
- reduced the management trigger to a compact ghost-style utility button and polished the dialog hierarchy for cleaner separation

## 0.3.9
- moved station management out of the inline radio layout into a compact overlay dialog so the station buttons stay visually dominant
- simplified station removal to a select-and-delete flow instead of a long inline action list
- kept station add in the same dialog with a small secondary trigger and touch-friendly modal layout for phone through desktop sizes

## 0.3.8
- simplified radio station management further by dropping edit controls from the UI and keeping the flow to add plus delete only
- increased separation between the main stations area and the management panel so station playback stays visually primary
- replaced the heavy station action wall with a slimmer scrollable saved-stations list and a cleaner single-action add form

## 0.3.7
- toned down the new station management UI so radio playback stays visually primary and management stays secondary
- moved station management behind a single toggle button, hid the panel by default, and compacted the edit/delete list to reduce scrolling
- simplified station manager rows to focus on station names and lighter actions instead of large always-open blocks

## 0.3.6
- replaced the fixed SomaFM-only station list with a persistent editable `stations.json` source that now supports add, edit, and delete operations
- added station management endpoints and radio-tab UI for validating and saving stream links, including SomaFM playlist URLs and direct stream URLs
- made the radio tab touch-friendlier with a dedicated manage section and added a generic `.hidden` helper so form actions hide correctly across the app

## 0.3.5
- synced the working Mini-PC hotfixes back into the main project copy so the main workspace stays the source of truth again
- added persistent `live_title` handling for radio playback, so ICY song titles no longer flash briefly and then fall back to the station name
- included radio metadata and `live_title` in the backend playback payload, so websocket-driven UI updates keep the currently playing song visible
- limited frontend metadata polling to websocket fallback periods instead of always polling alongside an active websocket

## 0.3.4
- separated the main play/pause button toggle flow from the dedicated `/api/pause` endpoint, so pause or resume no longer falls back to `loadfile(..., replace)` when nothing is currently loaded
- fixed mpv end-of-file state handling to wait for the real idle transition before clearing `current_file`, which prevents stale `end-file reason=stop` events from replace/reload operations from wiping the active stream state
- added an explicit `ended` playback flag so replay-after-end stays available without mixing it up with pause/resume or explicit stop

## 0.3.3
- fixed the mpv IPC command path so backend calls return as soon as mpv answers instead of stalling on the socket timeout, which should remove the big real-world lag on volume and play/pause
- stopped `/api/status` from querying stream metadata while nothing is loaded, so idle polling no longer burns extra IPC round-trips
- made websocket disconnect cleanup idempotent to reduce duplicate disconnect log spam when stale sockets are cleaned up from multiple paths
- merged backend playback state directly after play requests and stopped unrelated websocket playback events from clearing the play/pause in-flight lock

## 0.3.2
- kept the volume slider fully optimistic while scrubbing, then held backend volume sync briefly so websocket or status updates no longer yank the thumb backward mid-adjustment
- tightened volume send timing and skipped redundant backend volume writes so fast slider movement stays responsive without turning back into a request storm
- made play/pause clear its in-flight lock as soon as the HTTP response lands and return full playback state immediately for a snappier button response with cleaner rollback on failure

## 0.3.1
- fixed mpv async callback delivery so player state events now reach FastAPI from the mpv listener thread reliably
- removed duplicate backend volume websocket broadcasts and tightened frontend slider syncing to reduce slider-induced websocket churn
- kept the last selected track available for replay after end-of-track, so the main play button can start it again without choosing another item
- sent initial playback state on websocket connect and aligned status payloads so current_track stays consistent across reconnects and reloads

## 0.3.0
- replaced the remaining mpv polling approach with an event-driven mpv IPC listener for playback state updates
- now reacts to mpv property-change and end-file events instead of repeatedly polling player state
- reduced background churn and made the player state flow cleaner for slider interaction and track-end handling

## 0.2.11
- improved websocket disconnect logging so normal disconnects and real websocket errors are easier to distinguish during slider/debug work

## 0.2.10
- reduced backend/player polling load to avoid instability under rapid UI interaction
- serialized volume updates in the frontend so slider scrubbing no longer floods the backend with overlapping requests
- reduced slider-induced state churn by ignoring backend volume refreshes while a local volume change is in flight

## 0.2.9
- improved volume slider responsiveness by tightening frontend send timing and syncing volume updates more directly
- broadcast volume changes immediately from the backend so the UI stays in sync more reliably
- improved playback state tracking when a track finishes so ended media resets more cleanly
- hardened play/pause behavior to avoid bad resume toggles when nothing is currently loaded

## 0.2.8
- redesigned and polished the overall frontend UI for a more product-like, touch-friendly look
- improved header, tabs, station cards, library list, download area, and playback bar styling
- moved the Effects refresh action next to the preset selector and downgraded it to a quieter secondary action
- cleaned up and shortened user-facing copy for more consistent sentence-case English
- aligned remaining status and error strings in the frontend for a smoother UI tone

## 0.2.7
- reworked Effects tab spacing and section separation with stronger two-card structure
- unified card heading scale and improved create/manage visual distinction

## 0.2.6
- increased Effects tab spacing, padding, and form control sizing for a less cramped layout
- improved desktop card proportions and clearer visual separation

## 0.2.5
- polished Effects tab layout with centered content column and cleaner button sizing
- improved card spacing and visual separation for create/manage sections

## 0.2.4
- improved Effects tab spacing, grouping, and button layout
- updated stale status text to reflect automatic wav conversion

## 0.2.3
- aligned generated convolver preset JSON with working EasyEffects convolver structure
- removed mismatched custom fields like mix and stereo-width

## 0.2.2
- fixed ffmpeg IR conversion by forcing wav output format for .irs targets
- improved effects create flow with auto-filled preset name from uploaded file
- slightly improved effects action layout

## 0.2.1
- convert uploaded wav IR files to EasyEffects-compatible .irs using ffmpeg
- keep direct .irs uploads supported

## 0.2.0
- added EasyEffects preset listing and preset switching
- added IR listing, IR upload, and simple convolver preset creation
- simplified effects flow toward upload + preset creation in one step
- added preset deletion
- fixed missing multipart dependency in requirements

## 0.1.1
- fixed play/pause button state handling
- fixed source-switch autoplay/resume logic
- improved optimistic playback UI responsiveness

## 0.1.0
- initial FXRoute implementation

## 0.4.35 (2026-04-14)
- **Spotify Integration**: Full Spotify control tab via playerctl/MPRIS — play/pause, previous/next, shuffle, loop, seek slider, cover art, track info
- **Source-Agnostic Architecture**: Three-concept source model: `__visibleTab` (UI tab), `__footerSource` (footer display), controls route by visible tab
- **Source Exclusivity**: Backend-level mutual exclusion — Spotify and MPV can't play simultaneously. Playerctl pause for Spotify (no API cooldown), `set_pause(True)` for MPV, explicit `set_pause(False)` after `loadfile` to fix paused-after-Spotify bug
- **Footer Stability**: `updatePlaybackUI()` guards footer writes when Spotify is footer source. Spotify poll runs always, centralized `updateFooterForSpotify()` for Spotify footer state
- **Autoplay Fix**: `playRadio`/`playLocal` cancel in-flight actions instead of blocking. WebSocket `playback` events skipped during `playbackActionInFlight` to prevent optimistic state overwrite
- **MPV Pause Fix**: `loadfile` doesn't reset MPV's pause property — added explicit `set_pause(False)` after loadfile when switching from Spotify
- **UI Polish**: Spotify tab between Radio and Library. Unavailable states styled as cards. Progress bar, shuffle/loop buttons with capability gating
