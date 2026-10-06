#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The demo answers the Polarity Check routes like the backend: one verdict per speaker, no persistence.

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
    const response = await ctx.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
    return { status: response.status, data: JSON.parse(JSON.stringify(await response.json())) };
}

(async () => {
    const idle = await request('/api/measurements/polarity-check?bank_id=global');
    assert.equal(idle.status, 200);
    assert.deepEqual(idle.data.units, ['left', 'right']);
    assert.equal(idle.data.job, null);

    const started = await request('/api/measurements/polarity-check/start', { bank_id: 'global' });
    assert.equal(started.status, 200);
    const job = started.data.job;
    assert.equal(job.status, 'completed');
    assert.deepEqual(job.result.rows.map((row) => row.roles), [['left_low', 'left_high'], ['right_low', 'right_high']]);
    assert.ok(job.result.rows.every((row) => ['correct', 'reversed', 'inconclusive'].includes(row.verdict)));
    assert.equal(job.result.all_reversed, false);

    assert.equal((await request(`/api/measurements/polarity-check/jobs/${job.id}`)).data.job.id, job.id);
    assert.equal((await request(`/api/measurements/polarity-check/jobs/${job.id}/cancel`, {})).status, 200);
    assert.equal((await request('/api/measurements/polarity-check/jobs/missing')).status, 404);
    assert.equal((await request('/api/measurements/polarity-check?bank_id=global')).data.job.id, job.id);

    // Subwoofers belong to Auto Sub Optimize, an unknown bank is no speaker bank.
    assert.equal((await request('/api/measurements/polarity-check?bank_id=sub1')).status, 400);
    assert.equal((await request('/api/measurements/polarity-check/start', { bank_id: 'nonsense' })).status, 400);

    console.log('demo polarity check ok');
})().catch((error) => { console.error(error); process.exit(1); });
