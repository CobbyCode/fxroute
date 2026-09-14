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
async function rawRequest(url, body) {
    const response = await ctx.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
    return { status: response.status, data: JSON.parse(JSON.stringify(await response.json())) };
}
async function request(url, body) {
    const { status, data } = await rawRequest(url, body);
    assert.equal(status, 200);
    return data;
}
(async () => {
    const initial = await request('/api/audio/outputs');
    const scarlett = initial.outputs.find(o => o.label === 'Focusrite Scarlett 16i16');
    const stereo = initial.outputs.find(o => o.channels === 2);
    let output = await request('/api/audio/outputs', { key: scarlett.key });
    assert.equal(output.selected_output.device_profile.active_tier, '18ch');
    // The rate list always spans every band; the tier follows the rate.
    assert.deepEqual(output.selected_output.supported_rates, [44100, 48000, 88200, 96000, 176400, 192000]);
    const assignments = [2, 1, 3, 4, ...Array(13).fill(0), 1];
    output = await request('/api/audio/output-routing', { key: scarlett.key, assignments });
    assert.deepEqual(output.output_mode.output_routing.assignments, assignments);
    // Fixed 192 kHz moves the inventory to the 10-channel tier.
    await request('/api/audio/samplerate', { mode: 'fixed', rate: 192000 });
    output = await request('/api/audio/outputs');
    assert.equal(output.selected_output.channels, 10);
    assert.equal(output.selected_output.device_profile.active_tier, '10ch');
    assert.deepEqual(output.output_mode.output_routing.inactive_assignments, [18]);
    // Fixed 48 kHz moves back to the 18-channel tier, keeping assignments.
    await request('/api/audio/samplerate', { mode: 'fixed', rate: 48000 });
    output = await request('/api/audio/outputs');
    assert.equal(output.selected_output.device_profile.active_tier, '18ch');
    assert.equal(output.selected_output.channels, 18);
    assert.deepEqual(output.output_mode.output_routing.assignments, assignments);
    // Switching to a 2-channel device falls back to Stereo: crossover and sub
    // routing are off, the routing matrix is unavailable, and the switch
    // reports the capability reason like the backend.
    output = await request('/api/audio/outputs', { key: stereo.key });
    assert.equal(output.output_mode.output_routing.available, false);
    assert.equal(output.output_mode.mode, 'stereo');
    assert.equal(output.output_mode.available, false);
    assert.equal(output.output_mode.effective_output_channels, 2);
    const fallback = output.output_mode.mode_adjustment;
    assert.equal(fallback.adjusted, true);
    assert.equal(fallback.reason, 'device-channel-capacity');
    assert.equal(fallback.previous_mode, 'subwoofer-2.2');
    assert.equal(fallback.mode, 'stereo');
    assert.match(fallback.message, /2 channels/);
    // A subwoofer mode is refused on a 2-channel device, like the backend.
    const refused = await rawRequest('/api/audio/output-mode', { mode: 'subwoofer-2.1' });
    assert.equal(refused.status, 400);
    assert.match(refused.data.detail, /at least 4 channels/);
    // No mode was ever applied for the Scarlett, so Stereo stays and nothing
    // is reported as restored: a no-change switch remembers nothing.
    output = await request('/api/audio/outputs', { key: scarlett.key });
    assert.equal(output.output_mode.mode, 'stereo');
    assert.equal(output.output_mode.mode_adjustment, undefined);
    // A mode actually applied for the device is the one restored on return.
    await request('/api/audio/output-mode', { mode: 'subwoofer-2.1' });
    output = await request('/api/audio/outputs', { key: stereo.key });
    assert.equal(output.output_mode.mode, 'stereo');
    assert.equal(output.output_mode.mode_adjustment.reason, 'device-remembered-mode');
    assert.equal(output.output_mode.mode_adjustment.previous_mode, 'subwoofer-2.1');
    output = await request('/api/audio/outputs', { key: scarlett.key });
    assert.equal(output.output_mode.mode, 'subwoofer-2.1');
    assert.equal(output.output_mode.mode_adjustment.reason, 'device-remembered-mode');
    assert.equal(output.output_mode.mode_adjustment.previous_mode, 'stereo');
    console.log('Demo output routing and rate-driven tiers passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
