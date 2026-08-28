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
    let dspPresets = [
        { name: 'Direct', filename: 'Direct.json', path: '/demo/presets/Direct.json', source_presets: [] },
        { name: 'Neutral', filename: 'Neutral.json', path: '/demo/presets/Neutral.json', source_presets: [] },
        { name: 'Room Curve', filename: 'Room-Curve.json', path: '/demo/presets/Room-Curve.json', source_presets: [] },
        { name: 'PEQ — Vocal Boost', filename: 'PEQ-Vocal-Boost.json', path: '/demo/presets/PEQ-Vocal-Boost.json', source_presets: [] },
    ];
    let dspActivePreset = 'Direct';
    let dspExtras = {
        limiter: { enabled: false, params: {} },
        headroom: { enabled: false, params: { gainDb: -3 } },
        delay: { enabled: false, params: { leftMs: 0, rightMs: 0 } },
        bass_enhancer: { enabled: false, params: { amount: 0 } },
        autogain: { enabled: false, params: { targetDb: -12 } },
        loudness: { enabled: false, params: { strength: 10, fftSize: 4096, volumeDb: 0 } },
        tone_effect: { enabled: false, mode: 'crystalizer' },
    };
    let dspCompare = { presetA: '', presetB: '', activeSide: null };

    function syncDspToState() {
        const headroom = dspExtras.headroom;
        S.setDspSnapshot({
            headroomDb: headroom.enabled ? Number(headroom.params && headroom.params.gainDb) || 0 : 0,
            extras: dspExtras,
            presets: dspPresets,
            activePreset: dspActivePreset,
        });
    }

    function dspPayload() {
        return {
            available: true,
            presets: dspPresets,
            preset_count: dspPresets.length,
            active_preset: dspActivePreset,
            irs: [
                { name: 'Studio A — Large Live Room', basename: 'studio-a-live.irs', path: '/demo/irs/studio-a-live.irs', size: 128410 },
                { name: 'Club Room — 200 Capacity', basename: 'club-room-200.irs', path: '/demo/irs/club-room-200.irs', size: 96420 },
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

    // ── Music libraries ─────────────────────────────────────────────────
    const musicLibraries = {
        active_id: 'local',
        active_type: 'local',
        libraries: [
            { id: 'local', label: 'Local', type: 'local' },
            { id: 'demo-nas', label: 'NAS Music', type: 'smb' },
        ],
    };

    // ── Measurements (saved list seeded with real fixtures) ─────────────
    const savedMeasurements = S.getSavedMeasurements().slice();

    // ── Helpers ─────────────────────────────────────────────────────────
    function j(data, status = 200) {
        if (status === 302) {
            // Image-like redirect endpoints: the browser follows the Location
            // header only when the response really redirects; the demo serves
            // covers via the static pool instead.
            return Promise.resolve({
                ok: true,
                status: 200,
                url: data.redirect,
                json: () => Promise.resolve({}),
                text: () => Promise.resolve(''),
            });
        }
        return Promise.resolve({
            ok: status >= 200 && status < 300,
            status,
            json: () => Promise.resolve(data),
            text: () => Promise.resolve(JSON.stringify(data)),
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

    function emitState() {
        window.__demoBroadcast && window.__demoBroadcast('playback', S.getPlayback());
    }

    // ── Auto Sub Optimize simulation ────────────────────────────────────
    // A plausible multi-stage run: baseline sweep, per-sub coarse scans, a
    // fine scan and the combined matrix, then a complete mode-aware result
    // (2.2 / 2.2-stereo align both subs, 2.1 aligns the single sub). Stages
    // advance by elapsed time so every poll shows further progress instead
    // of jumping straight to a finished job.
    const autoSubJobs = {};
    let autoSubSeq = 0;

    function autoSubBaseline(mode) {
        return S.makeMeasurement({ name: 'AutoSub Baseline', channel: 'left', seed: 21, mode });
    }
    function autoSubConfirmation(mode) {
        return S.makeMeasurement({ name: 'AutoSub Confirmation', channel: 'right', seed: 22, mode });
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

    function autoSubResult(job) {
        const mode = job.mode;
        // The measurements travel both at job level (live baseline push while
        // the run is in progress) and inside result (final graph display).
        const baseline = autoSubBaseline(mode);
        const confirmation = autoSubConfirmation(mode);
        const base = {
            id: job.id,
            status: 'completed',
            message: 'Auto Sub Optimize completed.',
            target_curve: { label: 'Flat Target Curve' },
            baseline_measurement: baseline,
            confirmation_measurement: confirmation,
        };
        if (mode === 'subwoofer-2.2' || mode === 'subwoofer-2.2-stereo') {
            const result = {
                mode,
                applied: true,
                baseline_measurement: baseline,
                confirmation_measurement: confirmation,
                original_sub1_alignment_ms: 2.8,
                original_sub2_alignment_ms: 2.45,
                applied_sub1_alignment_ms: 3.4,
                applied_sub2_alignment_ms: 3.1,
                left_score_pct: 84.6,
                right_score_pct: 82.1,
                overall_score_pct: 83.4,
                winner: { score_pct: 83.0, score_L_pct: 84.6, score_R_pct: 82.1, overall_score_pct: 83.4 },
                sub1_coarse_winner: { delay_ms: 3.4, score_pct: 84.6 },
                sub2_coarse_winner: { delay_ms: 3.1, score_pct: 82.1 },
                derived_main_delay_ms: 0.0,
                derived_sub1_delay_ms: 3.4,
                derived_sub2_delay_ms: 3.1,
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
        const result = {
            mode,
            applied: true,
            baseline_measurement: baseline,
            confirmation_measurement: confirmation,
            original_alignment_ms: 2.8,
            applied_alignment_ms: 3.4,
            suggested_alignment_ms: 3.4,
            left_score_pct: 84.6,
            right_score_pct: 82.1,
            overall_score_pct: 83.4,
            winner: { score_pct: 83.0, score_L_pct: 84.6, score_R_pct: 82.1, overall_score_pct: 83.4 },
            coarse_winner: { delay_ms: 3.4, score_pct: 84.6 },
            runner_up: { delay_ms: 6.2, score_pct: 71.3 },
            fine_winner: { delay_ms: 3.4, score_pct: 84.6 },
            fine_scan: { triggered: true, status: 'completed' },
            confidence: 'high',
        };
        applyAutoSubResult(result);
        return { ...base, result };
    }

    // elapsedMs is injectable so the behavior test can fast-forward a run.
    function autoSubJobPayload(id, elapsedMs) {
        const job = autoSubJobs[id];
        if (!job) return null;
        if (job.status === 'cancelled') {
            return { id, status: 'cancelled', message: 'Auto Sub Optimize cancelled.' };
        }
        const mode = job.mode;
        const elapsed = (elapsedMs != null) ? elapsedMs : (Date.now() - job.startedAt);
        const isStereoBass = mode === 'subwoofer-2.2-stereo';
        const is22 = mode === 'subwoofer-2.2' || isStereoBass;
        const sub1Label = isStereoBass ? 'Left Sub' : 'Sub 1';
        const sub2Label = isStereoBass ? 'Right Sub' : 'Sub 2';
        const base = { id, target_curve: { label: 'Flat Target Curve' } };
        const withBaseline = () => ({ ...base, baseline_measurement: autoSubBaseline(mode) });

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
                    favicon: '',
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

        // ── Library ─────────────────────────────────────────────────────
        if (p === '/api/tracks') return j(lib.tracks);
        if (p === '/api/albums') {
            const q = String(query.get('query') || '').toLowerCase();
            return j(q ? lib.albums.filter(a => (a.name + ' ' + a.artist + ' ' + (a.genres || []).join(' ')).toLowerCase().includes(q)) : lib.albums);
        }
        if (p === '/api/playlists') {
            if (post) {
                const name = String(body.name || '').trim();
                const trackIds = Array.isArray(body.track_ids) ? body.track_ids : [];
                if (!name) return err('Playlist name required', 400);
                const id = 'playlist_' + Date.now();
                lib.playlists.push({ id, name, track_ids: trackIds, track_count: trackIds.length });
                return j({ status: 'ok', playlist: { id, name, track_ids: trackIds, track_count: trackIds.length } });
            }
            return j(lib.playlists);
        }
        const playlistCrud = p.match(/^\/api\/playlists\/([^/]+)(\/export)?$/);
        if (playlistCrud) {
            const id = playlistCrud[1];
            if (playlistCrud[2]) return err('Not found', 404);
            if (method === 'DELETE') {
                const idx = lib.playlists.findIndex(pl => pl.id === id);
                if (idx >= 0) lib.playlists.splice(idx, 1);
                return j({ status: 'ok', deleted: id });
            }
            return err('Playlist not found');
        }
        const albumTracksMatch = p.match(/^\/api\/albums\/([^/]+)\/tracks$/);
        if (albumTracksMatch) {
            const album = lib.albums.find(a => a.id === albumTracksMatch[1]);
            if (!album) return err('Album not found');
            return j(lib.tracks.filter(t => t.album === album.name));
        }
        const albumFavMatch = p.match(/^\/api\/albums\/([^/]+)\/favorite$/);
        if (albumFavMatch) {
            const album = lib.albums.find(a => a.id === albumFavMatch[1]);
            if (!album) return err('Album not found');
            album.favorite = !!body.favorite;
            return j({ status: 'ok', album_id: album.id, favorite: album.favorite });
        }
        const trackFavMatch = p.match(/^\/api\/tracks\/([^/]+)\/favorite$/);
        if (trackFavMatch) {
            const track = lib.tracks.find(t => t.id === trackFavMatch[1]);
            if (!track) return err('Track not found');
            track.favorite = !!body.favorite;
            return j({ status: 'ok', track_id: track.id, favorite: track.favorite });
        }
        const albumCoverMatch = p.match(/^\/api\/albums\/([^/]+)\/cover$/);
        if (albumCoverMatch) {
            const album = lib.albums.find(a => a.id === albumCoverMatch[1]);
            const redirect = album ? album.coverUrl : lib.demoImage('album:' + (albumCoverMatch[1] || 'x'));
            return j({ redirect });
        }
        const trackCoverMatch = p.match(/^\/api\/tracks\/cover\/([^/]+)$/);
        if (trackCoverMatch) {
            const track = lib.tracks.find(t => t.id === trackCoverMatch[1]);
            return j({ redirect: track ? track.cover_url : lib.demoImage('track:' + (trackCoverMatch[1] || 'x')) });
        }
        const trackCoverInfoMatch = p.match(/^\/api\/tracks\/cover-info\/([^/]+)$/);
        if (trackCoverInfoMatch) {
            const track = lib.tracks.find(t => t.id === trackCoverInfoMatch[1]);
            return j({ cover_url: track ? track.cover_url : '', cover_available: !!track });
        }
        const albumDiscover = p.match(/^\/api\/albums\/([^/]+)\/discover$/);
        if (albumDiscover) {
            const others = lib.albums.filter(a => a.id !== albumDiscover[1]).slice(0, 6);
            return j({ album_id: albumDiscover[1], items: others.map(a => ({ id: a.id, name: a.name, artist: a.artist, cover_url: a.coverUrl, kind: 'album' })), source: 'demo', cached: true, error: '' });
        }
        if (p === '/api/smart/top-tracks') return j(lib.tracks.slice(0, 40));
        if (p === '/api/library/status') return j({ scanning: false, tracks_found: lib.tracks.length, files_seen: 0 });
        if (p === '/api/library/refresh' && post) return j({ status: 'ok' });
        if (p === '/api/library/folders/delete' && post) return j({ status: 'ok' });
        if (p === '/api/tracks/delete' && post) return j({ status: 'ok' });
        if (p === '/api/tracks/download' && post) return j({ status: 'ok' });

        // ── Streaming providers ─────────────────────────────────────────
        if (p === '/api/streaming/providers') {
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', installed: true, available: true, authenticated: true, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', installed: true, available: true, authenticated: true, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', installed: true, available: true, authenticated: true, connected: true, capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
        if (p === '/api/streaming/providers/discovery') {
            // Same shape as the backend's streaming.discover_providers(): the
            // current frontend builds the provider tabs from this endpoint.
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: true, connected: true, backend: 'spotifyd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: true, connected: true, backend: 'qbzd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: true, connected: true, backend: 'tidalapi', capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
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
        const qobuzCmd = p.match(/^\/api\/streaming\/qobuz\/([a-z]+)$/);
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
            return j({ ...album, tracks: album.tracks });
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
                enrichment: { about: artist.name + ' is part of the FXRoute demo catalog. The simulated TIDAL library is fully browsable.' },
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
                outputMode.routing = { status: mode === 'stereo' ? 'Out 1/2 Main' : 'Out 1/2 Main · Out 3/4 Sub' };
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
            if (post) return j({ mode: 'app-playback', modes: [{ key: 'app-playback', label: 'App playback', selectable: true }], default_input: null, selected_input: null, current_input: null, inputs: [], bluetooth: {}, notes: [], pending: false });
            return j({ mode: 'app-playback', modes: [{ key: 'app-playback', label: 'App playback', selectable: true }], default_input: null, selected_input: null, current_input: null, inputs: [], bluetooth: {}, notes: [], pending: false });
        }
        if (p === '/api/music-libraries') return j(musicLibraries);
        if (p === '/api/music-libraries/manual' && post) {
            const url = String(body.url || '');
            const entry = { id: 'demo-nas-' + Date.now(), label: url.split('/').filter(Boolean).pop() || 'NAS', type: 'smb' };
            if (!musicLibraries.libraries.find(l => l.id === entry.id)) musicLibraries.libraries.push(entry);
            return j({ ...musicLibraries, entry });
        }
        if (p === '/api/music-libraries/select' && post) {
            const id = String(body.id || '');
            const entry = musicLibraries.libraries.find(l => l.id === id);
            if (entry) { musicLibraries.active_id = entry.id; musicLibraries.active_type = entry.type; }
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
            const merge = (key) => {
                const enabledKey = key + 'Enabled';
                if (body[enabledKey] === undefined && body.key !== key) return;
                dspExtras[key].enabled = !!body[enabledKey];
                if (key === 'headroom') dspExtras[key].params = { gainDb: Number(body.headroomGainDb ?? dspExtras[key].params.gainDb) };
                if (key === 'autogain') dspExtras[key].params = { targetDb: Number(body.autogainTargetDb ?? dspExtras[key].params.targetDb) };
                if (key === 'loudness') dspExtras[key].params = { ...dspExtras[key].params, strength: Number(body.loudnessStrength ?? dspExtras[key].params.strength), fftSize: Number(body.loudnessFftSize ?? dspExtras[key].params.fftSize) };
                if (key === 'delay') dspExtras[key].params = { ...dspExtras[key].params, leftMs: Number(body.delayLeftMs ?? 0), rightMs: Number(body.delayRightMs ?? 0) };
                if (key === 'bass_enhancer') dspExtras[key].params = { ...dspExtras[key].params, amount: Number(body.bassAmount ?? 0) };
                if (key === 'tone_effect') dspExtras[key].mode = String(body.toneEffectMode ?? 'crystalizer');
            };
            ['limiter', 'headroom', 'autogain', 'loudness', 'delay', 'bass_enhancer', 'tone_effect'].forEach(merge);
            syncDspToState();
            return j({ ok: true, extras: dspExtras });
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
        if (p === '/api/measurements') {
            const list = [];
            S.getSavedMeasurements().forEach(m => { if (!list.find(x => x.id === m.id)) list.push(m); });
            savedMeasurements.forEach(m => { if (!list.find(x => x.id === m.id)) list.push(m); });
            return j({
                measurements: list,
                storage: { used_bytes: 2000000, available_bytes: 100000000 },
                calibrations: [],
                house_curves: [],
                active_calibration_file_id: '',
                measurement_settings: { selectedInputId: 'demo_mic', selectedInputKey: 'demo_mic', selectedMicInputChannel: '1', measurementSampleRate: '48000' },
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
        if (p === '/api/measurements/calibrations' && post) return j({ ok: true });
        if (p === '/api/measurements/calibrations') return j({ calibrations: [] });
        if (p === '/api/measurements/house-curves' && post) return j({ ok: true });
        if (p === '/api/measurements/house-curves') return j({ house_curves: [] });
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
            autoSubJobs[id] = { id, mode: normalizeOutputModeName(outputMode.mode), startedAt: Date.now(), status: 'running' };
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
        if (p === '/api/certificate/local-root') return j({}, 404);

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