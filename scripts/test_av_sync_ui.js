#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
let ui;
try { ui = require('../static/av_sync.js'); } catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
}

function element() {
    const handlers = {};
    const hidden = new Set(['hidden']);
    return { value: '', textContent: '', innerHTML: '', disabled: false,
        classList: { add: (name) => hidden.add(name), remove: (name) => hidden.delete(name), contains: (name) => hidden.has(name) },
        addEventListener: (name, handler) => { handlers[name] = handler; },
        emit: (name) => handlers[name]?.(), focus() {}, querySelector: () => null };
}

async function main() {
    assert.ok(ui, 'AV Sync panel is not implemented');
    assert.equal(require('../static/output_effects_ui.js').normalizeEffectsUpmixerFftSize(8192), 8192,
        'HQ 8192 must survive the frontend form normalization');
    const elements = Object.fromEntries(['avSyncPanel', 'avSyncOpen', 'avSyncClose', 'avSyncTarget',
        'avSyncMode', 'avSyncActual', 'avSyncLimits', 'avSyncDetails', 'avSyncStatus', 'avSyncBasis', 'avSyncSave']
        .map(name => [name, element()]));
    let payload = { active_mode: 'stereo', revision: 7, settings: { target_ms: 260, basis_origin: 'adopted' },
        latency: { ready: true, target_ms: 260, actual_ms: 260, minimum_ms: 128, maximum_ms: 628,
            input_ms: 0, common_ms: 132, outputs: [{ role: 'main_l', dsp_ms: 128, align_ms: 0, balance_ms: 0, total_ms: 260 }] } };
    const posts = [];
    let rejectSave = false;
    ui.init({ getElements: () => elements, openModal() {}, closeModal() {}, refreshCatalog: async () => {},
        setTimeout: () => 1, clearTimeout() {},
        fetch: async (url, options) => {
            if (options?.method === 'POST') {
                posts.push(JSON.parse(options.body));
                if (rejectSave) return { ok: false, json: async () => ({ detail: 'Revision conflict' }) };
                payload.settings.target_ms = posts.at(-1).mutation.target_ms;
                payload.revision += 1;
                return { ok: true, json: async () => ({ revision: payload.revision, live_applied: true }) };
            }
            return { ok: true, json: async () => structuredClone(payload) };
        } });
    ui.bindEvents();
    await ui.open();
    assert.equal(elements.avSyncPanel.classList.contains('hidden'), false);
    assert.equal(elements.avSyncTarget.value, '260');
    elements.avSyncTarget.value = '300'; elements.avSyncTarget.emit('input');
    payload.latency.actual_ms = 270;
    await ui.refresh();
    assert.equal(elements.avSyncTarget.value, '300', 'polls must not overwrite an edited target');
    await ui.save();
    assert.deepEqual(posts[0], { expected_revision: 7, mutation: { kind: 'set_av_sync', mode: 'stereo', target_ms: 300 } });
    for (const value of ['', '-1', 'NaN', '20001']) {
        elements.avSyncTarget.value = value;
        await ui.save();
    }
    assert.equal(posts.length, 1, 'invalid targets never reach the API');
    await ui.refresh();
    assert.match(elements.avSyncStatus.textContent, /Enter a target/,
        'live polling must not erase input validation feedback');
    elements.avSyncTarget.value = '321'; elements.avSyncTarget.emit('input');
    rejectSave = true;
    await ui.save();
    await ui.refresh();
    assert.equal(elements.avSyncTarget.value, '321', 'failed saves preserve the edited target');
    assert.match(elements.avSyncStatus.textContent, /Revision conflict/,
        'live polling must not erase save errors');
    rejectSave = false;
    payload.active_mode = 'surround'; payload.settings.target_ms = 350;
    elements.avSyncTarget.value = '444'; elements.avSyncTarget.emit('input');
    await ui.refresh();
    assert.equal(elements.avSyncTarget.value, '350');
    assert.match(elements.avSyncMode.textContent, /Surround/);
    payload.latency = { ready: true, actual_ms: 600, target_ms: 350, minimum_ms: 600, maximum_ms: 900,
        common_ms: 0, input_ms: 171, limited: 1, outputs: [] };
    await ui.refresh();
    assert.match(elements.avSyncStatus.textContent, /cannot.*reduce|below.*minimum/i);
    assert.match(elements.avSyncActual.textContent, /600/);
    payload.latency.balance_unavailable = 1;
    await ui.refresh();
    assert.match(elements.avSyncStatus.textContent, /unknown|not guaranteed/i);
    ui.close();
    assert.equal(elements.avSyncPanel.classList.contains('hidden'), true);
    console.log('AV Sync UI tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
