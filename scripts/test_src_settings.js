#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs');
assert.ok(fs.existsSync(require('node:path').join(__dirname, '../static/src_settings.js')), 'Common SRC Settings is missing');
const src = require('../static/src_settings.js');
const vm = require('node:vm');
const path = require('node:path');

// Execute the actual Settings renderer with a profiled multichannel output.
const appSource = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
function settingsFunction(name) {
    const start = appSource.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `Missing Settings function: ${name}`);
    return appSource.slice(start, appSource.indexOf('\n}\n', start) + 2);
}
const samplerateHint = { textContent: '' };
const settings = {
    elements: { settingsSamplerateHint: samplerateHint },
    state: { settings: { audioOutputs: { selected_output: { device_profile: {
        active_tier: '18ch', tiers: [{ id: '18ch', channels: 18 }],
    } } } }, samplerate: { policy: { mode: 'fixed', rate: 44100 } } },
    renderSourceSection() {}, renderProviderSettings() {}, renderSubwooferPanel() {},
    renderOutputSystemSection() {}, applySourceModeUiState() {},
    SettingsSystem: { renderDeviceNameSettings() {}, renderHardwareController() {}, renderMaintenancePanel() {} },
    FXRouteUiHelpers: require('../static/ui_helpers.js'),
};
vm.createContext(settings);
vm.runInContext(settingsFunction('formatSampleRateKhz') + '\n' + settingsFunction('renderSettingsPanel'), settings);
for (const [rate, label] of [[44100, '44.1 kHz'], [48000, '48 kHz'], [96000, '96 kHz']]) {
    settings.state.samplerate.policy.rate = rate;
    settings.renderSettingsPanel();
    assert.equal(samplerateHint.textContent, `Playback graph and hardware output are fixed at ${label}.`);
}
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
assert.doesNotMatch(html, /<button[^>]*id="settings-src-restart"/);

(async () => {
    const select = { value: '', disabled: false };
    const hint = { textContent: '' };
    const guidance = 'Low: ~70% / 0.45× CPU · Standard: ~81,9% / 1.0× CPU · High: ~99.6% / 1.5× CPU';
    let response = { ok: true, json: async () => ({ mode: 'standard', available: true }) };
    const errors = [];
    const requests = [];
    src.init({ select, hint, fetchFn: async (url, options) => { requests.push({ url, options }); return response; },
        onError: message => errors.push(message) });
    await src.refresh();
    assert.equal(select.value, 'standard');
    assert.equal(select.disabled, false);
    assert.equal(hint.textContent, guidance);
    response = { ok: false, json: async () => ({ detail: 'Measurement is active; SRC mode is locked' }) };
    select.value = 'high';
    await src.save('high');
    assert.equal(select.value, 'standard');
    assert.equal(errors.at(-1), 'Measurement is active; SRC mode is locked');
    assert.equal(hint.textContent, guidance);
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), { mode: 'high' });
    // A non-JSON error body (plain-text 500, proxy page) must not surface as a parse error.
    const plainText = { ok: false, status: 500, json: async () => { throw new SyntaxError('Unexpected token \'I\''); } };
    response = plainText;
    await src.save('high');
    assert.equal(errors.at(-1), 'Cannot apply SRC mode');
    assert.equal(select.disabled, false);
    await src.refresh();
    assert.equal(hint.textContent, guidance);
    response = { ok: true, json: async () => ({ mode: 'standard', available: true }) };
    await src.refresh();
    response = { ok: true, json: async () => ({ mode: 'high', available: true }) };
    await src.save('high');
    assert.equal(select.value, 'high');
    assert.equal(select.disabled, false);
    response = { ok: true, json: async () => ({ mode: 'high', available: false, error: 'Converter failed' }) };
    await src.refresh();
    assert.equal(hint.textContent, guidance);
    assert.equal(select.disabled, true);
    response = { ok: true, json: async () => ({ mode: 'high', available: false, runtime_ready: true, restart_required: true }) };
    await src.refresh();
    assert.equal(select.disabled, false);
    assert.equal(hint.textContent, guidance);
    for (const mode of ['low', 'standard', 'high']) {
        response = { ok: true, json: async () => ({ mode, available: true }) };
        await src.save(mode);
        assert.equal(select.value, mode);
        assert.equal(hint.textContent, guidance);
        assert.deepEqual(JSON.parse(requests.at(-1).options.body), { mode });
    }
    let finish;
    src.init({ fetchFn: () => new Promise(resolve => { finish = resolve; }) });
    const applying = src.save('low');
    assert.equal(select.disabled, true);
    assert.equal(hint.textContent, guidance);
    src.update({ mode: 'high', available: true });
    finish({ ok: true, json: async () => ({ mode: 'low', available: true }) });
    await applying;
    assert.equal(select.value, 'low');
    assert.equal(select.disabled, false);
    const context = { console, URLSearchParams, FormData, Date, Math, setInterval() {}, clearInterval() {},
        fetch: async () => { throw new Error('Unexpected real request'); }, sessionStorage: { getItem() {}, setItem() {} } };
    context.window = context;
    vm.createContext(context);
    for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
        'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../demo', file), 'utf8'), context);
    }
    const demoDefault = await (await context.fetch('/api/audio/src')).json();
    assert.equal(demoDefault.mode, 'standard');
    const demoTracks = await (await context.fetch('/api/tracks')).json();
    await context.fetch('/api/play', { method: 'POST', body: JSON.stringify({ source: 'local', track_id: demoTracks[0].id }) });
    assert.equal((await (await context.fetch('/api/status')).json()).playing, true);
    const demoHigh = await (await context.fetch('/api/audio/src', {
        method: 'POST', body: JSON.stringify({ mode: 'high' }),
    })).json();
    assert.equal(demoHigh.mode, 'high');
    assert.equal(demoHigh.simulated, true);
    assert.equal(demoHigh.apply_requires_stopped_playback, false);
    assert.equal((await (await context.fetch('/api/status')).json()).playing, false);
    console.log('common SRC Settings: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
