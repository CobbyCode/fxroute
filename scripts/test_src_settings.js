#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs');
assert.ok(fs.existsSync(require('node:path').join(__dirname, '../static/src_settings.js')), 'Common SRC Settings is missing');
const src = require('../static/src_settings.js');

(async () => {
    const select = { value: '', disabled: false };
    const hint = { textContent: '' };
    const restart = { hidden: true, disabled: false };
    let response = { ok: true, json: async () => ({ mode: 'standard', available: true }) };
    const errors = [];
    const requests = [];
    src.init({ select, hint, restart, fetchFn: async (url, options) => { requests.push({ url, options }); return response; },
        onError: message => errors.push(message) });
    await src.refresh();
    assert.equal(select.value, 'standard');
    assert.equal(select.disabled, false);
    response = { ok: false, json: async () => ({ detail: 'Stop playback before changing SRC' }) };
    select.value = 'high';
    await src.save('high');
    assert.equal(select.value, 'standard');
    assert.equal(errors.at(-1), 'Stop playback before changing SRC');
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), { mode: 'high' });
    // A non-JSON error body (plain-text 500, proxy page) must not surface as a parse error.
    const plainText = { ok: false, status: 500, json: async () => { throw new SyntaxError('Unexpected token \'I\''); } };
    response = plainText;
    await src.save('high');
    assert.equal(errors.at(-1), 'Cannot apply SRC mode');
    assert.equal(select.disabled, false);
    await src.refresh();
    assert.equal(hint.textContent, 'Cannot read SRC mode');
    response = { ok: true, json: async () => ({ mode: 'standard', available: true }) };
    await src.refresh();
    response = { ok: true, json: async () => ({ mode: 'high', available: true }) };
    await src.save('high');
    assert.equal(select.value, 'high');
    assert.equal(select.disabled, false);
    response = { ok: true, json: async () => ({ mode: 'high', available: false, error: 'Converter failed' }) };
    await src.refresh();
    assert.match(hint.textContent, /Converter failed/);
    assert.equal(select.disabled, true);
    response = { ok: true, json: async () => ({ mode: 'high', available: false, runtime_ready: true, restart_required: true }) };
    await src.refresh();
    assert.equal(select.disabled, false);
    assert.equal(restart.hidden, false);
    const vm = require('node:vm');
    const path = require('node:path');
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
    const demoHigh = await (await context.fetch('/api/audio/src', {
        method: 'POST', body: JSON.stringify({ mode: 'high' }),
    })).json();
    assert.equal(demoHigh.mode, 'high');
    assert.equal(demoHigh.simulated, true);
    console.log('common SRC Settings: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
