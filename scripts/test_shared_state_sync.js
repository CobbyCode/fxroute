#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Exercise incoming snapshots and real effects controls, including crossed requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const effectsSource = fs.readFileSync(path.join(__dirname, '../static/output_effects_ui.js'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const dispatcher = appSource.slice(appSource.indexOf('function handleWebSocketMessage('), appSource.indexOf('// Tab navigation'));
const copy = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));

function snapshot(enabled = false) {
    return { available: true, presets: [], global_extras: {
        limiter: { enabled, params: {} }, headroom: { enabled, params: { gainDb: -6 } },
        autogain: { enabled, params: { targetDb: -23 } },
        loudness: { enabled, params: { strength: 7, fftSize: 8192 } },
        bass_enhancer: { enabled, params: { amount: 3.5 } },
        tone_effect: { enabled, mode: 'maximizer' },
        upmixer: { enabled, params: { mode: 'hq', center: 4, surround: 8, fftSize: 8192 } },
    } };
}

function harness() {
    const requests = [], timers = new Map(), refreshes = [], activeEditing = new Set();
    let timerId = 0;
    const classes = () => ({ hidden: true, toggle(_name, hidden) { this.hidden = hidden; } });
    const elements = {};
    for (const name of ['Limiter', 'Headroom', 'Autogain', 'Loudness', 'Bass', 'ToneEffect', 'Upmixer']) {
        elements[`effects${name}Enabled`] = { checked: false };
        elements[`effects${name}Details`] = { classList: classes() };
    }
    for (const name of ['HeadroomGainDb', 'AutogainTargetDb', 'LoudnessStrength', 'LoudnessFftSize',
        'BassAmount', 'ToneEffectMode', 'UpmixerMode', 'UpmixerCenter', 'UpmixerSurround', 'UpmixerFftSize']) {
        elements[`effects${name}`] = { value: '' };
    }
    const state = { dsp: { combineDraft: { presetName: 'local draft' }, assistStack: ['local'] },
        outputSystem: { catalog: { active_mode: 'surround' } } };
    const context = vm.createContext({ state, elements, console, setTimeout, clearTimeout,
        fetch: async (url, options) => {
            const request = { url, method: options?.method || 'GET', body: options?.body && JSON.parse(options.body) };
            requests.push(request);
            return new Promise(resolve => { request.resolve = (data, ok = true) => resolve({ ok, json: async () => copy(data) }); });
        },
        fetchOutputSystemCatalog: async force => { refreshes.push(force); },
        FXRouteUiHelpers: { roundToDecimals: (v, n) => Math.round(v * 10 ** n) / 10 ** n },
        FXRouteBankUI: { getDefaultEffectsCombineDraft: () => ({}), normalizeEffectsCombineDraft: d => d,
            renderEffectsCompare() {}, renderEffectsCombine() {} },
    });
    context.window = context;
    context.setTimeout = (fn) => { timers.set(++timerId, fn); return timerId; };
    context.clearTimeout = id => timers.delete(id);
    vm.runInContext(effectsSource, context);
    context.EffectsUI = context.FXRouteEffectsUI;
    context.EffectsUI.init({ getState: () => state, getElements: () => elements, getActiveEditing: () => activeEditing });
    context.resyncAfterReconnect = () => context.EffectsUI.fetchEffects();
    vm.runInContext(dispatcher, context);
    return { context, state, elements, requests, refreshes, timers, activeEditing,
        push: data => context.handleWebSocketMessage({ type: 'dsp', data: copy(data) }),
        async seed() {
            const read = context.EffectsUI.fetchEffects();
            requests.at(-1).resolve(snapshot());
            await read;
        },
    };
}

