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
        tracks: new Set(['t_album_01_t1', 't_album_01_t2', 't_album_02_t1', 't_album_03_t1', 't_album_04_t1', 't_album_05_t2', 't_album_07_t1', 't_album_09_t1', 't_album_12_t2', 't_album_14_t1', 't_album_16_t1', 't_album_18_t1']),
        albums: new Set(['t_album_01', 't_album_02', 't_album_04', 't_album_05', 't_album_07', 't_album_09', 't_album_12', 't_album_14', 't_album_16', 't_album_18']),
        artists: new Set(['t_artist_01', 't_artist_03', 't_artist_04', 't_artist_05', 't_artist_07', 't_artist_09', 't_artist_12', 't_artist_14', 't_artist_16', 't_artist_18']),
        playlists: new Set(['t_playlist_01', 't_playlist_03', 't_playlist_04', 't_playlist_05', 't_playlist_06', 't_playlist_07', 't_playlist_08']),
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
        presetEntry('Co LR Min Neutral 20-1283Hz -3dB', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 20-12000Hz -3dB 044321', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 30-10000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 30-12000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR HybAlign BK 30-3000Hz -7dB', { convolver: true }),
        presetEntry('Conv LR HybAlign Custom-House-Curve-1 30-12001Hz -3dB 134157', { convolver: true }),
        presetEntry('Conv LR HybAlign Harman 30-12001Hz -2.5dB 134143', { convolver: true }),
        presetEntry('Conv LR HybAlign Neutral 30-12000Hz -2dB 115915', { convolver: true }),
        presetEntry('Conv LR HybAlign Neutral 30-12000Hz -2dB 124911', { convolver: true }),
        presetEntry('Conv LR HybAlign Neutral 30-12001Hz -2dB 134132', { convolver: true }),
        presetEntry('Conv LR Min Gentle-bass-shelf-house-curve 30-250Hz -4dB 125139', { convolver: true }),
        presetEntry('Conv LR MinAlign Harman 30-300Hz -7dB', { convolver: true }),
        presetEntry('Conv R Lin Neutral 20-250Hz -4dB 140654', { convolver: true }),
        presetEntry('PEQ', { peq: { enabled: true, params: { channelMode: 'dual', eqMode: 'IIR', leftBands: [], rightBands: [] } } }),
        presetEntry('PEQ LR Measurement 2f 0tewt', { peq: { enabled: true, params: { channelMode: 'dual', eqMode: 'IIR', leftBands: [], rightBands: [] } } }),
    ];
    let dspActivePreset = 'Conv LR HybAlign BK 20-12000Hz -3dB 044321';
    let dspExtras = {
        limiter: { enabled: true, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 20.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
        headroom: { enabled: false, params: { gainDb: -3 } },
        delay: { enabled: false, params: { leftMs: 0, rightMs: 0 } },
        bass_enhancer: { enabled: false, params: { amount: 0, harmonics: 8.5, scope: 100.0, blend: 0.0 } },
        autogain: { enabled: false, params: { targetDb: -18 } },
        loudness: { enabled: false, params: { strength: 2, fftSize: 16384, volumeDb: 0 } },
        tone_effect: { enabled: false, mode: 'crystalizer' },
    };
    // The A/B compare block mirrors the seeded Global bank slots, so the card
    // and the legacy compare payload never name different presets. The chain
    // itself starts on the .104 active preset.
    let dspCompare = { presetA: 'Neutral', presetB: 'Conv LR HybAlign BK 30-3000Hz -7dB', activeSide: 'B' };

    // Audible offset for the meter sim: only the +3/+6 dB filter
    // presets lift the visible level (their real chains hold a broadband
    // gain stage); headroom cuts it 1:1 (the real headroom stage sits
    // before the monitor tap). The limiter needs no offset entry: it caps
    // the tapped signal at its threshold instead of shifting it.
    function presetMeterGainDb(name) {
        if (name === '+3') return 3;
        if (name === '+6') return 6;
        return 0;
    }

    function demoMeterOffsetDb() {
        let offset = presetMeterGainDb(dspActivePreset);
        const extras = dspExtras || {};
        if (extras.headroom && extras.headroom.enabled) {
            offset += Number(extras.headroom.params && extras.headroom.params.gainDb) || 0;
        }
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
        S.setDspSnapshot({
            meterOffsetDb: demoMeterOffsetDb(),
            limiterEnabled: !!(dspExtras.limiter && dspExtras.limiter.enabled),
            extras: dspExtras,
            presets: dspPresets,
            activePreset: dspActivePreset,
        });
    }
    syncDspToState();

    function dspPayload() {
        // The IR pool mirrors the .104 kernel stock, so a preset that carries a
        // convolver always finds its impulse response.
        const irs = dspPresets.filter(pr => pr.convolver).map(pr => ({
            name: pr.name, basename: pr.name, path: '/demo/irs/' + pr.name + '.irs', size: 262644,
        }));
        irs.push({
            name: 'Conv R Lin Neutral 20-250Hz -4dB 140654.wav', basename: 'Conv R Lin Neutral 20-250Hz -4dB 140654',
            path: '/demo/irs/Conv R Lin Neutral 20-250Hz -4dB 140654.wav', size: 1048576,
        });
        return {
            available: true,
            presets: dspPresets,
            preset_count: dspPresets.length,
            active_preset: dspActivePreset,
            irs,
            global_extras: dspExtras,
            global_extras_excluded_presets: [],
            compare: dspCompare,
            mode: 'native',
            paths: {},
        };
    }

    // ── Audio output model ──────────────────────────────────────────────
    // Crossover of the seeded demo system: mains split into two ways at
    // 3 kHz, subs fed by an 80 Hz bass-management split.
    const DEMO_CROSSOVER_HZ = 3000;
    const DEMO_BASS_HZ = 80;
    const SCARLETT_TIERS = [
        { id: '18ch', channels: 18, rates: [44100, 48000], probe_rate: 48000 },
        { id: '14ch', channels: 14, rates: [88200, 96000], probe_rate: 96000 },
        { id: '10ch', channels: 10, rates: [176400, 192000], probe_rate: 192000 },
    ];
    const SCARLETT_KEY = 'alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output';
    let scarlettTierId = '18ch';
    function scarlettTier() { return SCARLETT_TIERS.find(t => t.id === scarlettTierId) || SCARLETT_TIERS[0]; }
    function scarlettDeviceRates() {
        const rates = [];
        for (const tier of SCARLETT_TIERS) rates.push(...tier.rates);
        return [...new Set(rates)].sort((a, b) => a - b);
    }
    function scarlettNativeTierForRate(rate) {
        return SCARLETT_TIERS.find(t => t.rates.includes(rate)) || null;
    }
    const ROUTING_SIGNALS = ['Off', 'Main L', 'Main R', 'Sub 1', 'Sub 2'];
    const routingStore = {};
    function routingAssignments(key, channels) {
        const saved = routingStore[key];
        const base = Array.isArray(saved) ? saved.slice() : [1, 2, 3, 4];
        while (base.length < channels) base.push(0);
        return {
            assignments: base.slice(0, channels),
            inactive: base.map((v, i) => (i >= channels && v ? i + 1 : 0)).filter(Boolean),
            full: base,
        };
    }
    const OUTPUTS = [
        { key: 'alsa_output.pci-0000_00_1f.3.analog-stereo', name: 'Built-in Audio', label: 'Built-in Audio', description: 'Analog Stereo', channels: 2, active_rate: 48000, selectable: true, default: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000] },
        { key: 'alsa_output.usb-DEMO_DAC-00.analog-stereo', name: 'Demo USB DAC', label: 'Demo USB DAC', description: 'Hi-Res USB Audio', channels: 2, active_rate: 96000, selectable: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000] },
        { key: 'alsa_output.usb-MOTU_M4-00.analog-surround-40', name: 'MOTU M4', label: 'MOTU M4', description: '4-Channel USB Audio Interface', channels: 4, active_rate: 48000, selectable: true, supported_rates: [44100, 48000, 88200, 96000, 176400, 192000] },
        { key: SCARLETT_KEY, name: 'Focusrite Scarlett 16i16 4th Gen Pro', label: 'Focusrite Scarlett 16i16 4th Gen Pro', description: '18-Channel USB Audio Interface', channels: 18, active_rate: 48000, selectable: true, supported_rates: [44100, 48000] },
    ];
    // The demo starts on the 18-channel Scarlett, the .104 system: a 2-way
    // crossover plus two subs fits its 6 routed outputs, and its multichannel
    // capture input is the one that offers the split Electrical Ref L/R pair.
    let selectedOutputKeyCache = SCARLETT_KEY;
    function selectedOutput() {
        return OUTPUTS.find(o => o.key === selectedOutputKeyCache) || OUTPUTS[0];
    }

    // ── Output-mode per-device memory (mirrors the real backend) ─────────
    // A deliberate device switch derives the mode from the actually
    // available output channel count, never from the device name: a
    // subwoofer mode on a device with fewer than 4 channels falls back to
    // Stereo, and a mode remembered for this device is restored when the
    // device can carry it. Like the backend's persisted per-device memory,
    // a mode is only remembered when it is actually applied for that device
    // (mode change or an explicit mode request), never on a switch whose
    // effective mode already matches the current one.
    const OUTPUT_MODES = ['stereo', 'subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'];
    const SUBWOOFER_MODES = ['subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'];
    const deviceOutputModes = { [OUTPUTS[2].key]: 'subwoofer-2.2' };
    function isSubwooferOutputModeName(mode) {
        return SUBWOOFER_MODES.includes(String(mode || ''));
    }
    function outputModeLabel(mode) {
        if (mode === 'subwoofer-2.1') return '2.1';
        if (mode === 'subwoofer-2.2-stereo') return '2.2 Stereo Bass';
        if (mode === 'subwoofer-2.2') return '2.2';
        return 'Stereo';
    }
    function routingStatusForOutputMode(mode) {
        if (mode === 'stereo') return 'Out 1/2 Main';
        if (mode === 'subwoofer-2.2-stereo') return 'Out 1/2 Main · Out 3 Left Sub · Out 4 Right Sub';
        return 'Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2';
    }

    // ── Measurement capture model ───────────────────────────────────────
    // The demo reports the capture side that belongs to the selected output
    // device, exactly like the real machine pairs the Focusrite Scarlett 16i16
    // multichannel output with its 18-channel capture input. The UMIK-1 USB
    // measurement microphone stays manually selectable in the measurement
    // setup; captures with three or more channels (MOTU M4 with its line
    // inputs 3/4, Scarlett 16i16) show the split Electrical Ref L / R view,
    // smaller captures keep the previous single shared reference. The split
    // comes from the unchanged app.js logic, which switches on the capture
    // channel count.
    const SCARLETT_OUTPUT_KEY = 'alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output';
    const UMIK1_CAPTURE_INPUT = {
        id: 'demo_mic',
        label: 'UMIK-1',
        note: 'UMIK-1 USB measurement microphone (simulated)',
        channels: 1,
        supported_rates: [44100, 48000, 88200, 96000],
        sample_rate: 48000,
        measurement_sample_rate: 48000,
        node_name: 'demo_mic',
        persistent_id: 'demo_mic',
    };
    const STEREO_CAPTURE_INPUT = {
        id: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
        // Labels mirror the real capture list: the PipeWire node name, like the
        // .104 `alsa_input.usb-Focusrite_Scarlett_16i16...-multichannel-input`.
        label: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
        note: 'MOTU M4 analog inputs 1-4',
        channels: 4,
        supported_rates: [44100, 48000, 88200, 96000, 176400, 192000],
        sample_rate: 48000,
        measurement_sample_rate: 48000,
        node_name: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
        persistent_id: 'alsa_input.usb-MOTU_M4-00.analog-surround-40',
    };
    const MULTICHANNEL_CAPTURE_INPUT = {
        id: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
        label: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
        note: 'Scarlett 16i16 multichannel capture (18 channels)',
        channels: 18,
        supported_rates: [44100, 48000],
        sample_rate: 48000,
        measurement_sample_rate: 48000,
        node_name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
        persistent_id: 'device-serial:Focusrite_Scarlett_16i16_4th_Gen|node-name:alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
    };
    const CAPTURE_INPUTS = [UMIK1_CAPTURE_INPUT, STEREO_CAPTURE_INPUT, MULTICHANNEL_CAPTURE_INPUT];
    // The real store's scope note: the sweep is a host-local measurement
    // through the active output and the selected microphone, independent of
    // the active DSP preset. The demo serves the same wording.
    const DEMO_MEASUREMENT_SCOPE_NOTE = 'FXRoute measures with a host-local sweep through the active PipeWire output and selected microphone input. The result is a practical response trace for comparison and PEQ drafting, independent of the active DSP preset.';
    function captureInputForOutputKey(key) {
        return key === SCARLETT_OUTPUT_KEY ? MULTICHANNEL_CAPTURE_INPUT : STEREO_CAPTURE_INPUT;
    }
    let measurementCaptureInput = captureInputForOutputKey(selectedOutputKeyCache);
    // Electrical reference channels the demo persists for the selected capture.
    // Captures with three or more channels expose the split Ref L / R view on
    // their line inputs; smaller captures keep the single shared reference.
    // The defaults stay inside the capture's own channel count: the Scarlett
    // lands on the left/right line inputs 7/8, which is what the real
    // alignment run used, and the 4-channel MOTU M4 on 3/4.
    function measurementReferenceSettings() {
        const channels = Number(measurementCaptureInput.channels) || 0;
        if (channels >= 8) {
            return {
                selectedReferenceInputChannel: '8',
                selectedReferenceInputChannelLeft: '7',
                selectedReferenceInputChannelRight: '8',
            };
        }
        if (channels >= 4) {
            return {
                selectedReferenceInputChannel: '4',
                selectedReferenceInputChannelLeft: '3',
                selectedReferenceInputChannelRight: '4',
            };
        }
        if (channels >= 3) {
            return {
                selectedReferenceInputChannel: '3',
                selectedReferenceInputChannelLeft: '3',
                selectedReferenceInputChannelRight: '3',
            };
        }
        return {
            selectedReferenceInputChannel: channels >= 2 ? '2' : '',
            selectedReferenceInputChannelLeft: '',
            selectedReferenceInputChannelRight: '',
        };
    }
    // Live measurement settings. The real store persists these, and the
    // frontend reads them back from the PATCH response, so a deliberate
    // capture choice has to survive the round trip in the demo too.
    let measurementSettings = {
        selectedInputId: measurementCaptureInput.id,
        selectedInputKey: measurementCaptureInput.persistent_id,
        selectedInputConfigured: true,
        selectedMicInputChannel: '1',
        ...measurementReferenceSettings(),
        measurementSampleRate: 48000,
    };
    function setMeasurementCaptureInput(input, force) {
        if (!input || (!force && input.id === measurementSettings.selectedInputId)) return;
        measurementCaptureInput = input;
        measurementSettings = {
            ...measurementSettings,
            selectedInputId: input.id,
            selectedInputKey: input.persistent_id,
            selectedInputConfigured: true,
            // A different capture means a different channel layout: reset the
            // mic to input 1 and the reference to that device's defaults.
            selectedMicInputChannel: '1',
            ...measurementReferenceSettings(),
            measurementSampleRate: Number(input.measurement_sample_rate) || 48000,
        };
    }
    let outputMode = {
        mode: 'subwoofer-2.2',
        available: true,
        required_channels: 4,
        effective_output_channels: 4,
        crossover_frequency_hz: DEMO_BASS_HZ,
        slope: 'LR24',
        main_highpass_enabled: true,
        routing: { main_pair: [1, 2], sub_pair: [3, 4], status: 'Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2' },
        subwoofer: { crossover_frequency_hz: DEMO_BASS_HZ, slope: 'LR24', main_highpass_enabled: true, sub_level_db: -1.5, sub_alignment_ms: 2.8, sub_polarity: 'normal' },
        subwoofers: {
            sub1: { level_db: -1.5, alignment_ms: 2.8, polarity: 'normal', crossover_frequency_hz: DEMO_BASS_HZ, slope: 'LR24' },
            sub2: { level_db: 0.5, alignment_ms: 0.0, polarity: 'normal', crossover_frequency_hz: DEMO_BASS_HZ, slope: 'LR24' },
        },
        // Derived 2.2 delays: the DSP computes Main / Sub 1 / Sub 2 from the
        // measured alignment, shown in the subwoofer card. Seeded with the
        // current demo alignment so the card reads like a configured system.
        derived_main_delay_ms: 0.0,
        derived_sub1_delay_ms: 2.8,
        derived_sub2_delay_ms: 0.0,
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

    // ── Output System (v2 demo state) ─────────────────────────────
    // Multichannel modes, role routing and area banks for the Output
    // System settings section, the A/B bank selector and the Crossover
    // tile. Mutations apply to this demo state with revision guards,
    // mirroring the backend contracts (409/400); measurement locking is
    // not simulated here.
    const speakerWays = ['left_low', 'left_low_mid', 'left_mid', 'left_high', 'right_low',
        'right_low_mid', 'right_mid', 'right_high'];
    const subRoles = ['sub_l', 'sub_r', 'sub1', 'sub2'];
    function outputStateRoles(mode) {
        return [...(outputStateStore.modes[mode].crossover_enabled ? speakerWays : ['main_l', 'main_r']),
            ...(mode === 'stereo-sub' ? subRoles : [])];
    }
    const neutralBank = () => ({ preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A' });
    const bankPairs = { main: ['main_l', 'main_r'], low: ['left_low', 'right_low'],
        low_mid: ['left_low_mid', 'right_low_mid'], mid: ['left_mid', 'right_mid'],
        high: ['left_high', 'right_high'], sub: ['sub_l', 'sub_r'] };
    function demoBankDefinitions(roles) {
        const pending = new Set(roles);
        const definitions = { global: { id: 'global', label: 'Global', roles: ['global'], channel_mode: 'stereo' } };
        for (const [id, pair] of Object.entries(bankPairs)) {
            if (!pair.every(role => pending.has(role))) continue;
            definitions[id] = { id, label: `${id.split('_').map(word => word[0].toUpperCase() + word.slice(1)).join('-')} L/R`,
                roles: pair, channel_mode: 'stereo' };
            pair.forEach(role => pending.delete(role));
        }
        for (const role of pending) definitions[role] = { id: role,
            label: role.replace(/^sub(\d+)$/, 'Sub $1').replace(/^center$/, 'Center'), roles: [role], channel_mode: 'mono' };
        return definitions;
    }
    function demoBankSummary(bindings) {
        const common = values => new Set(values).size === 1 ? values[0] : null;
        return { preset: common(bindings.map(bank => bank.preset)), preset_a: common(bindings.map(bank => bank.preset_a)),
            preset_b: common(bindings.map(bank => bank.preset_b)),
            active_side: common(bindings.map(bank => bank.preset === bank.preset_a ? 'A' : bank.preset === bank.preset_b ? 'B' : null)),
            can_a: bindings.length > 0, can_b: bindings.length > 0 && bindings.every(bank => !!bank.preset_b) };
    }
    function updateDemoBanks(config, roles, mutation) {
        const slot = mutation.active_side || demoBankSummary(roles.map(role => config.banks[role])).active_side || 'A';
        if (!['A', 'B'].includes(slot)) throw new Error('Compare side must be A or B');
        const updated = {};
        for (const role of roles) {
            const bank = { ...config.banks[role] };
            for (const key of ['preset_a', 'preset_b']) if (key in mutation) bank[key] = mutation[key];
            if (mutation.preset) { bank[`preset_${slot.toLowerCase()}`] = mutation.preset; bank.preset = mutation.preset; }
            else if (mutation.active_side) {
                if (!bank[`preset_${slot.toLowerCase()}`]) throw new Error(`Compare side ${slot} has no assigned preset`);
                bank.preset = bank[`preset_${slot.toLowerCase()}`];
            }
            if (bank.preset_a === bank.preset_b) throw new Error('Compare slots must use distinct presets');
            updated[role] = bank;
        }
        Object.assign(config.banks, updated);
    }
    const defaultProcessing = () => ({ highpass: null, lowpass: null, level_db: 0.0, alignment_ms: 0.0, polarity: 'normal' });
    const lr24 = (frequency_hz) => ({ family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz });
    const defaultBassManagement = () => ({ frequency_hz: 80, main_highpass_enabled: true,
        family: 'linkwitz-riley', slope_db_oct: 24, sub_link: true, sub_filters: {} });
    // Mirror of the backend rule: Mono, Dual-Mono and a coupled Stereo pair run
    // the shared sub crossover; only an unlinked Stereo pair resolves per side.
    const bassCrossoverForSide = (bass, side, stereo) => {
        const source = bass || {};
        const shared = { family: source.family || 'linkwitz-riley',
            slope_db_oct: source.slope_db_oct || 24, frequency_hz: source.frequency_hz };
        if (!stereo || source.sub_link !== false) return shared;
        const override = (source.sub_filters || {})[side];
        if (!override) return shared;
        return { family: override.family, slope_db_oct: override.slope_db_oct, frequency_hz: override.frequency_hz };
    };
    const subSideForRole = (role) => {
        const name = String(role || '');
        return (name.startsWith('left_') || name === 'main_l' || name === 'sub_l') ? 'left' : 'right';
    };
    // Seeded with the configured .104 system, not an empty chain: a 2-way
    // LR24 crossover at 3 kHz across an 18-channel Scarlett in Stereo + Sub
    // mode with two subs behind an 80 Hz bass-management split.
    // Compare slots are stored per role and projected to banks in the
    // catalog, like the backend: Neutral on A, Direct on B, with the low,
    // high and both sub ways actually holding B.
    const demoWayBank = (onB) => ({ preset: onB ? 'Direct' : 'Neutral', preset_a: 'Neutral', preset_b: onB ? 'Direct' : null, active_side: onB ? 'B' : 'A' });
    const stereoSubBanks = () => ({
        global: { preset: 'Neutral', preset_a: 'Neutral', preset_b: 'Conv LR HybAlign BK 30-3000Hz -7dB', active_side: 'A' },
        main_l: neutralBank(), main_r: neutralBank(),
        left_low: demoWayBank(true), right_low: demoWayBank(true),
        left_low_mid: neutralBank(), right_low_mid: neutralBank(),
        left_mid: neutralBank(), right_mid: neutralBank(),
        left_high: demoWayBank(true), right_high: demoWayBank(true),
        sub_l: neutralBank(), sub_r: neutralBank(),
        sub1: demoWayBank(true), sub2: demoWayBank(true),
    });
    const wayProcessing = (lowpass_hz, level_db, alignment_ms) => ({
        ...defaultProcessing(),
        lowpass: lowpass_hz ? lr24(lowpass_hz) : null,
        level_db,
        alignment_ms,
    });
    const wayBankProcessing = (highpass_hz, level_db, alignment_ms) => ({
        ...defaultProcessing(),
        highpass: highpass_hz ? lr24(highpass_hz) : null,
        level_db,
        alignment_ms,
    });
    // Both ways start flat: the demo opens on a measured, configured system
    // whose speaker alignment has not run yet, so an alignment run really
    // moves the Crossover card's way trim and the revision (see
    // applySpeakerAlignProposal).
    const stereoSubProcessing = () => ({
        main_l: defaultProcessing(), main_r: defaultProcessing(),
        left_low: wayProcessing(DEMO_CROSSOVER_HZ, 0.0, 0.0),
        left_low_mid: wayProcessing(800, 0.0, 0.0),
        left_mid: wayProcessing(800, 0.0, 0.0),
        left_high: wayBankProcessing(DEMO_CROSSOVER_HZ, 0.0, 0.0),
        right_low: wayProcessing(DEMO_CROSSOVER_HZ, 0.0, 0.0),
        right_low_mid: wayProcessing(800, 0.0, 0.0),
        right_mid: wayProcessing(800, 0.0, 0.0),
        right_high: wayBankProcessing(DEMO_CROSSOVER_HZ, 0.0, 0.0),
        sub_l: defaultProcessing(), sub_r: defaultProcessing(),
        sub1: { ...defaultProcessing(), level_db: -1.5, alignment_ms: 2.8 },
        sub2: { ...defaultProcessing(), level_db: 0.5, alignment_ms: 0.0 },
    });
    const demoBassManagement = () => ({
        ...defaultBassManagement(),
        frequency_hz: DEMO_BASS_HZ,
        sub_filters: { left: lr24(DEMO_BASS_HZ), right: lr24(DEMO_BASS_HZ) },
    });
    const outputStateStore = {
        // Revision of the seeded .104 system. Every accepted mutation and the
        // Speaker Auto Alignment commit bump it, like the real output state.
        revision: 896,
        active_mode: 'stereo-sub',
        modes: {
            stereo: {
                crossover_enabled: false,
                selected_bank: 'global',
                banks: { global: neutralBank(), main_l: neutralBank(), main_r: neutralBank() },
                processing: { main_l: defaultProcessing(), main_r: defaultProcessing() },
                bass_management: defaultBassManagement(),
                extras: {},
                routing: {},
            },
            'stereo-sub': {
                crossover_enabled: true,
                selected_bank: 'global',
                banks: stereoSubBanks(),
                processing: stereoSubProcessing(),
                bass_management: demoBassManagement(),
                extras: {},
                routing: {},
            },
        },
    };
    // Out 1/2 mains, 3/4 subs, 5/6 tweeters, 7..18 silent — the .104 routing.
    const DEMO_ROUTING_TAIL = 'off';
    outputStateStore.modes.stereo.routing[SCARLETT_KEY] =
        ['main_l', 'main_r', ...Array(16).fill(DEMO_ROUTING_TAIL)];
    outputStateStore.modes['stereo-sub'].routing[SCARLETT_KEY] =
        ['left_low', 'right_low', 'sub1', 'sub2', 'left_high', 'right_high', ...Array(12).fill(DEMO_ROUTING_TAIL)];
    function outputStateTopology(mode, assignments) {
        const enabled = outputStateStore.modes[mode].crossover_enabled;
        const roles = outputStateRoles(mode).filter(role => assignments.includes(role));
        const subs = roles.filter(r => ['sub_l', 'sub_r', 'sub1', 'sub2'].includes(r));
        const subMode = !subs.length ? 'none' : subs.length === 1 ? 'mono'
            : subs.length === 2 && subs.includes('sub_l') && subs.includes('sub_r') ? 'stereo'
            : subs.length === 2 ? 'dual-mono' : 'unsupported';
        const issues = [];
        let wayCount = null;
        if (!enabled) {
            if (!roles.includes('main_l') || !roles.includes('main_r')) issues.push('Stereo routing requires Main L and Main R');
            if (subs.length > 2) issues.push('At most two distinct sub roles are supported');
        } else {
            const ways = ['low', 'low_mid', 'mid', 'high'];
            const left = roles.filter(r => r.startsWith('left_')).map(r => r.slice(5));
            const right = roles.filter(r => r.startsWith('right_')).map(r => r.slice(6));
            const complete = [['low', 'high'], ['low', 'mid', 'high'], ways];
            const leftOk = complete.some(set => JSON.stringify(set) === JSON.stringify(left));
            const rightOk = complete.some(set => JSON.stringify(set) === JSON.stringify(right));
            if (!leftOk || !rightOk || JSON.stringify(left) !== JSON.stringify(right)) {
                issues.push('Crossover requires complete Low/High, Low/Mid/High, or Low/Low-Mid/Mid/High ways');
            } else {
                wayCount = left.length;
            }
            if (subs.length > 2) issues.push('At most two distinct sub roles are supported');
        }
        return { mode, crossover_enabled: enabled, roles, sub_roles: subs, sub_mode: subMode,
            left_ways: roles.filter(role => role.startsWith('left_')),
            right_ways: roles.filter(role => role.startsWith('right_')), way_count: wayCount, issues };
    }
    function outputStateCatalog() {
        const out = outputEntry(selectedOutput());
        const key = out.key;
        const channels = Number(out.channels || 0);
        const modes = {};
        for (const mode of ['stereo', 'stereo-sub']) {
            const config = outputStateStore.modes[mode];
            const routing = (config.routing[key] || (config.crossover_enabled ? [] : ['main_l', 'main_r'])).slice();
            const topology = outputStateTopology(mode, routing.slice(0, channels || routing.length));
            const banks = {};
            for (const [id, definition] of Object.entries(demoBankDefinitions(topology.roles))) {
                banks[id] = { ...definition, ...demoBankSummary(definition.roles.map(role => config.banks[role])) };
            }
            const pureStereo = topology.roles.length === 2 && topology.roles.includes('main_l') && topology.roles.includes('main_r');
            modes[mode] = {
                crossover_enabled: config.crossover_enabled,
                selected_bank: pureStereo ? 'global' : (config.selected_bank === 'all' || banks[config.selected_bank] ? config.selected_bank : 'global'),
                banks,
                all_banks: demoBankSummary(topology.roles.map(role => config.banks[role])),
                processing: JSON.parse(JSON.stringify(config.processing)),
                bass_management: { ...config.bass_management },
                extras: JSON.parse(JSON.stringify(config.extras)),
                topology,
            };
        }
        const deviceRouting = {};
        for (const mode of ['stereo', 'stereo-sub']) {
            deviceRouting[mode] = (outputStateStore.modes[mode].routing[key]
                || (outputStateStore.modes[mode].crossover_enabled ? [] : ['main_l', 'main_r'])).slice();
        }
        return {
            status: 'ok', revision: outputStateStore.revision, active_mode: outputStateStore.active_mode,
            device: { key, channels, routing: deviceRouting },
            modes,
            capabilities: {
                modes: ['stereo', 'stereo-sub'],
                roles: { stereo: outputStateRoles('stereo'), 'stereo-sub': outputStateRoles('stereo-sub') },
                filter_families: { 'linkwitz-riley': [12, 24, 36, 48, 60, 72], butterworth: [6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72], bessel: [6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72] },
                max_slope_db_oct: 72,
                max_biquads_per_output: 32,
            },
        };
    }
    // Assigns a just-created preset to its bank when the request carries
    // a bank target (multipart fields arrive as strings, JSON as
    // numbers). Mirrors the backend binding contract.
    function assignDemoBankTarget(body, createdName) {
        const mode = String(body.bank_mode || '');
        const bankId = String(body.bank_id || '');
        if (!mode && !bankId) return null;
        if (!mode || !bankId) return { error: 'bank_mode and bank_id are required together', status: 400 };
        const revision = Number(body.expected_revision);
        if (!Number.isInteger(revision) || revision < 0) {
            return { error: 'expected_revision must be a non-negative integer', status: 400 };
        }
        const config = outputStateStore.modes[mode];
        const bank = config && outputStateCatalog().modes[mode].banks[bankId];
        if (!bank) return { error: `Unknown bank ${bankId}`, status: 400 };
        if (revision !== outputStateStore.revision) {
            return { conflict: true, created: createdName };
        }
        updateDemoBanks(config, bank.roles, { preset: createdName });
        outputStateStore.revision += 1;
        return { assigned: true, mode, bank_id: bankId, revision: outputStateStore.revision };
    }
    function demoButterworthDb(frequency, cutoff, slopeDbOct, kind) {
        const order = Math.max(1, Math.round(slopeDbOct / 6));
        const ratio = kind === 'lowpass' ? frequency / cutoff : cutoff / frequency;
        if (ratio <= 0) return 0;
        return -10 * Math.log10(1 + Math.pow(ratio, 2 * order));
    }
    function demoCrossoverDb(frequency, definition, kind) {
        if (!definition) return 0;
        const { family, slope_db_oct: slope, frequency_hz: cutoff } = definition;
        if (family === 'linkwitz-riley') {
            const half = demoButterworthDb(frequency, cutoff, slope / 2, kind);
            return 2 * half;
        }
        return demoButterworthDb(frequency, cutoff, slope, kind);
    }

    function scarlettOutputEntry() {
        const tier = scarlettTier();
        const base = OUTPUTS.find(o => o.key === SCARLETT_KEY);
        return { ...base, channels: tier.channels, active_rate: samplerate.active_rate, supported_rates: scarlettDeviceRates(),
            device_profile: { id: 'scarlett-16i16-4th-gen', tiers: SCARLETT_TIERS.map(x => ({ ...x, rates: x.rates.slice() })), active_tier: tier.id, source: 'alsa-usb-playback', manual: true } };
    }
    function outputEntry(out) {
        if (out.key === SCARLETT_KEY) return scarlettOutputEntry();
        return { ...out };
    }
    function outputsPayload(modeAdjustment) {
        const out = outputEntry(selectedOutput());
        const channels = Number(out.channels || 0);
        // Availability follows the actually available channel count, never
        // the device name: subwoofer modes need at least 4 channels.
        outputMode.available = channels >= 4;
        outputMode.effective_output_channels = channels;
        const routing = routingAssignments(out.key, channels);
        const mode = { ...outputMode, effective_output_channels: channels,
            output_routing: { available: channels > 2, device_key: out.key, assignments: routing.assignments,
                customized: Object.hasOwn(routingStore, out.key),
                signals: ROUTING_SIGNALS.map((label, id) => ({ id, label })), inactive_assignments: routing.inactive } };
        if (modeAdjustment && modeAdjustment.adjusted) {
            mode.mode_adjustment = modeAdjustment;
        }
        return {
            loaded: true,
            available: true,
            default_output: { key: OUTPUTS[0].key, target_name: OUTPUTS[0].name, target_label: OUTPUTS[0].label },
            selected_output: { key: out.key, label: out.name, channels, active_rate: out.active_rate, supported_rates: out.supported_rates, device_profile: out.device_profile },
            current_output: { key: out.key, label: out.name, channels, active_rate: out.active_rate },
            outputs: OUTPUTS.map(o => outputEntry(o)),
            notes: [],
            output_mode: mode,
        };
    }

    function applyDeviceOutputModeForSwitch(deviceKey, channels) {
        const count = Number(channels || 0);
        const currentMode = String(outputMode.mode || 'stereo');
        let candidate = deviceOutputModes[deviceKey] || currentMode;
        if (!OUTPUT_MODES.includes(candidate)) candidate = 'stereo';
        let effective = candidate;
        if (isSubwooferOutputModeName(effective) && count < 4) {
            effective = 'stereo';
        }
        let adjustment = null;
        if (effective !== currentMode) {
            const previous = currentMode;
            outputMode.mode = effective;
            outputMode.required_channels = effective === 'stereo' ? 2 : 4;
            outputMode.routing = { ...(outputMode.routing || {}), status: routingStatusForOutputMode(effective) };
            deviceOutputModes[deviceKey] = effective;
            const reason = (isSubwooferOutputModeName(candidate) && count < 4)
                ? 'device-channel-capacity'
                : 'device-remembered-mode';
            const message = reason === 'device-channel-capacity'
                ? `Output mode switched to ${outputModeLabel(effective)} — selected device supports ${count} channels.`
                : `Output mode restored to ${outputModeLabel(effective)} — last used with this device.`;
            adjustment = { adjusted: true, previous_mode: previous, mode: effective, reason, message };
        }
        outputMode.available = count >= 4;
        outputMode.effective_output_channels = count;
        return adjustment;
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
        const nativeTier = scarlettNativeTierForRate(rate);
        if (selectedOutputKeyCache === SCARLETT_KEY && nativeTier) scarlettTierId = nativeTier.id;
    }
    S.onSourceChanged = followSourceGraphRate;

    // ── Audio source model ──────────────────────────────────────────
    // Mirrors the real stereo-pair abstraction
    // (audio/samplerate/overview.py + audio/external_input.py):
    // multichannel capture interfaces are offered as adjacent stereo
    // pairs (Input 1-2, Input 3-4, ...), never as single mono channels
    // and never duplicated onto both sides. The MOTU M4 yields its two
    // pairs, the Focusrite Scarlett 16i16 capture its nine pairs with
    // the real positional channel suffixes. Single-pair devices keep
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
        {
            id: 104,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:1-2',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 1–2',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 0,
            pair_count: 9,
            pair_label: 'Input 1–2',
            pair_channels: [1, 2],
            left_channel: 'FL',
            right_channel: 'FR',
        },
        {
            id: 105,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:3-4',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 3–4',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 1,
            pair_count: 9,
            pair_label: 'Input 3–4',
            pair_channels: [3, 4],
            left_channel: 'RL',
            right_channel: 'RR',
        },
        {
            id: 106,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:5-6',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 5–6',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 2,
            pair_count: 9,
            pair_label: 'Input 5–6',
            pair_channels: [5, 6],
            left_channel: 'FC',
            right_channel: 'LFE',
        },
        {
            id: 107,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:7-8',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 7–8',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 3,
            pair_count: 9,
            pair_label: 'Input 7–8',
            pair_channels: [7, 8],
            left_channel: 'SL',
            right_channel: 'SR',
        },
        {
            id: 108,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:9-10',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 9–10',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 4,
            pair_count: 9,
            pair_label: 'Input 9–10',
            pair_channels: [9, 10],
            left_channel: 'AUX0',
            right_channel: 'AUX1',
        },
        {
            id: 109,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:11-12',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 11–12',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 5,
            pair_count: 9,
            pair_label: 'Input 11–12',
            pair_channels: [11, 12],
            left_channel: 'AUX2',
            right_channel: 'AUX3',
        },
        {
            id: 110,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:13-14',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 13–14',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 6,
            pair_count: 9,
            pair_label: 'Input 13–14',
            pair_channels: [13, 14],
            left_channel: 'AUX4',
            right_channel: 'AUX5',
        },
        {
            id: 111,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:15-16',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 15–16',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 7,
            pair_count: 9,
            pair_label: 'Input 15–16',
            pair_channels: [15, 16],
            left_channel: 'AUX14',
            right_channel: 'AUX15',
        },
        {
            id: 112,
            key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input::pair:17-18',
            source_key: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            name: 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input',
            device_label: 'Focusrite Scarlett 16i16',
            port_key: null,
            port_label: null,
            label: 'Focusrite Scarlett 16i16 · Input 17–18',
            sample_spec: 's32le 18ch 48000Hz',
            channels: 18,
            channel_map: null,
            active_rate: 48000,
            state: 'IDLE',
            is_default: false,
            selectable: true,
            is_active_port: true,
            pair_index: 8,
            pair_count: 9,
            pair_label: 'Input 17–18',
            pair_channels: [17, 18],
            left_channel: 'AUX16',
            right_channel: 'AUX17',
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
    // The demo presents "SMB_Demo_Library-1" (the first demo share) as its
    // active library, like a box with that share selected; Local and
    // "SMB_Demo_Library-2" stay selectable under Settings. Each share serves
    // its own catalog (local main, demo-library-2 SMB-1, demo-nas SMB-2).
    const musicLibraries = {
        active_id: 'demo-library-2',
        active_type: 'smb',
        libraries: [
            { id: 'local', label: 'Local', type: 'local' },
            { id: 'demo-library-2', label: 'SMB_Demo_Library-1', type: 'smb' },
            { id: 'demo-nas', label: 'SMB_Demo_Library-2', type: 'smb' },
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

    // Second/third demo catalogs (demo/data/library2.js = SMB_Demo_Library-1,
    // demo/data/library3.js = SMB_Demo_Library-2). Only the selected library
    // serves the browse surfaces; manually added shares fall back to local.
    const lib2 = window.FXROUTE_DEMO_LIBRARY2 || { tracks: [], albums: [] };
    const lib3 = window.FXROUTE_DEMO_LIBRARY3 || { tracks: [], albums: [] };
    function activeLib() {
        if (musicLibraries.active_id === 'demo-library-2') return lib2;
        if (musicLibraries.active_id === 'demo-nas') return lib3;
        return lib;
    }
    // Cross-catalog lookup: the active catalog wins, the others are
    // fallbacks so ids referenced by live state (a track still playing from
    // a library that was just switched away) keep resolving. Returns the
    // owning catalog alongside the item, so dependent endpoints serve or
    // mutate the right catalog instead of re-filtering through activeLib().
    const allCatalogs = () => [activeLib(), lib, lib2, lib3]
        .filter((cat, idx, arr) => cat && arr.indexOf(cat) === idx);
    function findAlbum(id) {
        for (const catalog of allCatalogs()) {
            const album = catalog.albums.find(a => a.id === id);
            if (album) return { album, catalog };
        }
        return null;
    }
    // Track ids are paths relative to the music root ("local_<Album>/01 - X.flac"),
    // so they carry "/" and spaces. Mirrors the real backend routes, which all
    // declare {track_id:path} (library/api.py).
    const TRACK_ID_PATH = '(.+)';
    function pathId(value) {
        try { return decodeURIComponent(value); } catch (_) { return value; }
    }

    function findTrack(id) {
        for (const catalog of allCatalogs()) {
            const track = catalog.tracks.find(t => t.id === id);
            if (track) return { track, catalog };
        }
        return null;
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

    // Job discovery after a reload. The backend keeps its jobs in a
    // process-wide map, so GET .../current answers with the newest live or
    // briefly retained job and a finished one is dropped after
    // AUTO_SUB_RETENTION_MS. The demo has no persistence layer, so the newest
    // job is parked in sessionStorage instead: a reloaded page finds its run
    // again and the measurement panel reattaches to it, exactly like the
    // product. Everything else about the job stays in memory.
    const AUTO_SUB_RETENTION_MS = 600000;
    const AUTO_SUB_STORAGE_KEY = 'fxroute.demo.autosub.job';

    function readStoredAutoSubJob() {
        try {
            if (typeof sessionStorage === 'undefined') return null;
            const stored = sessionStorage.getItem(AUTO_SUB_STORAGE_KEY);
            const job = stored ? JSON.parse(stored) : null;
            if (!job || !job.id || !job.startedAt || !job.mode) return null;
            return job;
        } catch (e) { return null; }
    }

    function storeAutoSubJob(job) {
        try {
            if (typeof sessionStorage === 'undefined') return;
            if (job) sessionStorage.setItem(AUTO_SUB_STORAGE_KEY, JSON.stringify(job));
            else sessionStorage.removeItem(AUTO_SUB_STORAGE_KEY);
        } catch (e) { /* storage is optional */ }
    }

    // A job past its retention is gone, in memory and in the session record
    // that would otherwise restore it on the next reload.
    function dropAutoSubJob(id) {
        delete autoSubJobs[id];
        const stored = readStoredAutoSubJob();
        if (!stored || stored.id === id) storeAutoSubJob(null);
    }

    // A restored job keeps running on its own elapsed time, so a reload
    // mid-run reattaches a run that is already further along.
    (function restoreAutoSubJob() {
        const job = readStoredAutoSubJob();
        if (!job) return;
        autoSubJobs[job.id] = job;
        autoSubSeq = Math.max(autoSubSeq, Number(String(job.id).split('_').pop()) || 0);
    })();

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
            // A cancelled run settles at once and is retained from there.
            if (!job.finishedAt) {
                job.finishedAt = Date.now();
                storeAutoSubJob(job);
            }
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
        // Terminal stage: the run finished at the stage script's end, which
        // is when the backend schedules the job's retention cleanup.
        if (!job.finishedAt) {
            job.finishedAt = job.startedAt + (is22 ? 8800 : 8000);
            storeAutoSubJob(job);
        }
        return autoSubResult(job);
    }

    // The newest job a reloaded page may reattach to: a live one, or a
    // finished one still inside its retention window. Mirrors
    // get_current_auto_sub_optimize_job() plus the backend's 10 minute
    // retention, so an old run is not rediscovered forever.
    function currentAutoSubJobPayload() {
        const now = Date.now();
        const ids = Object.keys(autoSubJobs);
        for (const id of ids) {
            const job = autoSubJobs[id];
            if (job.finishedAt && now - job.finishedAt >= AUTO_SUB_RETENTION_MS) dropAutoSubJob(id);
        }
        for (let index = ids.length - 1; index >= 0; index -= 1) {
            if (autoSubJobs[ids[index]]) return autoSubJobPayload(ids[index]);
        }
        return null;
    }

    // Demo measurement file stock: the .104 mic calibration is the active file
    // and no house curve is loaded yet. Uploads/deletes only mutate these
    // lists in memory.
    S.demoCalibrationOptions = S.demoCalibrationOptions || [{ id: 'MM1CES_allein_00d.txt', filename: 'MM1CES_allein_00d.txt' }];
    S.demoHouseCurveOptions = S.demoHouseCurveOptions || [];
    S.demoActiveCalibrationId = (S.demoActiveCalibrationId === undefined) ? 'MM1CES_allein_00d.txt' : S.demoActiveCalibrationId;

    // ── Speaker Align simulation ────────────────────────────────────────
    // One staged run per side, on top of the .104 alignment fixture
    // (demo/data/alignment.js). The job walks the real state machine —
    // queued → acquiring (shared planning take, then one take per way) →
    // confirming (verification take) → committed — with the backend's own
    // stage messages, so the panel's progress line reads like a live run.
    // On commit the proposal is really applied to the output state, so the
    // Crossover card's way trim and the revision follow the run.
    const speakerAlignJobs = {};
    let speakerAlignSeq = 0;
    const SPEAKER_ALIGN_FIXTURE = window.FXROUTE_DEMO_ALIGNMENT || null;

    // Way roles of the side, in configured low→high order. Speaker Align is
    // available from the Global bank of a crossover with 2..4 ways per side,
    // so the roles come from the routed topology, not from every entry the
    // processing table happens to carry.
    function speakerAlignRoles(side) {
        const config = outputStateStore.modes[outputStateStore.active_mode] || {};
        const routed = (config.routing && config.routing[selectedOutputKeyCache]) || [];
        const roles = outputStateRoles(outputStateStore.active_mode)
            .filter(role => role.startsWith(`${side}_`) && routed.includes(role));
        if (roles.length >= 2 && roles.length <= 4) return roles;
        return [`${side}_low`, `${side}_high`];
    }
    // Stage script: the shared planning take counts as step 1, so a two-way
    // side measures three takes. Mirrors the backend's progress indices and
    // its role spelling (`right_low` -> "right low").
    function speakerAlignScript(side) {
        const roles = speakerAlignRoles(side);
        const total = roles.length + 1;
        const steps = [
            { after: 900, status: 'queued', message: 'Speaker alignment queued.' },
            { after: 2400, status: 'acquiring', message: 'Measuring speaker ways…' },
            { after: 5200, status: 'acquiring', message: `Measuring ${side} ways (1/${total})…` },
        ];
        roles.forEach((role, index) => {
            steps.push({
                after: 8200 + index * 2200,
                status: 'acquiring',
                message: `Measuring ${role.replace(/_/g, ' ')} (${index + 2}/${total})…`,
            });
        });
        const confirmAt = 8200 + roles.length * 2200;
        steps.push({ after: confirmAt, status: 'confirming', message: 'Confirming alignment acoustically…' });
        steps.push({ after: confirmAt + 2600, status: 'confirming', message: `Verifying ${side} ways (1/1)…` });
        return { steps, doneAt: confirmAt + 4800 };
    }
    // Apply a committed proposal to the demo output state, like the backend's
    // commit: delay and level of every way of the side, then a new revision
    // that the job reports and the takes are re-stamped with.
    function applySpeakerAlignProposal(side, result) {
        if (!result) return null;
        const config = outputStateStore.modes[outputStateStore.active_mode];
        if (!config) return null;
        for (const role of speakerAlignRoles(side)) {
            const processing = config.processing[role];
            if (!processing) continue;
            const delay = Number(result.proposal.added_delay_ms?.[role]);
            const gain = Number(result.proposal.added_gain_db?.[role]);
            if (Number.isFinite(delay)) processing.alignment_ms = Math.round((processing.alignment_ms + delay) * 1e5) / 1e5;
            if (Number.isFinite(gain)) processing.level_db = Math.round((processing.level_db + gain) * 1e4) / 1e4;
        }
        outputStateStore.revision += 1;
        result.committed_revision = outputStateStore.revision;
        // The planning take was captured at the start revision; only the
        // verification take saw the committed state.
        const verification = (result.measurements || {}).after;
        if (verification && verification.measurement_target) {
            verification.measurement_target.revision = result.committed_revision;
        }
        return outputStateStore.revision;
    }

    function speakerAlignPayload(id, elapsedMs) {
        const job = speakerAlignJobs[id];
        if (!job) return null;
        const elapsed = (elapsedMs != null) ? elapsedMs : (Date.now() - job.startedAt);
        const base = speakerAlignBase(job);
        // A requested cancel settles on real time, independently of the stage
        // script the test drives with a synthetic elapsed value.
        if (job.status === 'cancelling' && Date.now() - (job.cancelledAt || 0) >= 900) job.status = 'cancelled';
        if (job.status === 'cancelling' || job.status === 'cancelled') {
            return { ...base, status: job.status,
                message: job.status === 'cancelling' ? 'Cancelling speaker alignment…' : 'Speaker alignment cancelled.',
                result: null, error: null };
        }
        const stage = job.script.steps.find(step => elapsed < step.after);
        if (stage) return { ...base, status: stage.status, message: stage.message, result: null, error: null };
        if (!job.result) {
            job.result = JSON.parse(JSON.stringify(job.fixtureResult));
            if (job.dryRun) {
                job.result.dry_run = true;
                job.result.committed_revision = null;
            } else {
                applySpeakerAlignProposal(job.side, job.result);
            }
        }
        return { ...base,
            status: job.dryRun ? 'trial-done' : 'committed',
            message: job.dryRun
                ? (job.result.check.confirmed
                    ? 'Trial alignment confirmed without committing.'
                    : 'Trial alignment did not confirm; nothing changed.')
                : `Committed speaker alignment at revision ${job.result.committed_revision}.`,
            result: job.result, error: null };
    }
    function speakerAlignBase(job) {
        return { id: job.id, side: job.side, dry_run: !!job.dryRun, params: job.params };
    }
    // A run for a side the fixture does not cover: still a complete result,
    // built from the seeded output state, so the panel never shows a hole.
    function speakerAlignFallbackResult(side) {
        const roles = speakerAlignRoles(side);
        const arrival = {};
        roles.forEach((role, index) => { arrival[role] = Number((1.4 + index * 2.6).toFixed(2)); });
        const latest = Math.max(...Object.values(arrival));
        const levels = {};
        roles.forEach((role, index) => { levels[role] = Number((-10.2 - index * 1.1).toFixed(2)); });
        const median = roles.map(role => levels[role]).sort((a, b) => a - b)[Math.floor(roles.length / 2) - (roles.length % 2 ? 0 : 1)] || 0;
        const addedGain = {};
        roles.forEach((role) => { addedGain[role] = Math.round((median - levels[role]) * 100) / 100; });
        const addedDelay = {};
        roles.forEach((role) => { addedDelay[role] = Math.round((latest - arrival[role]) * 1000) / 1000; });
        const residual = 0.05;
        const revision = outputStateStore.revision + 1;
        return {
            confirmed: true,
            side,
            sample_rate_hz: 48000,
            check: { confirmed: true, reasons: [], warnings: [],
                max_residual_ms: residual,
                before_spread_ms: Math.round((latest - Math.min(...Object.values(arrival))) * 1000) / 1000,
                after_arrival_ms: Object.fromEntries(roles.map(role => [role, latest])),
                tolerance_ms: 0.25,
                pairs: roles.length > 1 ? [{ roles: [roles[0], roles[1]], residual_within_pair_ms: residual }] : [],
                gain_spread_db: 0.18,
                before_gain_spread_db: Math.round(Math.abs(levels[roles[0]] - levels[roles[roles.length - 1]]) * 100) / 100,
                gain_tolerance_db: 2.0,
                way_isolation_db: Object.fromEntries(roles.map(role => [role, 19.0])),
                isolation_margin_db: 19.0,
                after_way_levels_db: Object.fromEntries(roles.map(role => [role, median])) },
            proposal: { start_revision: outputStateStore.revision,
                processing_fingerprint: 'demo-fingerprint', arrival_ms: arrival,
                reference_role: roles[roles.length - 1], added_delay_ms: addedDelay,
                way_levels_db: levels, added_gain_db: addedGain,
                planning_isolation_db: Object.fromEntries(roles.map(role => [role, 18.0])),
                arrival_source: 'shared-planning-take' },
            measurements: null,
            provenance: {},
            committed_revision: revision,
            dry_run: false,
        };
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
                const tid = String(body.track_id || '');
                const track = S.playTidal(tid, body.queue_track_ids);
                if (!track && tid) return err('Track not found', 404);
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
        const trackFavMatch = p.match(new RegExp('^/api/tracks/' + TRACK_ID_PATH + '/favorite$'));
        if (trackFavMatch) {
            const resolved = findTrack(pathId(trackFavMatch[1]));
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
        const trackCoverMatch = p.match(new RegExp('^/api/tracks/cover/' + TRACK_ID_PATH + '$'));
        if (trackCoverMatch) {
            const resolved = findTrack(pathId(trackCoverMatch[1]));
            return j({ redirect: resolved ? resolved.track.cover_url : lib.demoImage('track:' + pathId(trackCoverMatch[1] || 'x')) });
        }
        const trackCoverInfoMatch = p.match(new RegExp('^/api/tracks/cover-info/' + TRACK_ID_PATH + '$'));
        if (trackCoverInfoMatch) {
            const resolved = findTrack(pathId(trackCoverInfoMatch[1]));
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
        // Provider account state mirrors the real backend: an account provider
        // (Qobuz/TIDAL) can be disconnected from Settings and only its auth
        // endpoints flip it back. Spotify has no account login (installed
        // spotifyd pairs from the Spotify app), so its flag stays null.
        function demoProviderAccount() {
            S.demoProviderAuthenticated = S.demoProviderAuthenticated || { qobuz: true, tidal: true };
            return S.demoProviderAuthenticated;
        }
        if (p === '/api/streaming/providers') {
            const account = demoProviderAccount();
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', installed: true, available: true, authenticated: true, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', installed: true, available: true, authenticated: account.qobuz, connected: true, capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', installed: true, available: true, authenticated: account.tidal, connected: true, capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
        // The demo provider flags live on the shared state object so the
        // enabled toggles persist across fetch calls.
        if (p === '/api/streaming/providers/discovery') {
            // Same shape as the backend's streaming.discover_providers(): the
            // current frontend builds the provider tabs from this endpoint.
            S.demoProviderEnabled = S.demoProviderEnabled || { spotify: true, qobuz: true, tidal: true };
            const enabled = S.demoProviderEnabled;
            const account = demoProviderAccount();
            return j({ providers: [
                { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: null, enabled: enabled.spotify !== false, connected: true, backend: 'spotifyd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: account.qobuz, enabled: enabled.qobuz !== false, connected: true, backend: 'qbzd', capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true } },
                { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: account.tidal, enabled: enabled.tidal !== false, connected: true, backend: 'tidalapi', capabilities: { catalog: true, cover: true, progress: true } },
            ] });
        }
        // Settings -> Providers admin (device name rides the same payload).
        // Demo keeps every provider installed; the enabled and account flags are
        // the local state the toggles and auth endpoints write, so tabs hide/show
        // and the row reads Connect/Disconnect like the real backend.
        function demoProviderAdmin() {
            S.demoProviderEnabled = S.demoProviderEnabled || { spotify: true, qobuz: true, tidal: true };
            const enabled = S.demoProviderEnabled;
            const account = demoProviderAccount();
            return {
                providers: [
                    { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: null, enabled: enabled.spotify !== false },
                    { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: account.qobuz, enabled: enabled.qobuz !== false },
                    { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: account.tidal, enabled: enabled.tidal !== false },
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
            demoProviderAccount().qobuz = true;
            return j({ success: true });
        }
        if (p === '/api/streaming/qobuz/auth/logout' && post) {
            demoProviderAccount().qobuz = false;
            return j({ success: true });
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
        if (p === '/api/streaming/qobuz/status') return j({ ...S.qobuz.payload(), authenticated: demoProviderAccount().qobuz });
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
                authenticated: demoProviderAccount().tidal,
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
            // New playlists show their content like the local collage does:
            // the first track's album cover, pool fallback when empty.
            const firstArt = tracks.length && tracks[0].art_url ? tracks[0].art_url : lib.demoImage('playlist:' + id);
            const pl = { id, name, description: 'Demo playlist', track_count: tracks.length, art_url: firstArt, cover_url: firstArt, owner: 'fxroute-demo', is_public: true, tracks };
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
            if (!album) return j([]);
            // Normalized track dicts like the real provider boundary
            // (id/title/artist/album/art_url/duration/audio_quality): the
            // album track rows render thumb + subtitle from the track itself.
            return j(album.tracks.map(t => ({
                id: t.id,
                title: t.title,
                artist: album.artist,
                album: album.title,
                duration: t.duration,
                track_number: t.trackNumber,
                audio_quality: album.audio_quality,
                art_url: album.cover_url,
            })));
        }
        const tidalArtistDetail = p.match(/^\/api\/streaming\/tidal\/artists\/([^/]+)$/);
        if (tidalArtistDetail) {
            const artist = tidalArtists().find(a => a.id === tidalArtistDetail[1]);
            if (!artist) return err('Artist not found');
            const albums = tidalAlbums().filter(a => a.artist_id === artist.id);
            return j({
                ...artist,
                albums: albums.map(a => ({ id: a.id, title: a.title, artist: artist.name, year: a.year, audio_quality: a.audio_quality, num_tracks: a.num_tracks, art_url: a.cover_url })),
                top_tracks: albums.flatMap(a => a.tracks.slice(0, 3).map(t => ({ id: t.id, title: t.title, artist: artist.name, album: a.title, duration: t.duration, track_number: t.trackNumber, audio_quality: a.audio_quality, art_url: a.cover_url }))).slice(0, 10),
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
        if (p === '/api/streaming/tidal/auth/device/finish' && post) {
            demoProviderAccount().tidal = true;
            return j({ success: true });
        }
        if (p === '/api/streaming/tidal/auth/pkce' && post) return j({ url: 'https://link.tidal.com/demo-auth' });
        if (p === '/api/streaming/tidal/auth/pkce/finish' && post) {
            demoProviderAccount().tidal = true;
            return j({ success: true });
        }
        if (p === '/api/streaming/tidal/auth/logout' && post) {
            demoProviderAccount().tidal = false;
            return j({ success: true });
        }

        // ── Audio / settings ────────────────────────────────────────────
        if (p === '/api/audio/outputs') {
            if (post) {
                const key = String(body.key || '');
                if (OUTPUTS.find(o => o.key === key)) {
                    selectedOutputKeyCache = key;
                    // The capture interface follows the selected device, so
                    // picking the Scarlett 16i16 also switches the measurement
                    // setup to its 18-channel capture (split Ref L / R).
                    setMeasurementCaptureInput(captureInputForOutputKey(key), true);
                    // Derive the routing from the actually available channel
                    // count: a subwoofer mode on a device with fewer than 4
                    // channels falls back to Stereo (crossover/sub routing
                    // off), a remembered mode is restored when possible.
                    const channels = Number(outputEntry(selectedOutput()).channels || 0);
                    const adjustment = applyDeviceOutputModeForSwitch(key, channels);
                    return j(outputsPayload(adjustment));
                }
                return j(outputsPayload());
            }
            return j(outputsPayload());
        }
        if (p === '/api/audio/output-routing') {
            if (post) {
                const key = String(body.key || '');
                const out = outputEntry(selectedOutput());
                if (key !== out.key) return err('Selected output changed; refresh audio settings', 400);
                const values = body.assignments;
                if (!Array.isArray(values) || values.length !== out.channels || values.some(v => !Number.isInteger(v) || v < 0 || v > 4)) {
                    return err('Assign one signal (0-4) to each available hardware output', 400);
                }
                const prev = routingStore[key];
                const full = values.slice();
                if (Array.isArray(prev) && prev.length > full.length) full.push(...prev.slice(full.length));
                routingStore[key] = full;
                return j(outputsPayload());
            }
            return j(outputsPayload());
        }
        if (p === '/api/audio/output-mode') {
            if (post) {
                const mode = normalizeOutputModeName(String(body.mode || 'stereo'));
                const currentOut = outputEntry(selectedOutput());
                const currentChannels = Number(currentOut.channels || 0);
                if (isSubwooferOutputModeName(mode) && currentChannels < 4) {
                    const label = mode === 'subwoofer-2.1' ? '2.1'
                        : mode === 'subwoofer-2.2-stereo' ? '2.2 Stereo Bass' : '2.2';
                    return err(`${label} Subwoofer requires a selected multichannel output with at least 4 channels`, 400);
                }
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
                outputMode.available = currentChannels >= 4;
                outputMode.required_channels = mode === 'stereo' ? 2 : 4;
                outputMode.effective_output_channels = currentChannels;
                outputMode.routing = {
                    status: routingStatusForOutputMode(mode),
                };
                deviceOutputModes[currentOut.key] = mode;
                return j(outputsPayload());
            }
            const outNow = outputEntry(selectedOutput());
            outputMode.available = Number(outNow.channels || 0) >= 4;
            outputMode.effective_output_channels = Number(outNow.channels || 0);
            return j(outputMode);
        }

        if (p === '/api/audio/output-state' && !post) {
            return j(outputStateCatalog());
        }
        if (p === '/api/audio/output-state/apply' && post) {
            const revision = body.expected_revision;
            if (!Number.isInteger(revision) || revision < 0) return err('expected_revision must be a non-negative integer', 400);
            if (revision !== outputStateStore.revision) {
                return err({ code: 'revision-conflict', message: 'Output state changed', revision: outputStateStore.revision }, 409);
            }
            const mutation = body.mutation || {};
            const mode = String(mutation.mode || '');
            const config = outputStateStore.modes[mode];
            if (!config) return err(`Unknown output mode: ${mutation.mode}`, 400);
            const fail = (message) => err(message, 400);
            if (mutation.kind === 'switch_mode') {
                outputStateStore.active_mode = mode;
            } else if (mutation.kind === 'set_crossover') {
                if (typeof mutation.enabled !== 'boolean') return fail('Crossover enabled must be a boolean');
                config.crossover_enabled = mutation.enabled;
                const mapping = mutation.enabled ? { main_l: 'left_low', main_r: 'right_low' }
                    : { left_low: 'main_l', right_low: 'main_r' };
                const allowed = outputStateRoles(mode);
                for (const [key, assignments] of Object.entries(config.routing)) {
                    config.routing[key] = assignments.map(role => mapping[role] || (allowed.includes(role) ? role : 'off'));
                    for (const role of config.routing[key]) {
                        if (role !== 'off' && !config.banks[role]) {
                            config.banks[role] = neutralBank();
                            config.processing[role] = defaultProcessing();
                        }
                    }
                }
                if (config.selected_bank !== 'global' && config.selected_bank !== 'all'
                    && !allowed.includes(config.selected_bank)
                    && !Object.values(demoBankDefinitions(allowed)).some(definition => definition.id === config.selected_bank)) config.selected_bank = 'global';
            } else if (mutation.kind === 'set_routing') {
                const assignments = mutation.assignments;
                if (!Array.isArray(assignments)) return fail('Routing assignments must be an array');
                const allowed = new Set([...outputStateRoles(mode), 'off']);
                if (assignments.some(role => !allowed.has(role))) return fail('Routing contains an invalid role');
                const key = outputEntry(selectedOutput()).key;
                config.routing[key] = [...assignments, ...(config.routing[key] || []).slice(assignments.length)];
                for (const role of assignments) {
                    if (role !== 'off' && !config.banks[role]) {
                        config.banks[role] = neutralBank();
                        config.processing[role] = defaultProcessing();
                    }
                }
                const routedRoles = assignments.filter(role => role !== 'off');
                const routedBanks = demoBankDefinitions(routedRoles);
                let selected = config.selected_bank;
                for (const definition of Object.values(routedBanks)) {
                    if (definition.id === selected || definition.roles.includes(selected)) { selected = definition.id; break; }
                }
                const pureStereo = routedRoles.length === 2 && routedRoles.includes('main_l') && routedRoles.includes('main_r');
                if (pureStereo || (selected !== 'all' && !routedBanks[selected])) selected = 'global';
                config.selected_bank = selected;
            } else if (mutation.kind === 'select_bank') {
                const active = outputStateCatalog().modes[mode].banks;
                const roles = outputStateCatalog().modes[mode].topology.roles;
                if (roles.length === 2 && roles.includes('main_l') && roles.includes('main_r') && mutation.bank_id !== 'global') return fail('Pure stereo uses the Global bank; no bank selection is shown');
                if (mutation.bank_id !== 'all' && !active[mutation.bank_id]) return fail(`Unknown bank ${mutation.bank_id}`);
                config.selected_bank = mutation.bank_id;
            } else if (mutation.kind === 'set_bank_preset') {
                const bank = outputStateCatalog().modes[mode].banks[mutation.bank_id];
                if (!bank) return fail(`Unknown bank ${mutation.bank_id}`);
                try { updateDemoBanks(config, bank.roles, mutation); } catch (error) { return fail(error.message); }
            } else if (mutation.kind === 'switch_all_banks') {
                const roles = outputStateCatalog().modes[mode].topology.roles;
                try { updateDemoBanks(config, roles, mutation); } catch (error) { return fail(error.message); }
            } else if (mutation.kind === 'set_processing') {
                const settings = config.processing[mutation.role];
                if (!settings) return fail(`Unknown role ${mutation.role}`);
                for (const key of ['highpass', 'lowpass']) {
                    if (key in mutation) settings[key] = mutation[key];
                }
                for (const key of ['level_db', 'alignment_ms', 'polarity']) {
                    if (mutation[key] !== undefined && mutation[key] !== null) settings[key] = mutation[key];
                }
            } else if (mutation.kind === 'set_subwoofers') {
                const roles = outputStateCatalog().modes[mode].topology.sub_roles;
                if (!mutation.processing || JSON.stringify(Object.keys(mutation.processing).sort()) !== JSON.stringify(roles.slice().sort())) {
                    return fail('Sub settings must describe exactly the routed sub roles');
                }
                config.bass_management = { ...config.bass_management,
                    frequency_hz: mutation.frequency_hz ?? config.bass_management.frequency_hz,
                    main_highpass_enabled: mutation.main_highpass_enabled ?? config.bass_management.main_highpass_enabled };
                for (const key of ['family', 'slope_db_oct', 'sub_link']) {
                    if (mutation[key] !== undefined && mutation[key] !== null) config.bass_management[key] = mutation[key];
                }
                if (mutation.sub_filters !== undefined && mutation.sub_filters !== null) {
                    if (typeof mutation.sub_filters !== 'object') return fail('Sub crossover overrides must be an object keyed by side');
                    for (const [side, definition] of Object.entries(mutation.sub_filters)) {
                        if (side !== 'left' && side !== 'right') return fail('Sub crossover overrides must be keyed by left and right');
                        if (definition === null) delete config.bass_management.sub_filters[side];
                        else config.bass_management.sub_filters[side] = { ...definition };
                    }
                }
                for (const role of roles) Object.assign(config.processing[role], mutation.processing[role]);
            } else if (mutation.kind === 'set_extras') {
                if (typeof mutation.extras !== 'object' || mutation.extras === null) {
                    return fail('extras must be an object');
                }
                config.extras = mutation.extras;
            } else {
                return fail(`Unknown mutation kind: ${mutation.kind}`);
            }
            outputStateStore.revision += 1;
            const catalog = outputStateCatalog();
            const topology = catalog.modes[catalog.active_mode].topology;
            return j({ status: 'ok', revision: catalog.revision, active_mode: catalog.active_mode,
                fingerprint: `demo-${catalog.revision}`, fingerprint_changed: true,
                live_applied: topology.issues.length === 0, live_reason: topology.issues.length ? 'not-activatable' : null,
                topology });
        }
        if (p === '/api/audio/output-state/crossover-response' && !post) {
            const catalog = outputStateCatalog();
            const ways = {};
            const config = catalog.modes[catalog.active_mode];
            // Sub crossover high-pass from the subwoofer tile: with routed
            // subs and Main highpass on, every speaker way runs through the
            // sub crossover, type and slope included (mirrors the backend
            // plan). A true Stereo pair resolves per side while unlinked.
            const bass = config.bass_management || {};
            const hasSubs = (config.topology.sub_roles || []).length > 0;
            const stereoPair = config.topology.sub_mode === 'stereo';
            const derivedHighpassFor = (role) => {
                if (!hasSubs || bass.main_highpass_enabled !== true) return null;
                const shape = bassCrossoverForSide(bass, subSideForRole(role), stereoPair);
                const frequency = Number(shape.frequency_hz);
                if (!Number.isFinite(frequency) || frequency < 40 || frequency > 200) return null;
                return { family: shape.family, slope_db_oct: shape.slope_db_oct, frequency_hz: Math.round(frequency) };
            };
            for (const [role, settings] of Object.entries(catalog.modes[catalog.active_mode].processing)) {
                if (!config.crossover_enabled || !config.topology.roles.includes(role)) continue;
                if (!role.startsWith('left_') && !role.startsWith('right_')) continue;
                const points = [];
                const derivedHighpass = derivedHighpassFor(role);
                const way = role.split('_').slice(1).join('_');
                const required = way === 'low' ? ['lowpass'] : way === 'high' ? ['highpass'] : ['highpass', 'lowpass'];
                // Off is a valid direction: the curve shows the band the way
                // actually runs, complete only marks a fully set way.
                const complete = required.every(kind => settings[kind]);
                for (let index = 0; index < 180; index += 1) {
                    const frequency = 20 * Math.pow(1000, index / 179);
                    let level = 0;
                    if (settings.highpass) level += demoCrossoverDb(frequency, settings.highpass, 'highpass');
                    if (settings.lowpass) level += demoCrossoverDb(frequency, settings.lowpass, 'lowpass');
                    if (derivedHighpass) level += demoCrossoverDb(frequency, derivedHighpass, 'highpass');
                    points.push([Math.round(frequency * 1000) / 1000, Math.round(level * 1000) / 1000]);
                }
                ways[role] = { filters: { highpass: settings.highpass, lowpass: settings.lowpass },
                    derived_highpass: derivedHighpass ? { ...derivedHighpass } : null,
                    complete, points };
            }
            return j({ status: 'ok', revision: catalog.revision, mode: catalog.active_mode,
                crossover_enabled: config.crossover_enabled,
                bass_management: { ...config.bass_management },
                sub_roles: [...(config.topology.sub_roles || [])],
                sample_rate_hz: 48000, ways });
        }
        if (p === '/api/audio/samplerate') {
            if (post) {
                const mode = String(body.mode || 'auto');
                const rate = Number(body.rate || 0);
                const out = outputEntry(selectedOutput());
                const tierRates = out.key === SCARLETT_KEY ? scarlettDeviceRates() : out.supported_rates;
                if (mode === 'fixed' && rate && !tierRates.includes(rate)) return err('Selected output does not support this sample rate', 400);
                if (mode === 'fixed' && rate && out.key === SCARLETT_KEY) {
                    const native = scarlettNativeTierForRate(rate);
                    if (native && native.id !== scarlettTierId) scarlettTierId = native.id;
                }
                samplerate = {
                    ...samplerate,
                    mode: mode === 'fixed' ? 'fixed' : 'auto',
                    policy: { mode: mode === 'fixed' ? 'fixed' : 'auto', rate: (mode === 'fixed' && rate) ? rate : null },
                    active_rate: (mode === 'fixed' && rate) ? rate : samplerate.active_rate,
                };
                // Back in auto the graph follows the source again, so the
                // interface tier has to follow the graph rate down as well —
                // otherwise a fixed high rate would pin the channel count.
                if (mode !== 'fixed') followSourceGraphRate();
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
            const created = { success: true, preset: { name } };
            const binding = assignDemoBankTarget(body, name);
            if (binding && binding.error) return err(binding.error, binding.status);
            if (binding && binding.conflict) {
                created.bank = { assigned: false, created: name };
                return j(created, 409);
            }
            if (binding) created.bank = binding;
            return j(created);
        }
        if (p === '/api/dsp/presets/create-with-ir' && post) {
            const name = String(body.preset_name || body.name || 'Convolver Preset').trim() || 'Convolver Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], convolver: true });
            }
            syncDspToState();
            const created = { success: true, preset: { name } };
            const binding = assignDemoBankTarget(body, name);
            if (binding && binding.error) return err(binding.error, binding.status);
            if (binding && binding.conflict) {
                created.bank = { assigned: false, created: name };
                return j(created, 409);
            }
            if (binding) created.bank = binding;
            return j(created);
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
            const pinned = new Set();
            for (const config of Object.values(outputStateStore.modes)) {
                for (const bank of Object.values(config.banks)) {
                    for (const key of ['preset', 'preset_a', 'preset_b']) {
                        if (bank[key]) pinned.add(bank[key]);
                    }
                }
            }
            if (pinned.has(name)) return err(`Preset "${name}" is used by an output bank and cannot be deleted`, 400);
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
            const binding = assignDemoBankTarget(body, name);
            if (binding?.error) return err(binding.error, binding.status);
            if (binding?.conflict) return j({ preset: { name }, bank: { assigned: false } }, 409);
            return j({ success: true, preset: { name }, ...(binding ? { bank: binding } : {}) });
        }
        if (p === '/api/dsp/presets/import-bundle' && post) {
            const name = String(body.preset_name || body.name || 'Imported Bundle').trim() || 'Imported Bundle';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [] });
            }
            syncDspToState();
            const binding = assignDemoBankTarget(body, name);
            if (binding?.error) return err(binding.error, binding.status);
            if (binding?.conflict) return j({ preset: { name }, bank: { assigned: false } }, 409);
            return j({ success: true, preset: { name }, ...(binding ? { bank: binding } : {}) });
        }
        if (p === '/api/dsp/presets/import-filter-dual' && post) {
            const name = String(body.preset_name || body.name || 'Dual Filter Preset').trim() || 'Dual Filter Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], convolver: true });
            }
            syncDspToState();
            const binding = assignDemoBankTarget(body, name);
            if (binding?.error) return err(binding.error, binding.status);
            if (binding?.conflict) return j({ preset: { name }, bank: { assigned: false } }, 409);
            return j({ success: true, preset: { name }, ...(binding ? { bank: binding } : {}) });
        }
        if (p === '/api/dsp/presets/import-rew-peq' && post) {
            const name = String(body.preset_name || body.name || 'REW PEQ Preset').trim() || 'REW PEQ Preset';
            if (!dspPresets.find(pr => pr.name === name)) {
                dspPresets.push({ name, filename: name + '.json', path: '/demo/presets/' + name + '.json', source_presets: [], peq: { enabled: true, params: { channelMode: 'dual', eqMode: 'IIR', leftBands: [], rightBands: [] } } });
            }
            syncDspToState();
            const created = { success: true, preset: { name } };
            const binding = assignDemoBankTarget(body, name);
            if (binding && binding.error) return err(binding.error, binding.status);
            if (binding && binding.conflict) {
                created.bank = { assigned: false, created: name };
                return j(created, 409);
            }
            if (binding) created.bank = binding;
            return j(created);
        }

        // ── Measurements ────────────────────────────────────────────────
        // Seeded with the .104 fixtures; demo sweeps reuse them by name.
        if (p === '/api/measurements' || (p === '/api/measurements/settings' && !post)) {
            const payload = {
                status: 'ok',
                storage: { directory: '/home/paul/.config/fxroute/measurements', jobs_directory: '/home/paul/.config/fxroute/measurements/jobs' },
                calibrations: S.demoCalibrationOptions ? S.demoCalibrationOptions.map(c => ({ ...c })) : [],
                house_curves: S.demoHouseCurveOptions ? S.demoHouseCurveOptions.map(c => ({ ...c })) : [],
                active_calibration_file_id: S.demoActiveCalibrationId || '',
                measurement_settings: { ...measurementSettings },
                scope_note: DEMO_MEASUREMENT_SCOPE_NOTE,
            };
            if (p === '/api/measurements') {
                // The real store lists newest first. The alignment takes ship
                // with the demo, so the saved list shows the same Before/After
                // pair a real run leaves behind, area badge included.
                if (SPEAKER_ALIGN_FIXTURE && !S.getSavedMeasurements().some(m => /^Speaker Align /.test(String(m.name || '')))) {
                    const takes = SPEAKER_ALIGN_FIXTURE.takesFor('right');
                    if (takes) S.addSavedMeasurement(takes.after), S.addSavedMeasurement(takes.before);
                }
                const list = [];
                S.getSavedMeasurements().concat(savedMeasurements).forEach(m => { if (!list.find(x => x.id === m.id)) list.push(m); });
                list.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
                payload.measurements = list;
            }
            return j(payload);
        }
        if (p === '/api/measurements/inputs') {
            return j({
                status: 'ok',
                scope_note: DEMO_MEASUREMENT_SCOPE_NOTE,
                modes: [{ id: 'host-local', label: 'Host-local capture', primary: true,
                    available: true, note: 'FXRoute plays and records on the host via PipeWire.' }],
                inputs: CAPTURE_INPUTS.map(input => ({ ...input, available: true, kind: 'pipewire-source' })),
                selection: {
                    input_id: measurementSettings.selectedInputId,
                    persistent_id: measurementSettings.selectedInputKey,
                    configured: !!measurementSettings.selectedInputConfigured,
                    unavailable: false,
                    legacy_input_id: '',
                },
                capture_available: true,
                discovery: { method: 'wpctl status -n + pactl list short sources', source_count: CAPTURE_INPUTS.length },
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
            // The real endpoint accepts a single measurement object or a bulk
            // { measurements: [...] } and answers in kind: a single save
            // returns { measurement }, a bulk save { measurements }.
            if (Array.isArray(body.measurements)) {
                return j({ status: 'ok', measurements: body.measurements.map(m => S.addSavedMeasurement(m)) });
            }
            const single = S.addSavedMeasurement(body.measurement || body);
            return j({ status: 'ok', measurement: single });
        }
        const measFile = p.match(/^\/api\/measurements\/([^/]+)\/file$/);
        if (measFile && !post) {
            // The saved-run title links here, so a stored run has to resolve
            // to its own JSON document like the real FileResponse does. Only
            // stored runs resolve: a deleted one is gone for good.
            const id = measFile[1];
            const stored = S.getSavedMeasurements().find(m => m.id === id)
                || savedMeasurements.find(m => m.id === id);
            if (!stored) return err('Measurement not found', 404);
            return j(stored);
        }
        const measDelete = p.match(/^\/api\/measurements\/([^/]+)$/);
        if (measDelete && method === 'DELETE') {
            const id = measDelete[1];
            savedMeasurements.filter(m => m.id === id).forEach(m => {
                const idx = savedMeasurements.indexOf(m);
                if (idx >= 0) savedMeasurements.splice(idx, 1);
            });
            S.deleteSavedMeasurement(id);
            return j({ status: 'ok', deleted: id });
        }
        if (p === '/api/measurements/merge' && post) {
            // A real merge averages the selected traces and re-tags the result
            // as a merged measurement, so the saved row and the graph describe
            // the average rather than a clone of one source.
            const requested = body.measurementIds || body.measurement_ids;
            if (!Array.isArray(requested)) return err('measurementIds must be an array');
            if (requested.length < 2) return err('Select at least two saved measurements to merge');
            const sources = requested.map(id => S.getSavedMeasurements().find(m => m.id === id) || savedMeasurements.find(m => m.id === id));
            if (sources.some(source => !source)) return err('Measurement not found', 404);
            const name = String(body.name || `Merged ${sources.length} measurements`);
            const merged = S.mergeMeasurements(sources, name);
            return j({ status: 'ok', measurement: S.addSavedMeasurement(merged) });
        }
        if (p === '/api/measurements/settings' && post) {
            // A deliberate capture choice in the measurement setup wins over the
            // device-derived default until the output device changes again. The
            // real endpoint echoes the full settings block and the frontend
            // reads it back, so every channel choice has to round trip.
            const pick = (...keys) => {
                for (const key of keys) {
                    if (body[key] !== undefined) return body[key];
                }
                return undefined;
            };
            const requestedId = String(pick('selectedInputId', 'input_id') ?? '');
            const requested = CAPTURE_INPUTS.find(input => input.id === requestedId);
            if (requested) setMeasurementCaptureInput(requested);
            const channelKeys = {
                selectedMicInputChannel: ['selectedMicInputChannel', 'mic_input_channel'],
                selectedReferenceInputChannel: ['selectedReferenceInputChannel', 'reference_input_channel'],
                selectedReferenceInputChannelLeft: ['selectedReferenceInputChannelLeft', 'reference_input_channel_left'],
                selectedReferenceInputChannelRight: ['selectedReferenceInputChannelRight', 'reference_input_channel_right'],
            };
            for (const [target, keys] of Object.entries(channelKeys)) {
                const value = pick(...keys);
                if (value === undefined) continue;
                measurementSettings[target] = value === null ? '' : String(value);
            }
            const inputKey = pick('selectedInputKey', 'input_key');
            if (inputKey !== undefined) measurementSettings.selectedInputKey = String(inputKey || '');
            const rate = pick('measurementSampleRate', 'measurement_sample_rate');
            if (rate !== undefined) measurementSettings.measurementSampleRate = Number(rate) || 48000;
            return j({ status: 'ok', measurement_settings: { ...measurementSettings } });
        }
        // Calibration + house-curve files: the demo ships the .104 mic
        // calibration as the selected file; uploads/deletes only mutate the
        // in-memory option list and echo the applier shape
        // ({ calibrations, active_calibration_file_id } /
        // { house_curves }) the real frontend consumes.
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
            storeAutoSubJob(autoSubJobs[id]);
            return j({ job: { id, status: 'queued', message: 'Auto Sub Optimize: queued' } });
        }
        // Discovery, like the backend: the newest live or briefly retained
        // job, so a reloaded page reattaches its run.
        if (p === '/api/measurements/auto-sub-optimize/current') {
            return j({ status: 'ok', job: currentAutoSubJobPayload() });
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
            if (autoSubJobs[id]) {
                autoSubJobs[id].status = 'cancelled';
                autoSubJobs[id].finishedAt = Date.now();
                storeAutoSubJob(autoSubJobs[id]);
            }
            return j({ job: { id, status: 'cancelled', message: 'Auto Sub Optimize cancelled.' } });
        }
        if (p === '/api/speaker-align/start' && post) {
            const side = String(body.side || 'left') === 'right' ? 'right' : 'left';
            const id = 'demo_speaker_run_' + (++speakerAlignSeq);
            const fixtureParams = SPEAKER_ALIGN_FIXTURE ? SPEAKER_ALIGN_FIXTURE.paramsFor(side) : null;
            const params = {
                input_id: String(body.input_id || (fixtureParams && fixtureParams.input_id) || 'demo-mic'),
                mic_input_channel: String(body.mic_input_channel ?? (fixtureParams && fixtureParams.mic_input_channel) ?? '1'),
                reference_input_channel: String(body.reference_input_channel ?? ''),
                reference_input_channel_left: body.reference_input_channel_left ?? null,
                reference_input_channel_right: body.reference_input_channel_right ?? null,
                reference_id: String(body.reference_id || (fixtureParams && fixtureParams.reference_id) || ''),
                microphone_position_id: String(body.microphone_position_id || (fixtureParams && fixtureParams.microphone_position_id) || `${side}-fixed-${Date.now()}`),
                sweep_profile: body.sweep_profile ?? null,
                output_key: (fixtureParams && fixtureParams.output_key) || 'demo',
                channels: Number((fixtureParams && fixtureParams.channels) || 4),
                sample_rate_hz: Number((fixtureParams && fixtureParams.sample_rate_hz) || 48000),
            };
            const fixtureResult = (SPEAKER_ALIGN_FIXTURE ? SPEAKER_ALIGN_FIXTURE.runFor(side) : null) || speakerAlignFallbackResult(side);
            if (!fixtureResult.measurements) fixtureResult.measurements = SPEAKER_ALIGN_FIXTURE ? SPEAKER_ALIGN_FIXTURE.takesFor(side) : null;
            // The frozen area context of a take carries the revision the run
            // started from; the commit re-stamps both takes with the new one.
            const startRevision = outputStateStore.revision;
            fixtureResult.proposal.start_revision = startRevision;
            if (fixtureResult.provenance && fixtureResult.provenance.planning) {
                fixtureResult.provenance.planning.start_revision = startRevision;
            }
            for (const take of Object.values(fixtureResult.measurements || {})) {
                if (take && take.measurement_target) take.measurement_target.revision = startRevision;
            }
            speakerAlignJobs[id] = { id, side, dryRun: body.dry_run === true, params, startedAt: Date.now(),
                status: 'running', script: speakerAlignScript(side), fixtureResult };
            return j({ status: 'ok', job: { ...speakerAlignBase(speakerAlignJobs[id]), status: 'queued',
                message: 'Speaker alignment queued.', result: null, error: null } });
        }
        if (p === '/api/speaker-align/jobs' && !post) {
            return j({ status: 'ok', jobs: Object.keys(speakerAlignJobs).map((id) => speakerAlignPayload(id)).filter(Boolean) });
        }
        const speakerJob = p.match(/^\/api\/speaker-align\/jobs\/([^/]+)$/);
        if (speakerJob && !post) {
            const payload = speakerAlignPayload(speakerJob[1]);
            if (!payload) return err('Job not found', 404);
            return j({ status: 'ok', job: payload });
        }
        const speakerCancel = p.match(/^\/api\/speaker-align\/jobs\/([^/]+)\/cancel$/);
        if (speakerCancel && post) {
            const id = speakerCancel[1];
            if (!speakerAlignJobs[id]) return err('Job not found', 404);
            const job = speakerAlignJobs[id];
            // A live job goes to "cancelling" first and settles into
            // "cancelled" a moment later, like the backend's cancel path.
            if (job && job.status === 'running') { job.status = 'cancelling'; job.cancelledAt = Date.now(); }
            return j({ status: 'ok', job: speakerAlignPayload(id) });
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
        // Job discovery, for the contract test: what a reloaded page finds.
        currentAutoSubJobPayload,
        // Speaker align jobs are time-staged, so the test drives them at a
        // fixed elapsed time instead of waiting out the stage script.
        speakerAlignJobPayload: (id, elapsedMs) => speakerAlignPayload(id, elapsedMs),
    };
})();
