#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const { assignGlobalPreset } = require('./demo_preset_test_support.js');
const ctx = { console, Date, Math, URLSearchParams, FormData, setInterval() {}, clearInterval() {},
    fetch() { throw new Error('Unexpected real fetch'); } };
ctx.window = ctx;
vm.createContext(ctx);
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
    'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'demo', file), 'utf8'), ctx);
}
async function get(url) { return (await ctx.fetch(url)).json(); }
async function apply(mutation) {
    const state = await get('/api/audio/output-state');
    const result = await ctx.fetch('/api/audio/output-state/apply', { method: 'POST',
        body: JSON.stringify({ expected_revision: state.revision, mutation }) });
    assert.equal(result.status, 200);
}
(async () => {
    const state = await get('/api/audio/output-state');
    assert.equal((await get('/api/dsp/presets')).active_preset,
        state.modes['stereo-sub'].banks.global.preset, 'status derives the active mode Global slot');
    await assignGlobalPreset(ctx.fetch, '+6');
    assert.equal((await get('/api/dsp/presets')).active_preset, '+6');
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'global',
        preset_a: 'Direct', preset_b: '+6', active_side: 'A' });
    assert.equal((await get('/api/dsp/presets')).active_preset, 'Direct');
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'global', active_side: 'B' });
    assert.equal((await get('/api/dsp/presets')).active_preset, '+6');
    await apply({ kind: 'switch_mode', mode: 'stereo' });
    assert.equal((await get('/api/dsp/presets')).active_preset, 'Neutral');
    await apply({ kind: 'switch_mode', mode: 'stereo-sub' });
    assert.equal((await get('/api/dsp/presets')).active_preset, '+6');
    console.log('Demo preset ownership tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