test('live DSP frames repaint every effect and its details without write-back', async () => {
    const h = harness(); await h.seed();
    h.push(snapshot(true));
    for (const name of ['Limiter', 'Headroom', 'Autogain', 'Loudness', 'Bass', 'ToneEffect', 'Upmixer']) {
        assert.equal(h.elements[`effects${name}Enabled`].checked, true, `${name} must follow backend`);
    }
    assert.equal(Number(h.elements.effectsLoudnessStrength.value), 7);
    assert.equal(h.elements.effectsUpmixerMode.value, 'hq');
    assert.equal(h.elements.effectsLoudnessDetails.classList.hidden, false);
    assert.equal(h.elements.effectsBassAmount.value, '3.5');
    assert.equal(h.state.dsp.combineDraft.presetName, 'local draft');
    assert.deepEqual(h.state.dsp.assistStack, ['local']);
    h.push(snapshot(false));
    assert.equal(h.elements.effectsUpmixerEnabled.checked, false);
    assert.equal(h.elements.effectsLoudnessDetails.classList.hidden, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
    assert.equal(h.timers.size, 0, 'rendering must not schedule a save');
    assert.deepEqual(h.refreshes, [true, true], 'bank assignments follow DSP mutations too');
});

test('late initial/reconnect reads cannot undo a newer DSP push', async () => {
    const h = harness(); await h.seed();
    const read = h.context.EffectsUI.fetchEffects();
    h.push(snapshot(true));
    h.requests.at(-1).resolve(snapshot(false));
    await read;
    assert.equal(h.state.dsp.global_extras.loudness.enabled, true);
    assert.equal(h.elements.effectsLoudnessEnabled.checked, true);
});

test('pending local changes merge remote fields and POST only the local delta', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsLoudnessEnabled.checked = true;
    h.context.EffectsUI.saveEffectsExtrasDebounced(800);
    const remote = snapshot(); remote.global_extras.upmixer.enabled = true;
    h.push(remote);
    assert.equal(h.elements.effectsLoudnessEnabled.checked, true, 'retain the pending local toggle');
    assert.equal(h.elements.effectsUpmixerEnabled.checked, true, 'apply unrelated remote changes immediately');
    h.context.clearTimeout([...h.timers.keys()][0]);
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    const request = h.requests.at(-1);
    assert.deepEqual(request.body, { loudnessEnabled: true });
    remote.global_extras.loudness.enabled = true;
    h.push(remote);
    request.resolve({ extras: remote.global_extras }); await save;
    assert.equal(h.elements.effectsUpmixerEnabled.checked, true);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
});

test('a delayed save response cannot undo a newer push or lose edits queued in flight', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsLoudnessEnabled.checked = true;
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    const request = h.requests.at(-1);
    h.elements.effectsBassEnabled.checked = true;
    h.context.EffectsUI.saveEffectsExtrasDebounced(800);
    const remote = snapshot(); remote.global_extras.upmixer.enabled = true;
    remote.global_extras.loudness.enabled = true;
    h.push(remote);
    const oldResponse = snapshot(); oldResponse.global_extras.loudness.enabled = true;
    request.resolve({ extras: oldResponse.global_extras }); await save;
    assert.equal(h.state.dsp.global_extras.upmixer.enabled, true);
    assert.equal(h.elements.effectsUpmixerEnabled.checked, true);
    assert.equal(h.elements.effectsBassEnabled.checked, true);
});

