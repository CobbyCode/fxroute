#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The demo answers the Level Matching reset like the backend: stored levels dropped, 0 dB slots returned.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ctx = { console, Date, Math, URLSearchParams, FormData, setInterval() {}, clearInterval() {},
    fetch() { throw new Error('Unexpected real fetch'); } };
ctx.window = ctx;
vm.createContext(ctx);
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
    'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'demo', file), 'utf8'), ctx);
}
async function request(url, body) {
    const response = await ctx.fetch(url, { method: body ? 'POST' : 'GET', body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, data: JSON.parse(JSON.stringify(await response.json())) };
}

(async () => {
    const started = await request('/api/measurements/level-match/start', { bank_id: 'global' });
    assert.equal(started.status, 200);
    const before = await request('/api/measurements/level-match?bank_id=global');
    assert.ok(before.data.entries.length > 0);

    const reset = await request('/api/measurements/level-match/reset', { bank_id: 'global' });
    assert.equal(reset.status, 200);
    assert.equal(reset.data.mode, before.data.mode);
    assert.deepEqual(Object.keys(reset.data.values).sort(), before.data.entries.map((row) => row.role).sort());
    assert.ok(Object.values(reset.data.values).every((slots) => Object.values(slots).every((db) => db === 0)));
    assert.deepEqual((await request('/api/measurements/level-match?bank_id=global')).data.entries, []);

    // Surround is one family: any group's reset clears every speaker, Front L/R in 'global', the rest in 'bank'.
    const catalog = (await request('/api/audio/output-state')).data;
    const switched = await request('/api/audio/output-state/apply', {
        expected_revision: catalog.revision, mutation: { kind: 'switch_mode', mode: 'surround' } });
    assert.equal(switched.status, 200);
    assert.equal((await request('/api/measurements/level-match/start', { bank_id: 'low' })).status, 200);
    assert.equal((await request('/api/measurements/level-match/start', { bank_id: 'rear' })).status, 200);
    const surround = await request('/api/measurements/level-match/reset', { bank_id: 'rear' });
    assert.equal(surround.status, 200);
    assert.equal(surround.data.mode, 'surround');
    assert.deepEqual(surround.data.values.left_low, { global: 0 });
    assert.deepEqual(surround.data.values.rear_l, { bank: 0 });
    assert.ok(!Object.keys(surround.data.values).some((role) => role.startsWith('sub')));
    assert.deepEqual((await request('/api/measurements/level-match?bank_id=low')).data.entries, []);

    // Sub contexts still match mode speakers; an unknown bank is invalid.
    assert.equal((await request('/api/measurements/level-match/reset', { bank_id: 'sub1' })).status, 200);
    assert.equal((await request('/api/measurements/level-match/reset', { bank_id: 'nonsense' })).status, 400);

    for (const bank_id of ['global', 'all', 'sub1']) {
        const run = await request('/api/measurements/level-match/start', { bank_id });
        assert.equal(run.status, 200);
        assert.equal(run.data.job.result.values.left_low.global, -4);
        assert.equal(run.data.job.result.values.left_high.global, -4);
        assert.equal(run.data.job.result.values.rear_l.bank, 0);
        assert.ok(!('sub1' in run.data.job.result.values));
    }

    const routed = (await request('/api/audio/output-state')).data;
    const assignments = routed.device.routing.surround
        .map(role => role.startsWith('rear') || role.startsWith('side') ? 'off' : role);
    const removed = await request('/api/audio/output-state/apply', {
        expected_revision: routed.revision,
        mutation: { kind: 'set_routing', mode: 'surround', assignments } });
    assert.equal(removed.status, 200);
    const activeStatus = (await request('/api/measurements/level-match?bank_id=global')).data;
    assert.ok(activeStatus.entries.every(row => !/^(rear|side)/.test(row.role)));
    const activeRun = await request('/api/measurements/level-match/start', { bank_id: 'global' });
    assert.equal(activeRun.data.job.result.values.left_low.global, -2.5);
    assert.ok(!('rear_l' in activeRun.data.job.result.values));

    const current = (await request('/api/audio/output-state')).data;
    const centerAssignments = ['left_low', 'left_high', 'right_low', 'right_high',
        'center_low', 'center_mid', 'center_high', 'sub1', ...Array(10).fill('off')];
    assert.equal((await request('/api/audio/output-state/apply', {
        expected_revision: current.revision,
        mutation: { kind: 'set_routing', mode: 'surround', assignments: centerAssignments } })).status, 200);
    const allScope = (await request('/api/measurements/level-match?bank_id=global')).data;
    assert.deepEqual(allScope.units, ['left', 'right', 'center_low']);
    const centerScope = (await request('/api/measurements/level-match?bank_id=center_mid')).data;
    assert.deepEqual(centerScope.units, ['center_low']);
    const centerRun = await request('/api/measurements/level-match/start', { bank_id: 'center_mid' });
    assert.equal(centerRun.status, 200);
    const values = centerRun.data.job.result.values;
    assert.deepEqual(values.center_low, values.center_mid);
    assert.deepEqual(values.center_mid, values.center_high);
    console.log('demo level match ok');
})().catch((error) => { console.error(error); process.exit(1); });
