// Demo API routes: intercepts every fetch() the real FXRoute frontend makes
// and answers from the demo state. All mutations update the same state object
// the WebSocket simulation reads, so the UI behaves like a real backend.
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_ROUTES_LOADED) return;
    window.FXROUTE_DEMO_ROUTES_LOADED = true;

    const S = window.FXROUTE_DEMO_STATE;
    const lib = S.lib;
    const stations = S.stations;
    const catalogStations = S.catalogStations;
    const tidalAlbums = () => S.tidalAlbums();
    const tidalTracks = () => S.tidalTracks();
    const tidalArtists = () => S.tidalArtists();
    const tidalPlaylists = () => S.tidalPlaylists();

    // ── Favorites (TIDAL) ───────────────────────────────────────────────
    // Pre-filled generously so the browse surfaces look like a real library
    // instead of three lonely entries.
    const tidalFavs = {
        tracks: new Set(['t_album_01_t1', 't_album_01_t2', 't_album_02_t1', 't_album_03_t1', 't_album_04_t1', 't_album_06_t2', 't_album_08_t1', 't_album_11_t1', 't_album_13_t2', 't_album_16_t1', 't_album_18_t1', 't_album_20_t1']),
        albums: new Set(['t_album_01', 't_album_02', 't_album_04', 't_album_06', 't_album_08', 't_album_11', 't_album_13', 't_album_16', 't_album_18', 't_album_20']),
        artists: new Set(['t_artist_01', 't_artist_03', 't_artist_04', 't_artist_06', 't_artist_07', 't_artist_09', 't_artist_11', 't_artist_14', 't_artist_17', 't_artist_20']),
        playlists: new Set(['t_playlist_01', 't_playlist_03', 't_playlist_04', 't_playlist_06', 't_playlist_08', 't_playlist_10', 't_playlist_12', 't_playlist_15', 't_playlist_16', 't_playlist_20']),
    };

    // ── DSP state ───────────────────────────────────────────────────────
    // Mirrors the .104 preset stock: the empty Direct/Neutral chains, the
    // +3/+6 dB filter presets (real selectable presets, no headroom), the
    // PEQ/Combo chains and the four convolver kernels. Active preset and
    // limiter default match .104 (Conv LR MinAlign Harman 30-300Hz -7dB,
    // limiter on at -1 dB).
    function presetEntry(name, extra) {
        return { name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], ...(extra || {}) };
    }
    let dspPresets = [
        presetEntry('Direct'),
        presetEntry('Neutral'),
        presetEntry('+3'),
        presetEntry('+6'),
        presetEntry('Combo', { source_presets: ['+3', 'PEQ', 'Conv LR HybAlign BK 30-3000Hz -7dB'] }),
        presetEntry('Conv LR HybAlign BK 30-10000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 30-12000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 30-3000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR MinAlign Harman 30-300Hz -7dB', { convolver: true }),
        presetEntry('PEQ', { peq: { enabled: true, params: { channelMode: 'dual', eqMode: 'IIR', leftBands: [], rightBands: [] } } }),
    ];
    let dspActivePreset = 'Conv LR MinAlign Harman 30-300Hz -7dB';
    let dspExtras = {
        limiter: { enabled: true, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 20.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
        headroom: { enabled: false, params: { gainDb: -3 } },
        delay: { enabled: false, params: { leftMs: 0, rightMs: 0 } },
        bass_enhancer: { enabled: false, params: { amount: 0, harmonics: 8.5, scope: 100.0, blend: 0.0 } },
        autogain: { enabled: false, params: { targetDb: -18 } },
        loudness: { enabled: false, params: { strength: 2, fftSize: 16384, volumeDb: 0 } },
        tone_effect: { enabled: false, mode: 'crystalizer' },
    };
    let dspCompare = { presetA: 'Neutral', presetB: 'Conv LR MinAlign Harman 30-300Hz -7dB', activeSide: 'B' };

    // Audible preset gain for the meter sim: only the +3/+6 dB filter
    // presets lift the visible level (their real chains hold a broadband
    // gain stage); every other preset is level-neutral in the demo.
    function presetMeterGainDb(name) {
        if (name === '+3') return 3;
        if (name === '+6') return 6;
        return 0;
    }

    function demoMeterOffsetDb() {
        let offset = presetMeterGainDb(dspActivePreset);
        const extras = dspExtras || {};
        if (extras.autogain && extras.autogain.enabled) offset += 2;
        if (extras.loudness && extras.loudness.enabled) {
            offset += 0.4 * Math.max(1, Math.min(10, Number(extras.loudness.params && extras.loudness.params.strength) || 0));
        }
        if (extras.bass_enhancer && extras.bass_enhancer.enabled) {
            offset += Math.max(0, Math.min(3, Number(extras.bass_enhancer.params && extras.bass_enhancer.params.amount) || 0) / 4);
        }
        return Math.round(offset * 10) / 10;
    }

    function syncDspToState() {
        const limiter = dspExtras.limiter || {};
        S.setDspSnapshot({
            meterOffsetDb: demoMeterOffsetDb(),
            limiterThresholdDb: Number(limiter.params && limiter.params.thresholdDb) || -1,
            limiterEnabled: !!limiter.enabled,
            extras: dspExtras,
            presets: dspPresets,
            activePreset: dspActivePreset,
        });
    }
    syncDspToState();

    function dspPayload() {
        return {
            available: true,
            presets: dspPresets,
            preset_count: dspPresets.length,
            active_preset: dspActivePreset,
            irs: [
                { name: 'Conv LR HybAlign BK 30-10000Hz -7dB', basename: 'Conv LR HybAlign BK 30-10000Hz -7dB', path: '/demo/irs/Conv LR HybAlign BK 30-10000Hz -7dB.irs', size: 262428 },
                { name: 'Conv LR HybAlign BK 30-12000Hz -7dB', basename: 'Conv LR HybAlign BK 30-12000Hz -7dB', path: '/demo/irs/Conv LR HybAlign BK 30-12000Hz -7dB.irs', size: 262644 },
                { name: 'Conv LR HybAlign BK 30-3000Hz -7dB', basename: 'Conv LR HybAlign BK 30-3000Hz -7dB', path: '/demo/irs/Conv LR HybAlign BK 30-3000Hz -7dB.irs', size: 262644 },
                { name: 'Conv LR MinAlign Harman 30-300Hz -7dB', basename: 'Conv LR MinAlign Harman 30-300Hz -7dB', path: '/demo/irs/Conv LR MinAlign Harman 30-300Hz -7dB.irs', size: 262644 },
            ],
            global_extras: dspExtras,
            global_extras_excluded_presets: [],
            compare: dspCompare,
            mode: 'runtime',
            paths: {},
        };
    }

    // ── Audio output model ──────────────────────────────────────────────
    const OUTPUTS = [
        { key: 'alsa_output.pci-0000_00_1f.3.analog-stereo', name: 'Built-in Audio', label: 'Built-in Audio', description: 'Analog Stereo', channels: 2, active_rate: 48000, selectable: true, default: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000] },
        { key: 'alsa_output.usb-DEMO_DAC-00.analog-stereo', name: 'Demo USB DAC', label: 'Demo USB DAC', description: 'Hi-Res USB Audio', channels: 4, active_rate: 96000, selectable: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000] },
        { key: 'alsa_output.usb-MOTU_M4-00.analog-surround-40', name: 'MOTU M4', label: 'MOTU M4', description: '4-Channel USB Audio Interface', channels: 4, active_rate: 48000, selectable: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000] },
    ];
    // The demo starts on the 4-channel interface so the default 2.2 mode has
    // the channels it needs (Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2).
    let selectedOutputKeyCache = OUTPUTS[2].key;
    function selectedOutput() {
        return OUTPUTS.find(o => o.key === selectedOutputKeyCache) || OUTPUTS[0];
    }
    let outputMode = {
        mode: 'subwoofer-2.2',
        available: true,
        required_channels: 4,
        effective_output_channels: 4,
        routing: { main_pair: [1, 2], sub_pair: [3, 4], status: 'Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2' },
        subwoofer: { crossover_frequency_hz: 80, slope: 'LR24', main_highpass_enabled: true, sub_level_db: 0.0, sub_alignment_ms: 2.8, sub_polarity: 'normal' },
        subwoofers: {
            sub1: { level_db: 0.0, alignment_ms: 2.8, polarity: 'normal' },
            sub2: { level_db: 0.0, alignment_ms: 2.45, polarity: 'normal' },
        },
        // Derived 2.2 delays: the DSP computes Main / Sub 1 / Sub 2 from the
        // measured alignment, shown in the subwoofer card. Seeded with the
        // current demo alignment so the card reads like a configured system.
        derived_main_delay_ms: 0.0,
        derived_sub1_delay_ms: 2.8,
        derived_sub2_delay_ms: 2.45,
    };

    function normalizeSub(input = {}) {
        return {
            crossover_frequency_hz: Math.round(Number(input.crossover_frequency_hz ?? 80) || 80),
            slope: String(input.slope || 'LR24'),
            main_highpass_enabled: input.main_highpass_enabled !== false,
            sub_level_db: Number(input.sub_level_db ?? 0) || 0,
            sub_alignment_ms: Math.round((Number(input.sub_alignment_ms ?? 0) || 0) * 100) / 100,
            sub_polarity: String(input.sub_polarity || 'normal') === 'invert' ? 'invert' : 'normal',
        };
    }
    function normalizeSubOne(input = {}) {
        return {
            level_db: Number(input.level_db ?? 0) || 0,
            alignment_ms: Math.round((Number(input.alignment_ms ?? 0) || 0) * 100) / 100,
            polarity: String(input.polarity || 'normal') === 'invert' ? 'invert' : 'normal',
        };
    }
    function normalizeOutputModeName(mode) {
        return ['stereo', 'subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode) ? mode : 'stereo';
    }

    function outputsPayload() {
        const out = selectedOutput();
        return {
            loaded: true,
            available: true,
            default_output: { key: OUTPUTS[0].key, target_name: OUTPUTS[0].name, target_label: OUTPUTS[0].label },
            selected_output: { key: out.key, label: out.name, channels: out.channels, active_rate: out.active_rate, supported_rates: out.supported_rates },
            current_output: { key: out.key, label: out.name, channels: out.channels, active_rate: out.active_rate },
            outputs: OUTPUTS.map(o => ({ ...o })),
            notes: [],
            output_mode: outputMode,
        };
    }

    let samplerate = { available: true, active_rate: 48000, mode: 'auto', policy: { mode: 'auto', rate: null }, support: { rates: [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000] } };

    // Native-kHz simulation: mirrors the real rate resolution
    // (playback/orchestration.py coordinator_source_rate +
    // audio/samplerate/persistence.py effective_playback_rate). A fixed
    // policy pins the graph regardless of source; in auto the graph follows
    // the source: Spotify 44.1 kHz (SPOTIFY_PREARM_SAMPLE_RATE_HZ),
    // Qobuz/radio track rate with 44.1 kHz fallback, TIDAL per quality tier
    // (96 kHz Hi-Res, 44.1 kHz lossless/AAC), local track rate, 48 kHz idle.
    const TIDAL_TIER_GRAPH_RATE = { HI_RES_LOSSLESS: 96000, LOSSLESS: 44100, HIGH: 44100 };
    function followSourceGraphRate() {
        if (samplerate.mode === 'fixed' && samplerate.policy?.rate) return;
        const playback = S.getPlayback();
        // Stop/idle (no current track) releases the graph back to the
        // default rate, like the real system clearing the source pin. A
        // paused source keeps its rate: its renderer sink-input stays
        // allocated while paused.
        if (!playback.current_track) {
            samplerate.active_rate = 48000;
            return;
        }
        const owner = playback.playback_owner;
        const trackId = playback.current_track?.id;
        const inList = (list) => (list || []).find(t => String(t.id) === String(trackId));
        let rate = 48000;
        if (owner === 'spotify') rate = 44100;
        else if (owner === 'qobuz') rate = Number(S.qobuz.current?.sample_rate_hz) || 44100;
        else if (owner === 'radio') rate = 44100;
        // Local tracks resolve through the same cross-catalog lookup as the
        // other endpoints (active catalog first): a track still playing from
        // a library that is no longer active keeps its real rate.
        else if (owner === 'local') rate = Number(findTrack(trackId)?.track.sample_rate_hz) || 48000;
        else if (owner === 'tidal') rate = TIDAL_TIER_GRAPH_RATE[inList(S.tidalTracks())?.audio_quality] || 48000;
        samplerate.active_rate = rate;
    }
    S.onSourceChanged = followSourceGraphRate;

    // ── Audio source model ──────────────────────────────────────────
    // Mirrors the real stereo-pair abstraction
    // (audio/samplerate/overview.py + audio/external_input.py):
    // multichannel capture interfaces are offered as adjacent stereo
    // pairs (Input 1-2, Input 3-4, ...), never as single mono channels
    // and never duplicated onto both sides. Single-pair devices keep
    // the plain device label, exactly like the real overview.
    // Module scope: the fetch handler below must observe mutations
    // across requests (selected pair, active mode).
    const SOURCE_INPUTS = [
        {
            id: 101,
            key: 'alsa_input.usb-MOTU_M4-00.analog-surround-40::pair:1-2',
            source_key: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
            name: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
            device_label: 'MOTU M4',
            port_key: null,
            port_label: null,
            label: 'MOTU M4 · Input 1–2',
            sample_spec: 's32le 4ch 48000Hz',
            channels: 4,
            channel_map: ['front-left', 'front-right', 'rear-left', 'rear-right'],
            active_rate: 48000,
            state: 'RUNNING',
            is_default: true,
            selectable: true,
            is_active_port: true,
            pair_index: 0,
            pair_count: 2,
            pair_label: 'Input 1–2',
            pair_channels: [1, 2],
            left_channel: 'FL',
            right_channel: 'FR',
        },
        {
            id: 102,
            key: 'alsa_input.usb-MOTU_M4-00.analog-surround-40::pair:3-4',
            source_key: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
            name: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
            device_label: 'MOTU M4',
            port_key: null,
            port_label: null,
            label: 'MOTU M4 · Input 3–4',
            sample_spec: 's32le 4ch 48000Hz',
            channels: 4,
            channel_map: ['front-left', 'front-right', 'rear-left', 'rear-right'],
            active_rate: 48000,
            state: 'RUNNING',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 1,
            pair_count: 2,
            pair_label: 'Input 3–4',
            pair_channels: [3, 4],
            left_channel: 'RL',
            right_channel: 'RR',
        },
        {
            id: 103,
            key: 'alsa_input.usb-DemoSPDIF-00.iec958-stereo',
            source_key: 'alsa_input.usb-DemoSPDIF-00.iec958-stereo',
            name: 'alsa_input.usb-DemoSPDIF-00.iec958-stereo',
            device_label: 'USB S/PDIF',
            port_key: null,
            port_label: null,
            label: 'USB S/PDIF · Input',
            sample_spec: 's32le 2ch 48000Hz',
            channels: 2,
            channel_map: ['front-left', 'front-right'],
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 0,
            pair_count: 1,
            pair_label: 'Input 1–2',
            pair_channels: [1, 2],
            left_channel: 'FL',
            right_channel: 'FR',
        },
    ];
    // A connected phone streaming over A2DP: selecting bluetooth-input
    // in the demo lands on a genuinely active source (streaming state,
    // connected device, codec), not just a bare mode name.
    const BLUETOOTH_SOURCE = {
        available: true,
        selectable: true,
        state: 'streaming',
        receiver_enabled: true,
        discoverable: true,
        pairable: true,
        connected_device: 'Demo Phone',
        active_codec: 'aac',
        active_rate: 48000,
        notes: [],
    };
    let sourceMode = 'app-playback';
    let selectedSourceInputKey = SOURCE_INPUTS[0].key;
    function sourceInputByKey(key) {
        return SOURCE_INPUTS.find((item) => item.key === String(key || '')) || null;
    }
    function sourceOverview() {
        const selected = sourceInputByKey(selectedSourceInputKey) || SOURCE_INPUTS[0];
        const withSelected = (item) => ({ ...item, is_selected: item.key === selected.key });
        return {
            mode: sourceMode,
            modes: [
                { key: 'app-playback', label: 'App playback', selectable: true },
                { key: 'external-input', label: 'External input', selectable: true },
                { key: 'bluetooth-input', label: 'Bluetooth input', selectable: true },
            ],
            default_input: withSelected(SOURCE_INPUTS[0]),
            selected_input: withSelected(selected),
            current_input: withSelected(selected),
            inputs: SOURCE_INPUTS.map(withSelected),
            bluetooth: { ...BLUETOOTH_SOURCE },
            notes: [],
            pending: false,
        };
    }

    // ── Music libraries ─────────────────────────────────────────────────
    // The demo presents "NAS Library 1" (the second demo share) as its
    // active library, like a box with that share selected; Local and
    // "NAS Library 2" stay selectable under Settings and serve the main
    // catalog.
    const musicLibraries = {
        active_id: 'demo-library-2',
        active_type: 'smb',
        libraries: [
            { id: 'local', label: 'Local', type: 'local' },
            { id: 'demo-library-2', label: 'NAS Library 1', type: 'smb' },
            { id: 'demo-nas', label: 'NAS Library 2', type: 'smb' },
        ],
    };

    // ── Library scan / share-discovery simulation ───────────────────────
    // A real box rescans after boot and on refresh; the frontend polls
    // /api/library/status while `scanning` is true and re-fetches tracks
    // when it flips to false. The demo replays that cycle: a short scan
    // with ramping counts, then the settled totals. boot.js arms the scan
    // lazily (it loads after this file) so the very first status poll after
    // page load reports scanning; POST /api/library/refresh arms it on
    // demand. Tests can fast-forward by overriding S.demoScan.durationMs.
    function armLibraryScan(durationMs) {
        S.demoScan = {
            active: true,
            startedTs: Date.now(),
            durationMs: Number.isFinite(durationMs) ? durationMs : 1600 + Math.random() * 900,
            target: activeLib().tracks.length,
        };
    }
    function libraryScanStatus() {
        const scan = S.demoScan;
        if (!scan || !scan.active) {
            return { scanning: false, tracks_found: activeLib().tracks.length, files_seen: 0 };
        }
        const elapsed = Date.now() - scan.startedTs;
        if (elapsed >= scan.durationMs) {
            S.demoScan = null;
            return { scanning: false, tracks_found: scan.target, files_seen: Math.round(scan.target * 1.7) };
        }
        const progress = Math.min(1, elapsed / scan.durationMs);
        return {
            scanning: true,
            tracks_found: Math.min(scan.target, Math.floor(scan.target * progress)),
            files_seen: Math.floor(scan.target * progress * 1.7),
        };
    }
    function maybeArmBootScan() {
        if (window.__demoArmBootScan) {
            // Latch the boot flag once (boot.js runs after this file, so it
            // is read lazily on the first API call) and arm the scan.
            window.__demoArmBootScan = false;
            S.demoBootArmed = true;
            if (!S.demoScan) armLibraryScan();
        }
    }
    // The share-discovery flag rides /api/music-libraries: the first call
    // after boot reports a background rescan (the Settings selector shows
    // the cached shares and re-fetches once), then it settles.
    function musicLibrariesPayload() {
        maybeArmBootScan();
        let refreshing = false;
        if (S.demoBootArmed && !S.demoDiscoverySettled) {
            refreshing = true;
            S.demoDiscoverySettled = true;
        }
        return refreshing ? { ...musicLibraries, discovery_refreshing: true } : musicLibraries;
    }

    // Second demo catalog (demo/data/library2.js): the "NAS Library 1"
    // SMB share. Only the selected library serves the browse surfaces —
    // local and NAS keep serving the main catalog exactly as before.
    const lib2 = window.FXROUTE_DEMO_LIBRARY2 || { tracks: [], albums: [] };
    function activeLib() {
        return musicLibraries.active_id === 'demo-library-2' ? lib2 : lib;
    }
    // Cross-catalog lookup: the active catalog wins, the other one is a
    // fallback so ids referenced by live state (a track still playing from
    // the library that was just switched away) keep resolving. Returns the
    // owning catalog alongside the item, so dependent endpoints serve or
    // mutate the right catalog instead of re-filtering through activeLib().
    function findAlbum(id) {
        const active = activeLib();
        const other = active === lib ? lib2 : lib;
        const album = active.albums.find(a => a.id === id) || other.albums.find(a => a.id === id);
        return album ? { album, catalog: active.albums.includes(album) ? active : other } : null;
    }
    function findTrack(id) {
        const active = activeLib();
        const other = active === lib ? lib2 : lib;
        const track = active.tracks.find(t => t.id === id) || other.tracks.find(t => t.id === id);
        return track ? { track, catalog: active.tracks.includes(track) ? active : other } : null;
    }

    // ── Demo download bodies ────────────────────────────────────────────
    // Mirrors the real M3U8 export (library/playlist_io.build_m3u_for_playlist):
    // an #EXTM3U header plus one #EXTINF line and the track path per entry.
    // Tracks resolve cross-catalog, like the cover endpoints.
    function demoPlaylistM3u(playlist) {
        const lines = ['#EXTM3U'];
        for (const trackId of (playlist.track_ids || [])) {
            const track = findTrack(trackId)?.track;
            if (!track) continue;
            const duration = Number(track.duration) > 0 ? Math.round(Number(track.duration)) : -1;
            const label = track.artist ? `${track.artist} - ${track.title}` : (track.title || trackId);
            lines.push(`#EXTINF:${duration},${label}`);
            lines.push(String(track.url || track.path || trackId));
        }
        return lines.join('\n') + '\n';
    }

    // Selection download stands in for the real ZIP: the frontend only needs
    // a downloadable body, so this lists the requested tracks (title, artist,
    // source path) in a clearly-labeled demo manifest.
    function demoDownloadManifest(trackIds) {
        const lines = [
            'FXRoute web-demo selection download — the simulated box transfers no real files.',
            '',
        ];
        for (const id of (trackIds || [])) {
            const track = findTrack(id)?.track;
            lines.push(track
                ? `${track.artist} - ${track.title} :: ${track.url || track.path || id}`
                : `${id} (not found)`);
        }
        return lines.join('\n') + '\n';
    }

    // Demo stand-in for the real root CA bundle: the simulated box serves no
    // HTTPS, so the Settings certificate link downloads this clearly-labeled
    // placeholder instead of 404ing.
    const DEMO_CERT_PEM = [
        '-----BEGIN CERTIFICATE-----',
        'FXRoute web-demo placeholder certificate. This is not a real TLS',
        'certificate; the simulated box serves no HTTPS in the demo.',
        '-----END CERTIFICATE-----',
    ].join('\n') + '\n';

    // ── Measurements (saved list seeded with real fixtures) ─────────────
    const savedMeasurements = S.getSavedMeasurements().slice();

    // ── Helpers ─────────────────────────────────────────────────────────
    function makeDemoBlob(text, type) {
        // Real Blob in the browser so URL.createObjectURL works; a plain
        // text-bearing stand-in in test contexts without a Blob global.
        const content = String(text || '');
        if (typeof Blob !== 'undefined') {
            return new Blob([content], type ? { type } : undefined);
        }
        return {
            size: content.length,
            type: type || '',
            text: () => Promise.resolve(content),
            arrayBuffer: () => Promise.resolve(new Uint8Array(0).buffer),
        };
    }

    function j(data, status = 200, headers = {}) {
        // headers + blob(): the real frontend download paths read
        // Content-Disposition and consume resp.blob() (see
        // getDownloadFilenameFromResponse / triggerBlobDownload).
        const headerMap = { get: (name) => String(headers[name] || '') };
        const bodyText = (status === 302 || typeof data !== 'string') ? JSON.stringify(data) : data;
        const mime = String(headers['Content-Type'] || '').split(';')[0].trim();
        if (status === 302) {
            // Image-like redirect endpoints: the browser follows the Location
            // header only when the response really redirects; the demo serves
            // covers via the static pool instead.
            return Promise.resolve({
                ok: true,
                status: 200,
                url: data.redirect,
                headers: headerMap,
                json: () => Promise.resolve({}),
                text: () => Promise.resolve(''),
                blob: () => Promise.resolve(makeDemoBlob('', mime)),
            });
        }
        return Promise.resolve({
            ok: status >= 200 && status < 300,
            status,
            headers: headerMap,
            json: () => Promise.resolve(data),
            text: () => Promise.resolve(bodyText),
            blob: () => Promise.resolve(makeDemoBlob(bodyText, mime)),
        });
    }

    function err(detail, status = 404) {
        return j({ detail }, status);
    }

    function readBody(opts) {
        const body = (opts && opts.body) || '';
        if (typeof body === 'string') {
            try { return JSON.parse(body); } catch (e) { return {}; }
        }
        if (typeof FormData !== 'undefined' && body instanceof FormData) {
            const out = {};
            body.forEach((v, k) => { out[k] = v; });
            return out;
        }
        return {};
    }

    function tidalSearch(query) {
        const q = String(query || '').toLowerCase();
        const match = (text) => String(text || '').toLowerCase().includes(q);
        return {
            tracks: !q ? [] : tidalTracks().filter(t => match(t.title + ' ' + t.artist + ' ' + t.album)).slice(0, 25),
            albums: !q ? [] : tidalAlbums().filter(a => match(a.title + ' ' + a.artist)).slice(0, 25),
            artists: !q ? [] : tidalArtists().filter(a => match(a.name)).slice(0, 25),
            playlists: !q ? [] : tidalPlaylists().filter(p => match(p.name + ' ' + (p.description || ''))).slice(0, 25),
        };
    }

    // Static demo stand-ins for the shared artist enrichment the backend
    // attaches to TIDAL details: invented short bios (same `about` field
    // the real UI renders) plus similar artists drawn only from the demo
    // TIDAL catalog — same shape ({ type, artist, provider_artist_id,
    // art_url }) the real hydration/navigation expects, so similar cards
    // resolve to browsable demo artists without new logic.
    function tidalArtistEnrichment(artist) {
        const about = (lib.tidalArtistAbout && lib.tidalArtistAbout[artist.name]) || '';
        const similar = typeof lib.tidalSimilarFor === 'function' ? lib.tidalSimilarFor(artist.id) : [];
        return { available: true, about, similar };
    }

    function tidalAlbumEnrichment(album, artist) {
        const name = (artist && artist.name) || album.artist || '';
        const about = (lib.tidalArtistAbout && lib.tidalArtistAbout[name]) || '';
        const similar = (artist && typeof lib.tidalSimilarFor === 'function') ? lib.tidalSimilarFor(artist.id) : [];
        return {
            available: true,
            artist: { about },
            similar,
        };
    }

    function emitState() {
        window.__demoBroadcast && window.__demoBroadcast('playback', S.getPlayback());
    }

    // ── Auto Sub Optimize simulation ────────────────────────────────────
    // A plausible multi-stage run: baseline sweep, per-sub coarse scans, a
    // fine scan and the combined matrix, then the real Before/After result
    // of the matching .104 run for the active output mode and target curve
    // (2.1 Neutral, 2.2 BK, 2.2-Stereo Neutral or BK from the fixtures).
    // Stages advance by elapsed time so every poll shows further progress
    // instead of jumping straight to a finished job.
    const autoSubJobs = {};
    let autoSubSeq = 0;

    // Real .104 auto-sub runs, keyed by output mode + target key. Each run
    // holds its Before/After fixture pairs exactly as saved on .104: one
    // single-trace file per channel (left/right), so the demo replays the
    // true per-channel Before/After curves, never a full sweep.
    // The demo starts in 2.2 mode with Target Curve = Neutral, so the
    // 2.2 default is the Neutral pair. Bass-heavy targets (BK, Harman,
    // Bass Shelf) replay the BK pair — audibly closer than Neutral;
    // Neutral keeps the Neutral pair.
    // Bass-heavy targets without a BK run for their mode (2.1 has only
    // Neutral fixtures) fall back to that mode's Neutral pair.
    function autoSubRunFor(mode, targetKey) {
        const saved = S.getSavedMeasurements();
        const byId = (id) => saved.find((m) => m.id === id) || null;
        const runs = [
            { mode: 'subwoofer-2.1', targets: ['neutral', 'bk', 'harman', 'bass_shelf'], before: ['autosub-21-neutral-before-l', 'autosub-21-neutral-before-r'], after: ['autosub-21-neutral-after-l', 'autosub-21-neutral-after-r'] },
            { mode: 'subwoofer-2.2', targets: ['neutral'], before: ['autosub-22-neutral-before-l', 'autosub-22-neutral-before-r'], after: ['autosub-22-neutral-after-l', 'autosub-22-neutral-after-r'] },
            { mode: 'subwoofer-2.2', targets: ['bk', 'harman', 'bass_shelf'], before: ['autosub-22-bk-before-l', 'autosub-22-bk-before-r'], after: ['autosub-22-bk-after-l', 'autosub-22-bk-after-r'] },
            { mode: 'subwoofer-2.2-stereo', targets: ['neutral'], before: ['autosub-22stereo-neutral-before-l', 'autosub-22stereo-neutral-before-r'], after: ['autosub-22stereo-neutral-after-l', 'autosub-22stereo-neutral-after-r'] },
            { mode: 'subwoofer-2.2-stereo', targets: ['bk', 'harman', 'bass_shelf'], before: ['autosub-22stereo-bk-before-l', 'autosub-22stereo-bk-before-r'], after: ['autosub-22stereo-bk-after-l', 'autosub-22stereo-bk-after-r'] },
        ];
        const normalized = String(targetKey || 'neutral').toLowerCase();
        const complete = (run) => run.before.every(byId) && run.after.every(byId);
        return runs.find((run) => run.mode === mode && run.targets.includes(normalized) && complete(run))
            || runs.find((run) => run.mode === mode && complete(run))
            || null;
    }

    function autoSubRunMeasurements(mode, targetKey) {
        const run = autoSubRunFor(mode, targetKey);
        const saved = S.getSavedMeasurements();
        const byId = (id) => saved.find((m) => m.id === id) || null;
        if (run) {
            return { baseline: byId(run.before[0]), confirmation: byId(run.after[0]), run };
        }
        return { baseline: autoSubFallbackBaseline(mode), confirmation: autoSubFallbackConfirmation(mode), run: null };
    }

    function autoSubFallbackBaseline(mode) {
        return S.makeMeasurement({ name: 'AutoSub Baseline', channel: 'left', seed: 21, mode });
    }
    function autoSubFallbackConfirmation(mode) {
        return S.makeMeasurement({ name: 'AutoSub Confirmation', channel: 'right', seed: 22, mode });
    }
    function autoSubBaseline(mode) {
        return autoSubRunMeasurements(mode, autoSubJobsTarget(mode)).baseline;
    }
    function autoSubConfirmation(mode) {
        return autoSubRunMeasurements(mode, autoSubJobsTarget(mode)).confirmation;
    }
    function autoSubJobsTarget() {
        return 'neutral';
    }

    function applyAutoSubResult(result) {
        // Mirror the applied alignment into the audio-output model so the
        // subwoofer card and settings show the new derived delays.
        if (result.mode === 'subwoofer-2.2' || result.mode === 'subwoofer-2.2-stereo') {
            outputMode.subwoofers.sub1.alignment_ms = result.applied_sub1_alignment_ms;
            outputMode.subwoofers.sub2.alignment_ms = result.applied_sub2_alignment_ms;
            outputMode.subwoofer.sub_alignment_ms = result.applied_sub1_alignment_ms;
            outputMode.derived_main_delay_ms = result.derived_main_delay_ms;
            outputMode.derived_sub1_delay_ms = result.derived_sub1_delay_ms;
            outputMode.derived_sub2_delay_ms = result.derived_sub2_delay_ms;
        } else {
            outputMode.subwoofer.sub_alignment_ms = result.applied_alignment_ms;
            outputMode.subwoofers.sub1.alignment_ms = result.applied_alignment_ms;
            outputMode.subwoofers.sub2.alignment_ms = result.applied_alignment_ms;
            outputMode.derived_main_delay_ms = 0;
            outputMode.derived_sub1_delay_ms = result.applied_alignment_ms;
            outputMode.derived_sub2_delay_ms = result.applied_alignment_ms;
        }
    }

    function autoSubTargetLabel(targetKey) {
        const normalized = String(targetKey || 'neutral').toLowerCase();
        if (normalized === 'bk') return 'Bruel & Kjaer-style';
        if (normalized === 'harman') return 'Harman-style';
        if (normalized === 'bass_shelf') return 'Bass Shelf';
        return 'Neutral';
    }

    function autoSubResult(job) {
        const mode = job.mode;
        const targetKey = job.targetKey || 'neutral';
        // The measurements travel both at job level (live baseline push while
        // the run is in progress) and inside result (final graph display).
        // Both are the real Before/After pair of the matching .104 run.
        const { baseline, confirmation, run } = autoSubRunMeasurements(mode, targetKey);
        const targetLabel = (run && baseline.autosub_meta && baseline.autosub_meta.target
            && baseline.autosub_meta.target.label) || autoSubTargetLabel(targetKey);
        const base = {
            id: job.id,
            status: 'completed',
            message: 'Auto Sub Optimize completed.',
            target_curve: { key: targetKey, label: targetLabel },
            baseline_measurement: baseline,
            confirmation_measurement: confirmation,
        };
        if (mode === 'subwoofer-2.2' || mode === 'subwoofer-2.2-stereo') {
            const delays = autoSubRunDelays(run, mode);
            const result = {
                mode,
                applied: true,
                baseline_measurement: baseline,
                confirmation_measurement: confirmation,
                original_sub1_alignment_ms: delays.originalSub1,
                original_sub2_alignment_ms: delays.originalSub2,
                applied_sub1_alignment_ms: delays.appliedSub1,
                applied_sub2_alignment_ms: delays.appliedSub2,
                left_score_pct: 84.6,
                right_score_pct: 82.1,
                overall_score_pct: 83.4,
                winner: { score_pct: 83.0, score_L_pct: 84.6, score_R_pct: 82.1, overall_score_pct: 83.4 },
                sub1_coarse_winner: { delay_ms: delays.appliedSub1, score_pct: 84.6 },
                sub2_coarse_winner: { delay_ms: delays.appliedSub2, score_pct: 82.1 },
                derived_main_delay_ms: 0.0,
                derived_sub1_delay_ms: delays.appliedSub1,
                derived_sub2_delay_ms: delays.appliedSub2,
                fine_scan: { triggered: true, status: 'completed' },
                confidence: 'high',
            };
            if (mode === 'subwoofer-2.2-stereo') {
                result.left_winner = result.sub1_coarse_winner;
                result.right_winner = result.sub2_coarse_winner;
            }
            applyAutoSubResult(result);
            return { ...base, result };
        }
        const delays = autoSubRunDelays(run, mode);
        const result = {
            mode,
            applied: true,
            baseline_measurement: baseline,
            confirmation_measurement: confirmation,
            original_alignment_ms: delays.originalSub1,
            applied_alignment_ms: delays.appliedSub1,
            suggested_alignment_ms: delays.appliedSub1,
            left_score_pct: 84.6,
            right_score_pct: 82.1,
            overall_score_pct: 83.4,
            winner: { score_pct: 83.0, score_L_pct: 84.6, score_R_pct: 82.1, overall_score_pct: 83.4 },
            coarse_winner: { delay_ms: delays.appliedSub1, score_pct: 84.6 },
            runner_up: { delay_ms: 6.2, score_pct: 71.3 },
            fine_winner: { delay_ms: delays.appliedSub1, score_pct: 84.6 },
            fine_scan: { triggered: true, status: 'completed' },
            confidence: 'high',
        };
        applyAutoSubResult(result);
        return { ...base, result };
    }

    // Delay/score anchors for the demo result: read the applied sub delays
    // from the real run's autosub_meta so the status lines and the applied
    // output state match the displayed Before/After curves. The Before
    // delay is unknown to the demo, so the original is derived as a small
    // plausible offset from the applied value.
    function autoSubRunDelays(run, mode) {
        const fallback = mode === 'subwoofer-2.1'
            ? { originalSub1: 2.8, originalSub2: 2.8, appliedSub1: 3.4, appliedSub2: 3.4 }
            : { originalSub1: 2.8, originalSub2: 2.45, appliedSub1: 3.4, appliedSub2: 3.1 };
        if (!run) return fallback;
        const saved = S.getSavedMeasurements();
        const after = saved.find((m) => m.id === run.after[0]);
        const meta = (after && after.autosub_meta) || {};
        const delays = meta.final_delays_ms || {};
        const num = (value, fb) => (Number.isFinite(Number(value)) ? Number(value) : fb);
        if (mode === 'subwoofer-2.1') {
            const applied = num(delays.sub, fallback.appliedSub1);
            return { originalSub1: Math.round((applied + 0.6) * 100) / 100, originalSub2: Math.round((applied + 0.6) * 100) / 100, appliedSub1: applied, appliedSub2: applied };
        }
        const appliedSub1 = num(delays.sub1, fallback.appliedSub1);
        const appliedSub2 = num(delays.sub2, fallback.appliedSub2);
        return {
            originalSub1: Math.round((appliedSub1 + 0.6) * 100) / 100,
            originalSub2: Math.round((appliedSub2 + 0.6) * 100) / 100,
            appliedSub1,
            appliedSub2,
        };
    }

    // elapsedMs is injectable so the behavior test can fast-forward a run.
    function autoSubJobPayload(id, elapsedMs) {
        const job = autoSubJobs[id];
        if (!job) return null;
        if (job.status === 'cancelled') {
            return { id, status: 'cancelled', message: 'Auto Sub Optimize cancelled.' };
        }
        const mode = job.mode;
        const targetKey = job.targetKey || 'neutral';
        const elapsed = (elapsedMs != null) ? elapsedMs : (Date.now() - job.startedAt);
        const isStereoBass = mode === 'subwoofer-2.2-stereo';
        const is22 = mode === 'subwoofer-2.2' || isStereoBass;
        const sub1Label = isStereoBass ? 'Left Sub' : 'Sub 1';
        const sub2Label = isStereoBass ? 'Right Sub' : 'Sub 2';
        const { baseline } = autoSubRunMeasurements(mode, targetKey);
        const targetLabel = (baseline.autosub_meta && baseline.autosub_meta.target
            && baseline.autosub_meta.target.label) || autoSubTargetLabel(targetKey);
        const base = { id, target_curve: { key: targetKey, label: targetLabel } };
        const withBaseline = () => ({ ...base, baseline_measurement: baseline });

        if (elapsed < 900) return { ...base, status: 'queued', message: 'Auto Sub Optimize: queued' };
        if (elapsed < 2000) {
            const t = Math.min(1, (elapsed - 900) / 1100);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Baseline sweep (main L/R)…',
                progress: { current: 1, total: 5, stage: 'coarse', sweep_current: 1 + Math.floor(t * 4), sweep_total: 4 },
            };
        }
        if (is22 && elapsed < 4200) {
            const t = Math.min(1, (elapsed - 2000) / 2200);
            const n = 1 + Math.floor(t * 4);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Coarse scan ' + sub1Label + '…',
                progress: { current: 2, total: 5, stage: isStereoBass ? 'left_sub' : 'sub1_coarse', candidate_current: n, candidate_total: 4, sweep_current: n, sweep_total: 4 },
            };
        }
        if (is22 && elapsed < 6400) {
            const t = Math.min(1, (elapsed - 4200) / 2200);
            const n = 1 + Math.floor(t * 4);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Coarse scan ' + sub2Label + '…',
                progress: { current: 3, total: 5, stage: isStereoBass ? 'right_sub' : 'sub2_coarse', candidate_current: n, candidate_total: 4, sweep_current: n, sweep_total: 4 },
            };
        }
        if (is22 && elapsed < 7600) {
            const t = Math.min(1, (elapsed - 6400) / 1200);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Fine scan…',
                progress: { current: 4, total: 5, stage: 'fine', sweep_current: 1 + Math.floor(t * 3), sweep_total: 3 },
            };
        }
        if (is22 && elapsed < 8800) {
            const t = Math.min(1, (elapsed - 7600) / 1200);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Combined matrix…',
                progress: { current: 5, total: 5, stage: 'combined_matrix', sweep_current: 1 + Math.floor(t * 2), sweep_total: 2 },
            };
        }
        if (!is22 && elapsed < 6400) {
            const t = Math.min(1, (elapsed - 2000) / 4400);
            const n = 1 + Math.floor(t * 8);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Coarse scan…',
                progress: { current: 2, total: 4, stage: 'coarse', candidate_current: n, candidate_total: 8, sweep_current: n, sweep_total: 8 },
            };
        }
        if (!is22 && elapsed < 8000) {
            const t = Math.min(1, (elapsed - 6400) / 1600);
            return {
                ...withBaseline(),
                status: 'running',
                message: 'Fine scan…',
                progress: { current: 3, total: 4, stage: 'fine', sweep_current: 1 + Math.floor(t * 3), sweep_total: 3 },
            };
        }
        return autoSubResult(job);
    }

    // ── fetch interceptor ───────────────────────────────────────────────
    const origFetch = window.fetch;
    window.fetch = function (url, opts) {
        const raw = typeof url === 'string' ? url : (url && url.url) || '';
        const parsed = raw.replace(/^https?:\/\/[^/]+/, '');
        const p = parsed.split('?')[0];
        const method = (opts && opts.method) || 'GET';
        const query = new URLSearchParams((parsed.split('?')[1] || ''));
        const body = readBody(opts);
        const post = method === 'POST' || method === 'PATCH';

        // ── playback / status ───────────────────────────────────────────
        if (p === '/api/status') return j(S.getPlayback());
        if (p === '/api/play' && post) {
            const src = String(body.source || 'local');
            if (src === 'radio') {
                const track = S.playRadio(String(body.track_id || 'groovesalad'));
                return j({ status: 'playing', url: (track && track.url) || '', track: track || {}, playback: S.getPlayback() });
            }
            if (src === 'tidal') {
                const track = S.playTidal(String(body.track_id || ''), body.queue_track_ids);
                return j({ status: 'playing', url: (track && track.url) || '', track: track || {}, playback: S.getPlayback() });
            }
            const track = S.playLocal(String(body.track_id || ''), body.queue_track_ids);
            return j({ status: 'playing', url: (track && track.url) || '', track: track || {}, playback: S.getPlayback() });
        }
        if (p === '/api/playback/play' && post) { S.playLocal(String(body.track_id || '')); return j({ ok: true }); }
        if (p === '/api/playback/toggle' && post) { S.togglePause(); return j({ ok: true }); }
        if (p === '/api/playback/pause' && post) { S.togglePause(); return j({ ok: true }); }
        if (p === '/api/pause' && post) { S.togglePause(); return j({ ok: true }); }
        if (p === '/api/playback/stop' && post) { S.stop(); return j({ ok: true }); }
        if (p === '/api/stop' && post) { S.stop(); return j({ ok: true }); }
        if (p === '/api/playback/clear-queue' && post) { S.clearQueue(); return j({ ok: true }); }
        if (p === '/api/playback/next' && post) { S.playNext(); return j({ ok: true, playback: S.getPlayback() }); }
        if (p === '/api/playback/previous' && post) { S.playPrev(); return j({ ok: true, playback: S.getPlayback() }); }
        if (p === '/api/playback/seek' && post) { S.seek(body.position != null ? body.position : body.positionSec); return j({ ok: true }); }
        if (p === '/api/playback/shuffle' && post) { S.setShuffle(!!body.enabled); return j({ ok: true }); }
        if (p === '/api/playback/loop' && post) { S.setLoop(!!body.enabled); return j({ ok: true }); }
        if (p === '/api/volume' && post) { S.setVolume(body.volume); return j({ ok: true, volume: S.getVolume() }); }

        // ── Radio ───────────────────────────────────────────────────────
        if (p === '/api/stations') {
            if (post) {
                const sid = 'demo_station_' + (stations.length + 1);
                const streamUrl = String(body.stream_url || '');
                const station = {
                    id: sid,
                    title: String(body.name || 'Station ' + (stations.length + 1)),
                    name: String(body.name || 'Station ' + (stations.length + 1)),
                    provider: 'Custom',
                    artist: 'Custom',
                    genres: [],
                    input_url: streamUrl,
                    stream_url: streamUrl,
                    image_url: body.custom_image_url || '',
                    logo_url: body.custom_image_url || '',
                    custom_image_url: body.custom_image_url || '',
                };
                stations.push(station);
                return j({ station: { id: sid, title: station.title, stream_url: streamUrl, custom_image_url: station.custom_image_url } });
            }
            return j(stations.slice());
        }
        if (p === '/api/station-catalog') {
            return j(catalogStations.map((station) => {
                const saved = stations.find((item) =>
                    item.input_url === station.input_url || item.stream_url === station.stream_url);
                return { ...station, is_saved: !!saved, saved_station_id: saved?.id || null };
            }));
        }
        const stationCrud = p.match(/^\/api\/stations\/([^/]+)$/);
        if (stationCrud) {
            const id = stationCrud[1];
            const idx = stations.findIndex(s => s.id === id);
            if (method === 'DELETE') {
                if (idx >= 0) stations.splice(idx, 1);
                return j({ ok: true });
            }
            if (method === 'PUT') {
                const s = stations[idx];
                if (!s) return err('Station not found');
                if (body.stream_url) s.stream_url = String(body.stream_url);
                if (body.name) { s.name = String(body.name); s.title = String(body.name); }
                if (body.custom_image_url !== undefined) s.custom_image_url = String(body.custom_image_url);
                return j({ station: { id: s.id, title: s.title, stream_url: s.stream_url } });
            }
            return j(stations[idx] || {});
        }
        if (p === '/api/stations/import' && post) {
            const items = Array.isArray(body) ? body : (Array.isArray(body.items) ? body.items : []);
            const results = items.map((item, i) => {
                const id = 'demo_station_import_' + i;
                const name = item.name || item.title || 'Imported Station';
                const url = item.url || item.stream_url || '';
                stations.push({ id, title: name, name, provider: 'Imported', artist: 'Imported', input_url: url, stream_url: url, image_url: item.logo || '', logo_url: item.logo || '', custom_image_url: item.logo || '' });
                return { id, status: 'ok', title: name };
            });
            return j({ results });
        }
        const catalogSelection = p.match(/^\/api\/station-catalog\/([^/]+)\/selection$/);
        if (catalogSelection && post) {
            const cat = catalogStations.find(s => s.id === catalogSelection[1]);
            if (!cat) return err('Station not found in catalog');
            const id = 'station_' + cat.id;
            if (!stations.find(s => s.id === id)) {
                stations.push({ ...cat, id, title: cat.title, name: cat.name, artist: cat.artist });
            }
            return j({ station: { id, title: cat.title } });
        }
        if (p === '/api/station-browser/search') {
            const q = String(query.get('query') || '').toLowerCase();
            const results = catalogStations
                .filter(s => !q || s.title.toLowerCase().includes(q) || s.artist.toLowerCase().includes(q) || (s.genres || []).join(' ').toLowerCase().includes(q))
                .map(s => ({
                    stationuuid: s.id,
                    name: s.title,
                    url: s.input_url || s.stream_url,
                    url_resolved: s.stream_url,
                    homepage: 'https://example.invalid/' + s.id,
                    favicon: s.image_url || '',
                    tags: (s.genres || []).join(','),
                    country: 'Demo',
                    language: 'EN',
                    codec: 'MP3',
                    bitrate: 128,
                    is_saved: stations.some(st => st.id === 'station_' + s.id),
                    saved_station_id: stations.some(st => st.id === 'station_' + s.id) ? 'station_' + s.id : null,
                }));
            return j(results);
        }
        const browserSelection = p.match(/^\/api\/station-browser\/([^/]+)\/selection$/);
        if (browserSelection && post) {
            const uuid = browserSelection[1];
            const cat = catalogStations.find(s => s.id === uuid);
            const id = 'station_' + uuid;
            if (!stations.find(s => s.id === id)) {
                stations.push({ ...(cat || { id: uuid, title: uuid, name: uuid, artist: 'Radio', provider: 'Online' }), id });
            }
            return j({ station: { id, title: (cat && cat.title) || uuid } });
        }

        // ── Library (served from the selected library catalog) ──────────
        if (p === '/api/tracks') return j(activeLib().tracks);
        if (p === '/api/albums') {
            const catalog = activeLib().albums;
            const q = String(query.get('query') || '').toLowerCase();
            return j(q ? catalog.filter(a => (a.name + ' ' + a.artist + ' ' + (a.genres || []).join(' ')).toLowerCase().includes(q)) : catalog);
        }
        if (p === '/api/playlists') {
            // Playlists live per library catalog, like separate shares on a
            // real box: selecting a library switches the playlist set too.
            const catalogPlaylists = activeLib().playlists || [];
            if (post) {
                const name = String(body.name || '').trim();
                const trackIds = Array.isArray(body.track_ids) ? body.track_ids : [];
                if (!name) return err('Playlist name required', 400);
                const id = 'playlist_' + Date.now();
                catalogPlaylists.push({ id, name, track_ids: trackIds, track_count: trackIds.length });
                return j({ status: 'ok', playlist: { id, name, track_ids: trackIds, track_count: trackIds.length } });
            }
            return j(catalogPlaylists);
        }
        const playlistExportMatch = p.match(/^\/api\/playlists\/([^/]+)\/export$/);
        if (playlistExportMatch) {
            // Mirror the real backend (library/api.py export_playlist): an
            // M3U8 attachment with one EXTINF line + path per track. Handled
            // before the generic CRUD route so export is not swallowed.
            const id = playlistExportMatch[1];
            const catalogPlaylists = activeLib().playlists || [];
            const playlist = catalogPlaylists.find(pl => pl.id === id);
            if (!playlist) return err('Playlist not found', 404);
            const safeName = String(playlist.name || 'playlist').replace(/["\r\n]/g, '');
            return j(demoPlaylistM3u(playlist), 200, {
                'Content-Type': 'audio/x-mpegurl; charset=utf-8',
                'Content-Disposition': `attachment; filename="${safeName}.m3u8"`,
            });
        }
        const playlistCrud = p.match(/^\/api\/playlists\/([^/]+)$/);
        if (playlistCrud) {
            const id = playlistCrud[1];
            if (method === 'DELETE') {
                const catalogPlaylists = activeLib().playlists || [];
                const idx = catalogPlaylists.findIndex(pl => pl.id === id);
                if (idx >= 0) catalogPlaylists.splice(idx, 1);
                return j({ status: 'ok', deleted: id });
            }
            return err('Playlist not found');
        }
        const albumTracksMatch = p.match(/^\/api\/albums\/([^/]+)\/tracks$/);
        if (albumTracksMatch) {
            // Tracks come from the album's owning catalog: an id from the
            // inactive library (e.g. a still-playing track) must not be
            // filtered against the active catalog.
            const resolved = findAlbum(albumTracksMatch[1]);
            if (!resolved) return err('Album not found');
            const { album, catalog } = resolved;
            return j(catalog.tracks.filter(t => t.album === album.name && (t.artist === album.artist || t.album_artist === album.artist)));
        }
        const albumFavMatch = p.match(/^\/api\/albums\/([^/]+)\/favorite$/);
        if (albumFavMatch) {
            const resolved = findAlbum(albumFavMatch[1]);
            if (!resolved) return err('Album not found');
            resolved.album.favorite = !!body.favorite;
            return j({ status: 'ok', album_id: resolved.album.id, favorite: resolved.album.favorite });
        }
        const trackFavMatch = p.match(/^\/api\/tracks\/([^/]+)\/favorite$/);
        if (trackFavMatch) {
            const resolved = findTrack(trackFavMatch[1]);
            if (!resolved) return err('Track not found');
            resolved.track.favorite = !!body.favorite;
            return j({ status: 'ok', track_id: resolved.track.id, favorite: resolved.track.favorite });
        }
        const albumCoverMatch = p.match(/^\/api\/albums\/([^/]+)\/cover$/);
        if (albumCoverMatch) {
            const resolved = findAlbum(albumCoverMatch[1]);
            const redirect = resolved ? resolved.album.coverUrl : lib.demoImage('album:' + (albumCoverMatch[1] || 'x'));
            return j({ redirect });
        }
        const trackCoverMatch = p.match(/^\/api\/tracks\/cover\/([^/]+)$/);
        if (trackCoverMatch) {
            const resolved = findTrack(trackCoverMatch[1]);
            return j({ redirect: resolved ? resolved.track.cover_url : lib.demoImage('track:' + (trackCoverMatch[1] || 'x')) });
        }
        const trackCoverInfoMatch = p.match(/^\/api\/tracks\/cover-info\/([^/]+)$/);
        if (trackCoverInfoMatch) {
            const resolved = findTrack(trackCoverInfoMatch[1]);
            return j({ cover_url: resolved ? resolved.track.cover_url : '', cover_available: !!resolved });
        }
        const albumDiscover = p.match(/^\/api\/albums\/([^/]+)\/discover$/);
        if (albumDiscover) {
            // Same contract as the backend (ListenBrainz artist-seeded
            // suggestions, max 6): reuse the active demo catalog — same
            // genre first, then same decade, then the rest in catalog
            // order. Returns full album entries so the real UI renders
            // identical tiles, no demo-only recommendation logic.
            const catalog = activeLib().albums;
            const album = catalog.find(a => a.id === albumDiscover[1]);
            if (!album) return err('Album not found');
            const genre = (album.genres || [])[0] || '';
            const decade = Math.floor(Number(album.year || 0) / 10);
            const scored = catalog
                .filter(a => a.id !== album.id)
                .map(a => {
                    const sameGenre = genre && (a.genres || []).includes(genre) ? 0 : 1;
                    const sameDecade = Number.isFinite(decade) && decade > 0
                        && Math.floor(Number(a.year || 0) / 10) === decade ? 0 : 1;
                    return { album: a, score: sameGenre * 10 + sameDecade };
                })
                .sort((x, y) => x.score - y.score);
            return j({ album_id: album.id, items: scored.slice(0, 6).map(s => s.album), source: 'demo', cached: true, error: '' });
        }
        if (p === '/api/smart/top-tracks') return j(activeLib().tracks.slice(0, 40));
        if (p === '/api/library/status') {
            maybeArmBootScan();
            return j(libraryScanStatus());
        }
        if (p === '/api/library/refresh' && post) {
            armLibraryScan();
            return j({ status: 'ok', ...libraryScanStatus() });
        }
        if (p === '/api/library/folders/delete' && post) return j({ status: 'ok' });
        if (p === '/api/tracks/delete' && post) return j({ status: 'ok' });
        if (p === '/api/tracks/download' && post) {
            // Selection download: a downloadable body for the POSTed ids so
            // the frontend blob path works (see downloadSelectedTracks).
            const trackIds = Array.isArray(body.track_ids) ? body.track_ids : [];
            const single = trackIds.length === 1;
            return j(demoDownloadManifest(trackIds), 200, {
                'Content-Type': 'text/plain; charset=utf-8',
                'Content-Disposition': `attachment; filename="${single ? 'track' : 'fxroute-library-selection.zip'}"`,
            });
        }

        // ── Streaming providers ─────────────────────────────────────────
        if (p === '/api/streaming/providers') {
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', installed: true, available: true, authenticated: true, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', installed: true, available: true, authenticated: true, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', installed: true, available: true, authenticated: true, connected: true, capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
        // The demo provider flags live on the shared state object so the
        // enabled toggles persist across fetch calls.
        if (p === '/api/streaming/providers/discovery') {
            // Same shape as the backend's streaming.discover_providers(): the
            // current frontend builds the provider tabs from this endpoint.
            S.demoProviderEnabled = S.demoProviderEnabled || { spotify: true, qobuz: true, tidal: true };
            const enabled = S.demoProviderEnabled;
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: null, enabled: enabled.spotify !== false, connected: true, backend: 'spotifyd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: true, enabled: enabled.qobuz !== false, connected: true, backend: 'qbzd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: true, enabled: enabled.tidal !== false, connected: true, backend: 'tidalapi', capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
        // Settings -> Providers admin (device name rides the same payload).
        // Demo keeps all providers installed/connected; toggles only flip
        // the local enabled flag so tabs hide/show like the real backend.
        function demoProviderAdmin() {
            S.demoProviderEnabled = S.demoProviderEnabled || { spotify: true, qobuz: true, tidal: true };
            const enabled = S.demoProviderEnabled;
            return {
                providers: [
                    { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: null, enabled: enabled.spotify !== false },
                    { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: true, enabled: enabled.qobuz !== false },
                    { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: true, enabled: enabled.tidal !== false },
                ],
                device_name: 'fxroute',
                device_name_can_change: false,
            };
        }
        if (p === '/api/streaming/providers/admin') return j(demoProviderAdmin());
        const providerEnabledMatch = p.match(/^\/api\/streaming\/providers\/([^/]+)\/enabled$/);
        if (providerEnabledMatch && post) {
            const id = providerEnabledMatch[1];
            S.demoProviderEnabled = S.demoProviderEnabled || { spotify: true, qobuz: true, tidal: true };
            if (id in S.demoProviderEnabled) S.demoProviderEnabled[id] = body.enabled !== false;
            return j({ ok: true, enabled: S.demoProviderEnabled[id] !== false });
        }
        const providerOpMatch = p.match(/^\/api\/streaming\/providers\/([^/]+)\/(install|uninstall)$/);
        if (providerOpMatch && post) {
            return j({ installed: true, log: 'Demo: provider operation simulated.' });
        }
        const providerSvcMatch = p.match(/^\/api\/streaming\/providers\/([^/]+)\/service\/([^/]+)$/);
        if (providerSvcMatch && post) {
            return j({ ok: true });
        }
        if (p === '/api/system/device-name' && post) {
            return j({ hostname: 'fxroute', changed: false });
        }
        if (p === '/api/streaming/qobuz/auth/login' && post) {
            return j({ login_url: 'https://example.invalid/qobuz-login-demo' });
        }
        if (p === '/api/streaming/qobuz/auth/login/cancel' && post) return j({ ok: true });
        if (p === '/api/streaming/qobuz/auth/login/finish' && post) {
            return j({ success: true });
        }
        if (p === '/api/streaming/qobuz/auth/logout' && post) return j({ success: true });
        if (p === '/api/spotify/status') return j(S.spotify.snapshot());
        const spotifyCmd = p.match(/^\/api\/spotify\/([a-z_]+)$/);
        if (spotifyCmd && post) {
            const cmd = spotifyCmd[1];
            const sp = S.spotify;
            if (cmd === 'toggle') sp.toggle();
            else if (cmd === 'demo_start') { sp.demoStart(); }
            else if (cmd === 'play') { if (!sp.current) sp.demoStart(); else { sp.playing = true; sp.lastTick = Date.now(); } }
            else if (cmd === 'pause') sp.playing = false;
            else if (cmd === 'next') sp.next();
            else if (cmd === 'previous') sp.previous();
            else if (cmd === 'shuffle') sp.toggleShuffle();
            else if (cmd === 'loop') sp.cycleLoop();
            else if (cmd === 'seek') sp.seek(body.position);
            emitState();
            return j(sp.snapshot());
        }
        if (p === '/api/streaming/spotify/status') return j(S.spotify.snapshot());
        if (p === '/api/streaming/qobuz/status') return j(S.qobuz.payload());
        const qobuzCmd = p.match(/^\/api\/streaming\/qobuz\/([a-z_]+)$/);
        if (qobuzCmd && post) {
            const cmd = qobuzCmd[1];
            const q = S.qobuz;
            if (cmd === 'toggle') q.toggle();
            else if (cmd === 'demo_start' || cmd === 'play') { q.demoStart(); }
            else if (cmd === 'pause') q.playing = false;
            else if (cmd === 'next') q.next();
            else if (cmd === 'previous') q.previous();
            else if (cmd === 'seek') q.seek(body.position);
            else if (cmd === 'shuffle') q.toggleShuffle();
            else if (cmd === 'repeat') q.cycleLoop();
            emitState();
            return j(q.payload());
        }
        if (p === '/api/streaming/tidal/status') {
            const ps = S.getPlayback();
            const isTidal = ps.current_track && ps.current_track.source === 'tidal';
            return j({
                installed: true,
                available: true,
                authenticated: true,
                connected: true,
                status: (isTidal && ps.playing) ? 'Playing' : (isTidal ? 'Paused' : (ps.current_track ? 'Paused' : 'Stopped')),
                title: ps.current_track ? ps.current_track.title : '',
                artist: ps.current_track ? ps.current_track.artist : '',
                album: ps.current_track ? ps.current_track.album : '',
                artUrl: ps.current_track ? (ps.current_track.art_url || ps.current_track.artwork_url || '') : '',
                user: { id: 'demo-tidal-user' },
                position: ps.position,
                duration: ps.duration,
                shuffle: !!ps.queue.shuffle,
                loop: ps.queue.loop ? 'playlist' : 'none',
                capabilities: { catalog: true, cover: true, progress: true, seek: true, transport: true },
            });
        }
        if (p === '/api/streaming/tidal/library/snapshot') {
            return j({
                user_id: 'demo-tidal-user',
                ids: {
                    tracks: Array.from(tidalFavs.tracks),
                    albums: Array.from(tidalFavs.albums),
                    artists: Array.from(tidalFavs.artists),
                    playlists: Array.from(tidalFavs.playlists),
                },
                tracks: tidalTracks().filter(t => tidalFavs.tracks.has(t.id)),
                albums: tidalAlbums().filter(a => tidalFavs.albums.has(a.id)),
                artists: tidalArtists().filter(a => tidalFavs.artists.has(a.id)),
                playlists: tidalPlaylists().filter(pl => tidalFavs.playlists.has(pl.id)),
            });
        }
        if (p === '/api/streaming/tidal/favorites/ids') {
            return j({ tracks: [...tidalFavs.tracks], albums: [...tidalFavs.albums], artists: [...tidalFavs.artists], playlists: [...tidalFavs.playlists] });
        }
        if (p === '/api/streaming/tidal/favorites') {
            const type = String(query.get('type') || 'albums');
            if (type === 'tracks') return j(tidalTracks().filter(t => tidalFavs.tracks.has(t.id)));
            if (type === 'artists') return j(tidalArtists().filter(a => tidalFavs.artists.has(a.id)));
            if (type === 'playlists') return j(tidalPlaylists().filter(pl => tidalFavs.playlists.has(pl.id)));
            return j(tidalAlbums().filter(a => tidalFavs.albums.has(a.id)));
        }
        if (p === '/api/streaming/tidal/playlists' && !post) {
            return j(tidalPlaylists().filter(pl => tidalFavs.playlists.has(pl.id)));
        }
        if (p === '/api/streaming/tidal/playlists/create' && post) {
            const name = String(body.name || 'New Playlist').trim();
            const trackIds = Array.isArray(body.track_ids) ? body.track_ids : [];
            const id = 't_playlist_' + Date.now();
            const tracks = trackIds.map(tid => tidalTracks().find(t => t.id === tid)).filter(Boolean).map(t => ({ ...t }));
            const pl = { id, name, description: 'Demo playlist', track_count: tracks.length, art_url: lib.demoImage('playlist:' + id), cover_url: lib.demoImage('playlist:' + id), owner: 'fxroute-demo', is_public: true, tracks };
            tidalPlaylists().push(pl);
            tidalFavs.playlists.add(id);
            return j({ id, name, owner: 'fxroute-demo', track_count: tracks.length });
        }
        const tidalPlaylistTracksAdd = p.match(/^\/api\/streaming\/tidal\/playlists\/([^/]+)\/tracks$/);
        if (tidalPlaylistTracksAdd && post) {
            const pl = tidalPlaylists().find(x => x.id === tidalPlaylistTracksAdd[1]);
            const ids = Array.isArray(body.track_ids) ? body.track_ids : [];
            const add = ids.map(id => tidalTracks().find(t => t.id === id)).filter(Boolean);
            if (pl) pl.tracks.push(...add);
            return j({ ok: true });
        }
        const tidalPlaylistDetail = p.match(/^\/api\/streaming\/tidal\/playlists\/([^/]+)$/);
        if (tidalPlaylistDetail) {
            const pl = tidalPlaylists().find(x => x.id === tidalPlaylistDetail[1]);
            if (!pl) return err('Playlist not found');
            return j({
                id: pl.id,
                name: pl.name,
                description: pl.description,
                cover_url: pl.cover_url,
                art_url: pl.art_url,
                track_count: pl.tracks.length,
                owner: pl.owner,
                tracks: pl.tracks,
            });
        }
        const tidalPlaylistTracks = p.match(/^\/api\/streaming\/tidal\/playlists\/([^/]+)\/tracks$/);
        if (tidalPlaylistTracks && !post) {
            const pl = tidalPlaylists().find(x => x.id === tidalPlaylistTracks[1]);
            return j(pl ? pl.tracks : []);
        }
        const tidalAlbumDetail = p.match(/^\/api\/streaming\/tidal\/albums\/([^/]+)$/);
        if (tidalAlbumDetail) {
            const album = tidalAlbums().find(a => a.id === tidalAlbumDetail[1]);
            if (!album) return err('Album not found');
            const artist = tidalArtists().find(a => a.id === album.artist_id);
            return j({ ...album, tracks: album.tracks, enrichment: tidalAlbumEnrichment(album, artist) });
        }
        const tidalAlbumTracks = p.match(/^\/api\/streaming\/tidal\/albums\/([^/]+)\/tracks$/);
        if (tidalAlbumTracks) {
            const album = tidalAlbums().find(a => a.id === tidalAlbumTracks[1]);
            return j(album ? album.tracks : []);
        }
        const tidalArtistDetail = p.match(/^\/api\/streaming\/tidal\/artists\/([^/]+)$/);
        if (tidalArtistDetail) {
            const artist = tidalArtists().find(a => a.id === tidalArtistDetail[1]);
            if (!artist) return err('Artist not found');
            const albums = tidalAlbums().filter(a => a.artist_id === artist.id);
            return j({
                ...artist,
                albums: albums.map(a => ({ id: a.id, title: a.title, artist: artist.name, year: a.year, audio_quality: a.audio_quality, num_tracks: a.num_tracks, art_url: a.cover_url })),
                top_tracks: albums.flatMap(a => a.tracks.slice(0, 3).map((t, i) => ({ ...t, id: a.id + '_top' + i, artist: artist.name, album: a.title, art_url: a.cover_url }))).slice(0, 10),
                enrichment: tidalArtistEnrichment(artist),
            });
        }
        if (p === '/api/streaming/tidal/search') {
            return j(tidalSearch(query.get('q')));
        }
        const tidalFavToggle = p.match(/^\/api\/streaming\/tidal\/(tracks|albums|artists|playlists)\/([^/]+)\/favorite$/);
        if (tidalFavToggle && post) {
            const type = tidalFavToggle[1];
            const id = tidalFavToggle[2];
            if (body.favorite) tidalFavs[type].add(id);
            else tidalFavs[type].delete(id);
            return j({ favorite: !!body.favorite });
        }
        if (p === '/api/streaming/tidal/auth/device' && post) return j({ device_code: 'DEMO42', user_code: 'DEMO-AUTH', verification_uri: 'https://link.tidal.com', verification_uri_complete: 'https://link.tidal.com/demo-auth' });
        if (p === '/api/streaming/tidal/auth/device/finish' && post) return j({ success: true });
        if (p === '/api/streaming/tidal/auth/pkce' && post) return j({ url: 'https://link.tidal.com/demo-auth' });
        if (p === '/api/streaming/tidal/auth/pkce/finish' && post) return j({ success: true });
        if (p === '/api/streaming/tidal/auth/logout' && post) return j({ success: true });

        // ── Audio / settings ────────────────────────────────────────────
        if (p === '/api/audio/outputs') {
            if (post) {
                const key = String(body.key || '');
                if (OUTPUTS.find(o => o.key === key)) selectedOutputKeyCache = key;
                return j(outputsPayload());
            }
            return j(outputsPayload());
        }
        if (p === '/api/audio/output-mode') {
            if (post) {
                const mode = normalizeOutputModeName(String(body.mode || 'stereo'));
                if (body.subwoofer) {
                    outputMode.subwoofer = normalizeSub(body.subwoofer);
                }
                if (body.subwoofers) {
                    outputMode.subwoofers = {
                        sub1: normalizeSubOne(body.subwoofers.sub1 || {}),
                        sub2: normalizeSubOne(body.subwoofers.sub2 || {}),
                    };
                }
                if (body.crossover_frequency_hz !== undefined && !body.subwoofer) {
                    outputMode.subwoofer.crossover_frequency_hz = Math.round(Number(body.crossover_frequency_hz) || 80);
                }
                outputMode.mode = mode;
                outputMode.available = true;
                outputMode.required_channels = mode === 'stereo' ? 2 : 4;
                outputMode.effective_output_channels = selectedOutput().channels;
                outputMode.routing = {
                    status: mode === 'stereo' ? 'Out 1/2 Main'
                        : mode === 'subwoofer-2.2-stereo' ? 'Out 1/2 Main · Out 3 Left Sub · Out 4 Right Sub'
                            : 'Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2',
                };
                return j(outputsPayload());
            }
            return j(outputMode);
        }
        if (p === '/api/audio/samplerate') {
            if (post) {
                const mode = String(body.mode || 'auto');
                const rate = Number(body.rate || 0);
                samplerate = {
                    ...samplerate,
                    mode: mode === 'fixed' ? 'fixed' : 'auto',
                    policy: { mode: mode === 'fixed' ? 'fixed' : 'auto', rate: (mode === 'fixed' && rate) ? rate : null },
                    active_rate: (mode === 'fixed' && rate) ? rate : samplerate.active_rate,
                };
                return j(samplerate);
            }
            return j(samplerate);
        }
        if (p === '/api/audio/source-mode') {
            if (post) {
                const mode = String(body.mode || '');
                const inputKey = body.inputKey != null && body.inputKey !== ''
                    ? String(body.inputKey)
                    : (body.input_key != null ? String(body.input_key) : '');
                if (mode === 'bluetooth-input') {
                    // Switching sources stops app playback, like the real
                    // backend pausing every app renderer for external input.
                    S.stop();
                    sourceMode = mode;
                    return j(sourceOverview());
                }
                if (mode === 'external-input') {
                    const input = (inputKey && sourceInputByKey(inputKey))
                        || sourceInputByKey(selectedSourceInputKey)
                        || SOURCE_INPUTS[0];
                    if (inputKey && !sourceInputByKey(inputKey)) return err('Unknown input: ' + inputKey, 400);
                    selectedSourceInputKey = input.key;
                    S.stop();
                    sourceMode = mode;
                    return j(sourceOverview());
                }
                sourceMode = 'app-playback';
                if (inputKey && sourceInputByKey(inputKey)) selectedSourceInputKey = inputKey;
                return j(sourceOverview());
            }
            return j(sourceOverview());
        }
        if (p === '/api/music-libraries') return j(musicLibrariesPayload());
        if (p === '/api/music-libraries/manual' && post) {
            const url = String(body.url || '');
            const entry = { id: 'demo-nas-' + Date.now(), label: url.split('/').filter(Boolean).pop() || 'NAS', type: 'smb' };
            if (!musicLibraries.libraries.find(l => l.id === entry.id)) musicLibraries.libraries.push(entry);
            return j({ ...musicLibraries, entry });
        }
        if (p === '/api/music-libraries/select' && post) {
            const id = String(body.id || '');
            const entry = musicLibraries.libraries.find(l => l.id === id);
            if (entry) {
                const changed = entry.id !== musicLibraries.active_id;
                musicLibraries.active_id = entry.id;
                musicLibraries.active_type = entry.type;
                // Local playback + queue fallbacks in state.js resolve
                // against the active catalog.
                if (typeof S.setActiveLibraryId === 'function') S.setActiveLibraryId(entry.id);
                // A real box rescans the freshly selected share; arm a short
                // scan (the frontend polls /api/library/status right after
                // the switch, so the new catalog visibly settles).
                if (changed) armLibraryScan(1100 + Math.random() * 800);
            }
            return j(musicLibraries);
        }
        if (p === '/api/system/update') {
            if (post) return j({ ok: true, installed_version: '0.9.11', stdout: 'Current: 0.9.11\nRemote: 0.9.11\nFXRoute is already up to date.', restart_scheduled: false });
            return j({ ok: true, installed_version: '0.9.11', stdout: 'Current: 0.9.11\nRemote: 0.9.11\nFXRoute is already up to date.', detail: '' });
        }
        // Demo-only power contract: expose the same capability flags as the
        // real frontend, but keep both actions as inert acknowledgements.
        if (p === '/api/system/power') return j({
            available: true,
            suspend: 'yes',
            power_off: 'yes',
            suspend_supported: true,
            power_off_supported: true,
            unavailable_reason: null,
        });
        if (p === '/api/system/power/suspend' && post) {
            return j({ ok: true, status: 'simulated', action: 'suspend' });
        }
        if (p === '/api/system/power/power-off' && post) {
            return j({ ok: true, status: 'simulated', action: 'power-off' });
        }
        if (p === '/api/system/restore' && post) return j({ ok: true, installed_version: '0.9.11', stdout: 'Restore completed.\nUpdate completed: 0.9.11', restart_scheduled: true });

        // ── DSP ─────────────────────────────────────────────────────────
        if (p === '/api/dsp/presets') return j(dspPayload());
        if (p === '/api/dsp/presets/load' && post) {
            const name = String(body.preset_name || body.name || 'Direct');
            if (dspPresets.find(pr => pr.name === name)) dspActivePreset = name;
            syncDspToState();
            return j({ success: true, active_preset: dspActivePreset, message: 'Preset "' + dspActivePreset + '" loaded' });
        }
        if (p === '/api/dsp/presets/create-peq' && post) {
            const name = String(body.presetName || body.preset_name || '').trim();
            if (!name) return err('Preset name required', 400);
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({
                    name,
                    filename: name + '.json',
                    path: '/demo/presets/' + name + '.json',
                    source_presets: [],
                    peq: body.peq && body.peq.params ? { enabled: true, params: body.peq.params } : null,
                });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/create-with-ir' && post) {
            const name = String(body.preset_name || body.name || 'Convolver Preset').trim() || 'Convolver Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], convolver: true });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/combine' && post) {
            const name = String(body.presetName || 'Combined Preset').trim();
            const presetNames = Array.isArray(body.presetNames) && body.presetNames.length
                ? body.presetNames
                : [body.preset1, body.preset2].filter(Boolean);
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: presetNames });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/delete' && post) {
            const name = String(body.preset_name || body.name || '');
            const idx = dspPresets.findIndex(pr => pr.name === name);
            if (idx >= 0 && name !== 'Direct' && name !== 'Neutral') dspPresets.splice(idx, 1);
            if (dspActivePreset === name) dspActivePreset = 'Direct';
            syncDspToState();
            return j({ success: true });
        }
        if (p === '/api/dsp/compare' && post) { dspCompare = { ...body }; return j({ ok: true }); }
        if (p === '/api/dsp/extras' && post) {
            // Same merge vocabulary as the backend
            // (merge_effects_extras_from_json): camelCase from the app and
            // snake_case from the form posts, applied only when supplied.
            // The extras response mirrors save_dsp_extras: { status, extras }.
            const toBool = (value) => value === true || value === 'true' || value === 1;
            const pick = (...names) => {
                for (const name of names) {
                    if (body[name] !== undefined) return body[name];
                }
                return undefined;
            };
            const section = (key) => {
                dspExtras[key] = dspExtras[key] || {};
                return dspExtras[key];
            };
            const params = (key) => {
                const entry = section(key);
                entry.params = entry.params && typeof entry.params === 'object' ? entry.params : {};
                return entry.params;
            };
            let value = pick('limiterEnabled', 'limiter_enabled');
            if (value !== undefined) section('limiter').enabled = toBool(value);
            value = pick('headroomEnabled', 'headroom_enabled');
            if (value !== undefined) section('headroom').enabled = toBool(value);
            value = pick('headroomGainDb', 'headroom_gain_db');
            if (value !== undefined) params('headroom').gainDb = Number(value);
            value = pick('autogainEnabled', 'autogain_enabled');
            if (value !== undefined) section('autogain').enabled = toBool(value);
            value = pick('autogainTargetDb', 'autogain_target_db');
            if (value !== undefined) params('autogain').targetDb = Number(value);
            value = pick('loudnessEnabled', 'loudness_enabled');
            if (value !== undefined) section('loudness').enabled = toBool(value);
            value = pick('loudnessStrength', 'loudness_strength');
            if (value !== undefined) params('loudness').strength = Number(value);
            value = pick('loudnessFftSize', 'loudness_fft_size');
            if (value !== undefined) params('loudness').fftSize = Number(value);
            value = pick('delayEnabled', 'delay_enabled');
            if (value !== undefined) section('delay').enabled = toBool(value);
            value = pick('delayLeftMs', 'delay_left_ms');
            if (value !== undefined) params('delay').leftMs = Number(value);
            value = pick('delayRightMs', 'delay_right_ms');
            if (value !== undefined) params('delay').rightMs = Number(value);
            value = pick('bassEnabled', 'bass_enabled');
            if (value !== undefined) section('bass_enhancer').enabled = toBool(value);
            value = pick('bassAmount', 'bass_amount');
            if (value !== undefined) params('bass_enhancer').amount = Number(value);
            value = pick('toneEffectEnabled', 'tone_effect_enabled');
            if (value !== undefined) section('tone_effect').enabled = toBool(value);
            value = pick('toneEffectMode', 'tone_effect_mode');
            if (value !== undefined) section('tone_effect').mode = String(value);
            syncDspToState();
            return j({ status: 'ok', ok: true, extras: dspExtras });
        }
        if (p === '/api/dsp/extras' && !post) {
            return j({ status: 'ok', extras: dspExtras, excluded_presets: [] });
        }
        if (p === '/api/dsp/presets/import-json' && post) {
            const name = String(body.preset_name || body.name || 'Imported Preset').trim() || 'Imported Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [] });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/import-bundle' && post) {
            const name = String(body.preset_name || body.name || 'Imported Bundle').trim() || 'Imported Bundle';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [] });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/import-filter-dual' && post) {
            const name = String(body.preset_name || body.name || 'Dual Filter Preset').trim() || 'Dual Filter Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], convolver: true });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }
        if (p === '/api/dsp/presets/import-rew-peq' && post) {
            const name = String(body.preset_name || body.name || 'REW PEQ Preset').trim() || 'REW PEQ Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], peq: { enabled: true, params: { channelMode: 'dual', eqMode: 'IIR', leftBands: [], rightBands: [] } } });
            }
            syncDspToState();
            return j({ success: true, preset: { name } });
        }

        // ── Measurements ────────────────────────────────────────────────
        // Seeded with the .104 fixtures; demo sweeps reuse them by name.
        if (p === '/api/measurements') {
            const list = [];
            S.getSavedMeasurements().forEach(m => { if (!list.find(x => x.id === m.id)) list.push(m); });
            savedMeasurements.forEach(m => { if (!list.find(x => x.id === m.id)) list.push(m); });
            return j({
                measurements: list,
                storage: { used_bytes: 2000000, available_bytes: 100000000 },
                calibrations: [{ id: 'MM1CES_allein_00d.txt', filename: 'MM1CES_allein_00d.txt' }],
                house_curves: [],
                active_calibration_file_id: 'MM1CES_allein_00d.txt',
                measurement_settings: { selectedInputId: 'demo_mic', selectedInputKey: 'demo_mic', selectedMicInputChannel: '1', selectedReferenceInputChannel: '2', measurementSampleRate: '48000' },
                scope_note: 'Ready for the first measurement.',
            });
        }
        if (p === '/api/measurements/inputs') {
            return j({
                inputs: [{
                    id: 'demo_mic',
                    label: 'Demo Microphone',
                    note: 'simulated capture',
                    channels: 1,
                    supported_rates: [44100, 48000, 88200, 96000],
                    measurement_sample_rate: 48000,
                    node_name: 'demo_mic',
                    persistent_id: 'demo_mic',
                }, {
                    id: 'alsa_input.usb-MOTU_M4-00.analog-stereo',
                    label: 'alsa_input.usb-MOTU_M4-00.analog-stereo',
                    note: 'MOTU M4 analog inputs 1/2',
                    channels: 2,
                    supported_rates: [44100, 48000, 88200, 96000, 176400, 192000],
                    measurement_sample_rate: 48000,
                    node_name: 'alsa_input.usb-MOTU_M4-00.analog-stereo',
                    persistent_id: 'alsa_input.usb-MOTU_M4-00.analog-stereo',
                }],
                capture_available: true,
                selection: { input_id: 'demo_mic', persistent_id: 'demo_mic', configured: true },
                scope_note: 'Ready for the first measurement.',
            });
        }
        if (p === '/api/measurements/start' && post) {
            const role = String(body.measurement_role || 'single');
            const channel = String(body.channel || 'left');
            const id = S.startMeasurement({ role, channel, mode: outputMode.mode, seed: 1 + Math.random() * 50 });
            return j({ job: { id, job_kind: 'single', status: 'running', progress_pct: 0, message: 'Sweep starting…' } });
        }
        const measJobMatch = p.match(/^\/api\/measurements\/jobs\/([^/]+)$/);
        if (measJobMatch) {
            const payload = S.jobPayload(measJobMatch[1]);
            if (!payload) return err('Job not found');
            if (payload.status === 'completed' && payload.result && payload.result.measurement) {
                if (!savedMeasurements.find(m => m.id === payload.result.measurement.id)) savedMeasurements.unshift(payload.result.measurement);
            }
            return j({ job: payload });
        }
        const measCancel = p.match(/^\/api\/measurements\/jobs\/([^/]+)\/cancel$/);
        if (measCancel && post) { S.cancelJob(measCancel[1]); return j({ job: { id: measCancel[1], status: 'cancelled', message: 'Measurement cancelled.' } }); }
        if (p === '/api/measurements/save' && post) {
            const saved = [];
            if (Array.isArray(body.measurements)) {
                body.measurements.forEach(m => saved.push(S.addSavedMeasurement(m)));
            } else if (body.measurement) {
                saved.push(S.addSavedMeasurement(body.measurement));
            } else {
                saved.push(S.addSavedMeasurement(body));
            }
            return j({ measurements: saved });
        }
        const measDelete = p.match(/^\/api\/measurements\/([^/]+)$/);
        if (measDelete && method === 'DELETE') {
            savedMeasurements.filter(m => m.id === measDelete[1]).forEach(m => {
                const idx = savedMeasurements.indexOf(m);
                if (idx >= 0) savedMeasurements.splice(idx, 1);
            });
            S.deleteSavedMeasurement(measDelete[1]);
            return j({ ok: true });
        }
        if (p === '/api/measurements/merge' && post) {
            const name = String(body.name || 'Merged');
            const m = S.makeMeasurement({ name, id: 'demo_meas_merged_' + Date.now(), seed: 1 + Math.random() * 50 });
            return j({ measurement: S.addSavedMeasurement(m) });
        }
        if (p === '/api/measurements/settings' && post) return j({ ok: true });
        // Calibration + house-curve files: the demo ships the .104 mic
        // calibration as the selected file; uploads/deletes only mutate the
        // in-memory option list and echo the applier shape
        // ({ calibrations, active_calibration_file_id } /
        // { house_curves }) the real frontend consumes.
        S.demoCalibrationOptions = S.demoCalibrationOptions || [{ id: 'MM1CES_allein_00d.txt', filename: 'MM1CES_allein_00d.txt' }];
        S.demoHouseCurveOptions = S.demoHouseCurveOptions || [];
        S.demoActiveCalibrationId = (S.demoActiveCalibrationId === undefined) ? 'MM1CES_allein_00d.txt' : S.demoActiveCalibrationId;
        function demoCalibrationState() {
            return { calibrations: S.demoCalibrationOptions.slice(), active_calibration_file_id: S.demoActiveCalibrationId || '' };
        }
        if (p === '/api/measurements/calibrations' && post) {
            const filename = String(body.calibration_file_name || body.filename || body.name || 'uploaded-calibration.txt');
            const id = filename;
            if (!S.demoCalibrationOptions.find(o => o.id === id)) S.demoCalibrationOptions.push({ id, filename });
            S.demoActiveCalibrationId = id;
            return j(demoCalibrationState());
        }
        if (p === '/api/measurements/calibrations') return j(demoCalibrationState());
        if (p === '/api/measurements/calibrations/active' && (method === 'PATCH' || post)) {
            const id = String(body.calibration_file_id || '');
            S.demoActiveCalibrationId = (!id || S.demoCalibrationOptions.find(o => o.id === id)) ? id : '';
            return j(demoCalibrationState());
        }
        const demoCalCrud = p.match(/^\/api\/measurements\/calibrations\/([^/]+)(\/export)?$/);
        if (demoCalCrud) {
            if (demoCalCrud[2]) return j({ redirect: './demo/data/measurements.js' });
            if (method === 'DELETE') {
                S.demoCalibrationOptions = S.demoCalibrationOptions.filter(o => o.id !== demoCalCrud[1]);
                if (S.demoActiveCalibrationId === demoCalCrud[1]) S.demoActiveCalibrationId = '';
                return j(demoCalibrationState());
            }
        }
        function demoHouseCurveState(extra) {
            return { house_curves: S.demoHouseCurveOptions.slice(), ...(extra || {}) };
        }
        if (p === '/api/measurements/house-curves' && post) {
            const filename = String(body.house_curve_file_name || body.filename || body.name || 'house-curve.txt');
            const id = 'demo-house-' + Date.now();
            S.demoHouseCurveOptions.push({ id, filename });
            return j(demoHouseCurveState({ uploaded_house_curve_id: id }));
        }
        if (p === '/api/measurements/house-curves') return j(demoHouseCurveState());
        const demoHouseCrud = p.match(/^\/api\/measurements\/house-curves\/([^/]+)(\/export)?$/);
        if (demoHouseCrud) {
            if (demoHouseCrud[2]) return j({ redirect: './demo/data/measurements.js' });
            if (method === 'DELETE') {
                S.demoHouseCurveOptions = S.demoHouseCurveOptions.filter(o => o.id !== demoHouseCrud[1]);
                return j(demoHouseCurveState());
            }
        }
        if (p === '/api/measurements/spl-calibration') return j(S.splCalibrationPayload());
        if (p === '/api/measurements/spl-calibration/noise' && post) {
            if (body.enabled) {
                S.spl.noiseActive = true;
                return j({ status: 'playing', settle_seconds: 1.0, average_seconds: 3.0 });
            }
            S.spl.noiseActive = false;
            return j({ status: 'stopped' });
        }
        if (p === '/api/measurements/spl-calibration/automatic' && post) {
            const result = S.splMeasureAutomatic();
            return j(result);
        }
        if (p === '/api/measurements/spl-calibration/apply' && post) {
            const result = S.splApplyCalibration(body.measured_spl_db);
            if (result.error) return err(result.error, 400);
            return j(result);
        }
        if (p === '/api/measurements/auto-sub-optimize/start' && post) {
            const id = 'demo_autosub_' + (++autoSubSeq);
            // The real frontend sends the selected target curve as a JSON
            // snapshot in the FormData; the demo only needs its key to pick
            // the matching real .104 run (neutral/bk/harman, house:* falls
            // back to the mode default).
            let targetKey = 'neutral';
            const rawSnapshot = body.target_curve_snapshot;
            if (typeof rawSnapshot === 'string' && rawSnapshot.trim()) {
                try {
                    const snapshot = JSON.parse(rawSnapshot);
                    if (snapshot && typeof snapshot.key === 'string' && snapshot.key.trim()) {
                        targetKey = snapshot.key.trim().toLowerCase().startsWith('house:')
                            ? 'neutral'
                            : snapshot.key.trim().toLowerCase();
                    }
                } catch (e) { /* keep the default */ }
            }
            autoSubJobs[id] = { id, mode: normalizeOutputModeName(outputMode.mode), targetKey, startedAt: Date.now(), status: 'running' };
            return j({ job: { id, status: 'queued', message: 'Auto Sub Optimize: queued' } });
        }
        const autoSubJob = p.match(/^\/api\/measurements\/auto-sub-optimize\/jobs\/([^/]+)$/);
        if (autoSubJob) {
            const payload = autoSubJobPayload(autoSubJob[1]);
            if (!payload) return err('Job not found');
            return j({ job: payload });
        }
        const autoSubCancel = p.match(/^\/api\/measurements\/auto-sub-optimize\/jobs\/([^/]+)\/cancel$/);
        if (autoSubCancel && post) {
            const id = autoSubCancel[1];
            if (autoSubJobs[id]) autoSubJobs[id].status = 'cancelled';
            return j({ job: { id, status: 'cancelled', message: 'Auto Sub Optimize cancelled.' } });
        }
        if (p === '/api/measurements/lr-repeat/start' && post) {
            const id = S.startLrRepeatMeasurement({ base_name: body.base_name });
            return j({ job: { id, job_kind: 'lr-repeat', status: 'running', progress_pct: 0, message: 'L/R repeat queued.' } });
        }
        if (p === '/api/power/measurement-heartbeat' && post) return j({ ok: true });
        if (p === '/api/debug/21-runtime-state' && post) return j({ ok: true });
        if (p === '/api/hardware/status') return j({ available: false, connected: false, device: null, status: {}, notes: [] });
        if (p === '/api/hardware/auto/off' && post) return j({ ok: true });
        if (p === '/api/hardware/auto/on' && post) return j({ ok: true });
        if (p === '/api/hardware/input/rca' && post) return j({ ok: true });
        if (p === '/api/hardware/input/xlr' && post) return j({ ok: true });
        if (p === '/api/hardware/input/press' && post) return j({ ok: true });
        if (p === '/api/download' || p === '/api/download/status') return j({ status: 'idle' });
        if (p === '/api/download/cancel' && post) return j({ ok: true });
        if (p === '/api/stream/info') return j({ codec: 'FLAC', bitrate_kbps: 1411, sample_rate: 48000 });
        if (p === '/api/certificate/local-root') {
            // The demo box has no real TLS certificate; serve the clearly
            // labeled placeholder so the Settings download link works.
            return j(DEMO_CERT_PEM, 200, {
                'Content-Type': 'application/x-pem-file',
                'Content-Disposition': 'attachment; filename="fxroute-demo-certificate.crt"',
            });
        }

        // ── fallthrough ─────────────────────────────────────────────────
        if (p.startsWith('/api/')) console.warn('[demo] unmapped API call:', method, p);
        return origFetch.apply(this, arguments);
    };

    window.FXROUTE_DEMO_API = {
        playbackPayload: S.getPlayback,
        easyeffectsStatus: dspPayload,
        autoSubJobPayload,
    };
})();