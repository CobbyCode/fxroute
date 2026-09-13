#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Measurement-setup capture inputs in the web demo: the demo microphone
// presents as UMIK-1 (never "Demo Microphone"), no invented capture
// devices exist, and the Focusrite Scarlett 16i16 external inputs appear
// exclusively in the external-input selection as adjacent stereo pairs,
// exactly like the real overview (audio/samplerate/overview.py) builds
// them. The simulation itself is unchanged.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const SCARLETT_SOURCE = 'alsa_input.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-input';
// Real positional channel suffixes (audio/output_ports.py
// POSITIONAL_CHANNEL_SUFFIXES, then AUX<index>) for a map-less 18-channel
// node, grouped as adjacent stereo pairs like the real overview.
const SCARLETT_PAIRS = [
    [[1, 2], 'FL', 'FR'],
    [[3, 4], 'RL', 'RR'],
    [[5, 6], 'FC', 'LFE'],
    [[7, 8], 'SL', 'SR'],
    [[9, 10], 'AUX0', 'AUX1'],
    [[11, 12], 'AUX2', 'AUX3'],
    [[13, 14], 'AUX4', 'AUX5'],
    [[15, 16], 'AUX14', 'AUX15'],
    [[17, 18], 'AUX16', 'AUX17'],
];

function makeDemoContext() {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date,
        Math,
        console,
        URLSearchParams,
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of [
        'demo/data/library.js',
        'demo/data/library2.js',
        'demo/data/library3.js',
        'demo/data/radio.js',
        'demo/data/measurements.js',
        'demo/state.js',
        'demo/routes.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), ctx);
    }
    return ctx;
}

(async () => {
    const ctx = makeDemoContext();
    const demoFetch = ctx.fetch;
    const state = ctx.FXROUTE_DEMO_STATE;

    // No "Demo Microphone" label and no invented 4i4 capture anywhere in
    // the served demo layer.
    for (const file of ['demo/routes.js', 'demo/state.js']) {
        const source = fs.readFileSync(path.join(root, file), 'utf8');
        assert.ok(!source.includes('Demo Microphone'), file + ' must not mention Demo Microphone');
        assert.ok(!source.includes('Scarlett_4i4'), file + ' must not invent a 4i4 capture');
    }

    // Capture list: UMIK-1 plus the output-derived MOTU/Scarlett captures.
    const inputs = await (await demoFetch('/api/measurements/inputs')).json();
    assert.equal(inputs.capture_available, true);
    assert.equal(JSON.stringify(inputs.inputs.map((i) => i.id)),
        JSON.stringify(['demo_mic', 'alsa_input.usb-MOTU_M4-00.analog-surround-40', SCARLETT_SOURCE]));
    const umik = inputs.inputs.find((i) => i.id === 'demo_mic');
    assert.equal(umik.label, 'UMIK-1');
    assert.equal(umik.channels, 1);
    // MOTU M4: 4-channel capture with split Electrical Ref L / R, like the
    // Scarlett — its line inputs 3/4 carry the reference pair.
    const motu = inputs.inputs.find((i) => i.id === 'alsa_input.usb-MOTU_M4-00.analog-surround-40');
    assert.equal(motu.channels, 4);

    // External-input selection: the Scarlett capture appears as its nine
    // adjacent stereo pairs, like the MOTU M4 pairs.
    const source = await (await demoFetch('/api/audio/source-mode')).json();
    const scarlett = source.inputs.filter((i) => i.source_key === SCARLETT_SOURCE);
    assert.equal(scarlett.length, 9, 'Scarlett offers nine stereo pairs');
    scarlett.forEach((entry, index) => {
        const [[left, right], leftChannel, rightChannel] = SCARLETT_PAIRS[index];
        assert.equal(entry.key, SCARLETT_SOURCE + '::pair:' + left + '-' + right);
        assert.equal(entry.device_label, 'Focusrite Scarlett 16i16');
        assert.equal(entry.label, 'Focusrite Scarlett 16i16 · Input ' + left + '–' + right);
        assert.equal(entry.channels, 18);
        assert.equal(entry.pair_index, index);
        assert.equal(entry.pair_count, 9);
        assert.equal(JSON.stringify(Array.from(entry.pair_channels)), JSON.stringify([left, right]));
        assert.equal(entry.left_channel, leftChannel);
        assert.equal(entry.right_channel, rightChannel);
        assert.equal(entry.selectable, true);
    });
    assert.ok(scarlett.every((entry) => entry.left_channel !== entry.right_channel),
        'never duplicated onto both sides');

    // A Scarlett pair is selectable as external input.
    const selected = await (await demoFetch('/api/audio/source-mode', {
        method: 'POST',
        body: JSON.stringify({ mode: 'external-input', inputKey: SCARLETT_SOURCE + '::pair:3-4' }),
    })).json();
    assert.equal(selected.mode, 'external-input');
    assert.equal(selected.selected_input.key, SCARLETT_SOURCE + '::pair:3-4');
    assert.equal(selected.selected_input.label, 'Focusrite Scarlett 16i16 · Input 3–4');
    assert.equal(JSON.stringify(Array.from(selected.selected_input.pair_channels)), JSON.stringify([3, 4]));
    await demoFetch('/api/audio/source-mode',
        { method: 'POST', body: JSON.stringify({ mode: 'app-playback' }) });

    // MOTU M4 measurement capture: split refs on line inputs 3/4 via the
    // unchanged channel-count logic (no new device, no new channel model).
    const motuSettings = await (await demoFetch('/api/measurements')).json();
    const motuCapture = motuSettings.measurement_settings;
    // Default output is the MOTU M4, so the default capture is its 4ch input.
    assert.equal(motuCapture.selectedInputId, 'alsa_input.usb-MOTU_M4-00.analog-surround-40');
    assert.equal(motuCapture.selectedReferenceInputChannelLeft, '3');
    assert.equal(motuCapture.selectedReferenceInputChannelRight, '4');

    // The simulation itself is unchanged: sweeps start and carry UMIK-1 facts.
    const started = await (await demoFetch('/api/measurements/start',
        { method: 'POST', body: JSON.stringify({ measurement_role: 'single', channel: 'left' }) })).json();
    assert.equal(started.job.status, 'running');
    const sweep = state.makeMeasurement({ name: 'Setup Check', channel: 'left' });
    assert.equal(sweep.input_device.label, 'UMIK-1');
    assert.equal(sweep.input_device.id, 'demo_mic');
    state.stop();

    console.log('ok demo measurement inputs (UMIK-1 + Scarlett external-input pairs)');
})();