test('output-state notifications refresh shared settings through read APIs', async () => {
    const h = harness(); await h.seed();
    h.context.handleWebSocketMessage({ type: 'output_state', data: { revision: 12 } });
    await tick();
    assert.deepEqual(h.refreshes, [true]);
    assert.equal(h.requests.at(-1).url, '/api/dsp/presets');
    h.requests.at(-1).resolve(snapshot(true)); await tick();
    assert.equal(h.elements.effectsUpmixerEnabled.checked, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('normalized save results replace the local value without another POST', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsBassAmount.value = '200';
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    const result = snapshot(); result.global_extras.bass_enhancer.params.amount = 100;
    h.requests.at(-1).resolve({ extras: result.global_extras }); await save;
    assert.equal(h.elements.effectsBassAmount.value, '100');
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
    assert.equal(h.timers.size, 0);
});

test('focusing and leaving an unchanged value never writes the shared settings', async () => {
    const h = harness(); await h.seed();
    h.context.EffectsUI.saveEffectsExtrasDebounced(0);
    await tick();
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('a newer remote change to the same field wins over a delayed save response', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsLoudnessEnabled.checked = true;
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    h.push(snapshot(false));
    const oldResult = snapshot(true);
    h.requests.at(-1).resolve({ extras: oldResult.global_extras }); await save;
    assert.equal(h.state.dsp.global_extras.loudness.enabled, false);
    assert.equal(h.elements.effectsLoudnessEnabled.checked, false);
});

for (const newestFirst of [false, true]) {
    test(`overlapping reads retain the newest requested snapshot, newestFirst=${newestFirst}`, async () => {
        const h = harness(); await h.seed();
        const first = h.context.EffectsUI.fetchEffects();
        const older = h.requests.at(-1);
        const second = h.context.EffectsUI.fetchEffects();
        const newer = h.requests.at(-1);
        if (newestFirst) {
            newer.resolve(snapshot(true)); await second;
            older.resolve(snapshot(false)); await first;
        } else {
            older.resolve(snapshot(false)); await first;
            newer.resolve(snapshot(true)); await second;
        }
        assert.equal(h.state.dsp.global_extras.loudness.enabled, true);
        assert.equal(h.elements.effectsLoudnessEnabled.checked, true);
    });
}

test('rejected edits return to backend state without an automatic retry', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsToneEffectEnabled.checked = true;
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    h.requests.at(-1).resolve({ detail: 'DSP state is locked' }, false);
    await save;
    const refresh = h.requests.at(-1);
    assert.equal(refresh.method, 'GET');
    refresh.resolve(snapshot()); await tick();
    assert.equal(h.elements.effectsToneEffectEnabled.checked, false);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
    assert.equal(h.timers.size, 0);
});

test('a rejected focused numeric edit returns to backend state before blur', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsUpmixerCenter.value = '9';
    h.activeEditing.add(h.elements.effectsUpmixerCenter);
    const save = h.context.EffectsUI._doSaveEffectsExtras();
    h.requests.at(-1).resolve({ detail: 'DSP state is locked' }, false);
    await save;
    h.requests.at(-1).resolve(snapshot()); await tick();
    assert.equal(Number(h.elements.effectsUpmixerCenter.value), 4);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
});

test('the actual init dispatcher refreshes settings missed before the socket became ready', async () => {
    const h = harness(); await h.seed();
    const before = h.requests.length;
    h.context.handleWebSocketMessage({ type: 'init', data: {} });
    assert.equal(h.requests.length, before + 1);
    h.requests.at(-1).resolve(snapshot(true)); await tick();
    assert.equal(h.elements.effectsLoudnessEnabled.checked, true);
    assert.equal(h.elements.effectsUpmixerEnabled.checked, true);
});

test('focused but unedited controls still follow remote values', async () => {
    const h = harness(); await h.seed();
    h.activeEditing.add(h.elements.effectsHeadroomGainDb);
    h.activeEditing.add(h.elements.effectsUpmixerCenter);
    const remote = snapshot();
    remote.global_extras.headroom.params.gainDb = -8;
    remote.global_extras.upmixer.params.center = 9;
    h.push(remote);
    assert.equal(Number(h.elements.effectsHeadroomGainDb.value), -8);
    assert.equal(Number(h.elements.effectsUpmixerCenter.value), 9);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('an actual pending numeric edit keeps its text while unrelated remote settings update', async () => {
    const h = harness(); await h.seed();
    h.elements.effectsUpmixerCenter.value = '9.';
    h.activeEditing.add(h.elements.effectsUpmixerCenter);
    h.context.EffectsUI.saveEffectsExtrasDebounced(800);
    h.push(snapshot(true));
    assert.equal(h.elements.effectsUpmixerCenter.value, '9.');
    assert.equal(h.elements.effectsLoudnessEnabled.checked, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('clearing a focused zero-valued field preserves unfinished text without a write', async () => {
    const h = harness(); await h.seed();
    const zero = snapshot(); zero.global_extras.bass_enhancer.params.amount = 0;
    h.push(zero);
    h.elements.effectsBassAmount.value = '';
    h.activeEditing.add(h.elements.effectsBassAmount);
    h.context.EffectsUI.saveEffectsExtrasDebounced(800);
    h.push(snapshot(true));
    assert.equal(h.elements.effectsBassAmount.value, '');
    await h.context.EffectsUI._doSaveEffectsExtras();
    assert.equal(h.elements.effectsBassAmount.value, '', 'no-op save keeps unfinished text');
    h.activeEditing.delete(h.elements.effectsBassAmount);
    h.context.EffectsUI.saveEffectsExtrasDebounced(0);
    assert.equal(h.elements.effectsBassAmount.value, '3.5', 'blur returns to the current backend value');
    assert.ok(h.requests.every(r => r.method === 'GET'));
});
