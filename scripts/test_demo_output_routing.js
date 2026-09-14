#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ctx = { console, Date, Math, URLSearchParams, FormData, setInterval() {}, clearInterval() {},
    fetch() { throw new Error('Unexpected real fetch'); } };
ctx.window = ctx;
vm.createContext(ctx);
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js', 'data/measurements.js', 'state.js', 'routes.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'demo', file), 'utf8'), ctx);
}
async function request(url, body) {
    const response = await ctx.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
    assert.equal(response.status, 200);
    return JSON.parse(JSON.stringify(await response.json()));
}
(async () => {
    const initial = await request('/api/audio/outputs');
    const scarlett = initial.outputs.find(o => o.label === 'Focusrite Scarlett 16i16');
    const stereo = initial.outputs.find(o => o.channels === 2);
    let output = await request('/api/audio/outputs', { key: scarlett.key });
    assert.equal(output.selected_output.device_profile.active_tier, '18ch');
    const assignments = [2, 1, 3, 4, ...Array(13).fill(0), 1];
    output = await request('/api/audio/output-routing', { key: scarlett.key, assignments });
    assert.deepEqual(output.output_mode.output_routing.assignments, assignments);
    await request('/api/audio/samplerate', { mode: 'fixed', rate: 44100 });
    output = await request('/api/audio/channel-tier', { key: scarlett.key, tier: '10ch' });
    assert.equal(output.selected_output.channels, 10);
    assert.deepEqual(output.selected_output.supported_rates, [44100, 48000, 88200, 96000, 176400, 192000]);
    assert.deepEqual(output.output_mode.output_routing.inactive_assignments, [18]);
    assert.deepEqual((await request('/api/audio/samplerate')).policy, { mode: 'fixed', rate: 44100 });
    output = await request('/api/audio/channel-tier', { key: scarlett.key, tier: '14ch' });
    assert.deepEqual(output.selected_output.supported_rates, [44100, 48000, 88200, 96000]);
    output = await request('/api/audio/channel-tier', { key: scarlett.key, tier: '18ch' });
    assert.deepEqual(output.output_mode.output_routing.assignments, assignments);
    output = await request('/api/audio/outputs', { key: stereo.key });
    assert.equal(output.output_mode.output_routing.available, false);
    console.log('Demo output routing and channel tiers passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
