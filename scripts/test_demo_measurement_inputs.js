#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Measurement-setup capture inputs in the web demo: the demo microphone
// presents as UMIK-1 (never "Demo Microphone"), a 4-channel Focusrite
// capture is selectable so the mic channel offers the realistic
// Input 1-4 range, and the simulation itself is unchanged.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const FOCUSRITE_4CH_ID = 'alsa_input.usb-Focusrite_Scarlett_4i4_4th_Gen-00.analog-surround-40';

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

// Mic channel options exactly like the frontend renders them
// (static/app.js renderMeasurementPanelInputsSection).
const micChannelOptions = (channelCount) =>
    Array.from({ length: channelCount }, (_, index) => 'Input ' + (index + 1));

(async () => {
    const ctx = makeDemoContext();
    const demoFetch = ctx.fetch;
    const state = ctx.FXROUTE_DEMO_STATE;

    // No "Demo Microphone" label anywhere in the served demo layer.
    for (const file of ['demo/routes.js', 'demo/state.js']) {
        const source = fs.readFileSync(path.join(root, file), 'utf8');
        assert.ok(!source.includes('Demo Microphone'), file + ' must not mention Demo Microphone');
    }

    const inputs = await (await demoFetch('/api/measurements/inputs')).json();
    assert.equal(inputs.capture_available, true);
    const umik = inputs.inputs.find((i) => i.id === 'demo_mic');
    assert.ok(umik, 'UMIK-1 stays selectable under its stable demo_mic id');
    assert.equal(umik.label, 'UMIK-1');
    assert.equal(umik.channels, 1);
    assert.deepEqual(micChannelOptions(umik.channels), ['Input 1']);

    const focusrite = inputs.inputs.find((i) => i.id === FOCUSRITE_4CH_ID);
    assert.ok(focusrite, 'a 4-channel Focusrite capture is offered');
    assert.equal(focusrite.channels, 4);
    assert.ok(String(focusrite.label).includes('Focusrite'), 'Focusrite label reads like the real capture list');
    assert.deepEqual(micChannelOptions(focusrite.channels), ['Input 1', 'Input 2', 'Input 3', 'Input 4']);

    // The 4-channel Focusrite is selectable in the setup; the settings echo it.
    const select = await demoFetch('/api/measurements/settings',
        { method: 'POST', body: JSON.stringify({ selectedInputId: FOCUSRITE_4CH_ID }) });
    assert.equal(select.status, 200);
    const selected = await (await demoFetch('/api/measurements/inputs')).json();
    assert.equal(selected.selection.input_id, FOCUSRITE_4CH_ID);
    const settings = await (await demoFetch('/api/measurements')).json();
    assert.equal(settings.measurement_settings.selectedInputId, FOCUSRITE_4CH_ID);
    // Four channels use the split electrical reference pair, like every
    // capture with three or more channels in the unchanged frontend logic.
    assert.equal(settings.measurement_settings.selectedReferenceInputChannelLeft, '3');
    assert.equal(settings.measurement_settings.selectedReferenceInputChannelRight, '4');

    // The simulation itself is unchanged: sweeps start and carry UMIK-1 facts.
    const started = await (await demoFetch('/api/measurements/start',
        { method: 'POST', body: JSON.stringify({ measurement_role: 'single', channel: 'left' }) })).json();
    assert.equal(started.job.status, 'running');
    const sweep = state.makeMeasurement({ name: 'Setup Check', channel: 'left' });
    assert.equal(sweep.input_device.label, 'UMIK-1');
    assert.equal(sweep.input_device.id, 'demo_mic');
    state.stop();

    console.log('ok demo measurement inputs (UMIK-1 + Focusrite Input 1-4)');
})();
