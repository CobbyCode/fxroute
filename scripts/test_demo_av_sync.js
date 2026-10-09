#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const ctx = { console, Date, Math, URLSearchParams, FormData, setInterval() {}, clearInterval() {},
    fetch() { throw new Error('Unexpected real fetch'); } };
ctx.window = ctx; vm.createContext(ctx);
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
    'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'demo', file), 'utf8'), ctx);
}
async function request(url, body) {
    const response = await ctx.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
    const data = JSON.parse(JSON.stringify(await response.json()));
    assert.equal(response.status, 200, JSON.stringify(data));
    return data;
}
(async () => {
    await request('/api/dsp/presets/load', { preset_name: 'Neutral' });
    await request('/api/dsp/extras', { loudness_enabled: true, loudnessFftSize: 8192,
        limiter_enabled: false, tone_effect_enabled: false });
    const initialTiming = await request('/api/audio/av-sync');
    assert(initialTiming.latency.minimum_ms > 100);
    await request('/api/dsp/extras', { loudness_enabled: false });
    const reducedTiming = await request('/api/audio/av-sync');
    assert.equal(reducedTiming.latency.minimum_ms, 0,
        'Removing common DSP latency must not create relative balance');
    assert(reducedTiming.latency.outputs.every(row => row.balance_ms === 0));
    let catalog = await request('/api/audio/output-state');
    for (const mode of ['stereo', 'stereo-sub', 'surround']) {
        await request('/api/audio/output-state/apply', { expected_revision: catalog.revision,
            mutation: { kind: 'set_av_sync', mode, target_ms: mode === 'surround' ? 350 : 260 } });
        catalog = await request('/api/audio/output-state');
        await request('/api/audio/output-state/apply', { expected_revision: catalog.revision,
            mutation: { kind: 'switch_mode', mode } });
        catalog = await request('/api/audio/output-state');
        const timing = await request('/api/audio/av-sync');
        assert.equal(timing.active_mode, mode);
        assert.equal(timing.settings.target_ms, mode === 'surround' ? 350 : 260);
        assert.equal(timing.latency.ready, true);
        assert.equal(timing.latency.actual_ms, timing.settings.target_ms);
    }
    console.log('Demo AV Sync tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
