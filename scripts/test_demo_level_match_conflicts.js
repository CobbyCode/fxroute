#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Calibration rejection must preserve fetch/client contracts without mutating output state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const outputState = require('../static/output_state.js');

function harness(tree) {
    const ctx = { console, Date, Math, URLSearchParams, FormData,
        setInterval() {}, clearInterval() {},
        fetch() { throw new Error('Unexpected real fetch'); } };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
        'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '..', tree, file), 'utf8'), ctx);
    }
    const request = (url, body) => ctx.fetch(url, body === undefined ? {} : {
        method: 'POST', body: JSON.stringify(body),
    });
    const catalog = () => outputState.fetchCatalog(ctx.fetch);
    return { ctx, request, catalog };
}

const conflict = 'Level Matching configuration changed; measure again';
const cases = [
    ['missing measured revision', async (h, before, mutation) => {
        delete mutation.state_revision;
        const response = await h.request('/api/audio/output-state/apply', {
            expected_revision: before.revision, mutation });
        assert.equal(response.status, 409);
        assert.equal(response.ok, false);
        assert.equal(typeof response.json, 'function');
        assert.deepEqual(JSON.parse(JSON.stringify(await response.json())), { detail: conflict });
    }],
    ['divergent inner revision', async (h, before, mutation) => {
        mutation.state_revision += 1;
        const response = await h.request('/api/audio/output-state/apply', {
            expected_revision: before.revision, mutation });
        assert.equal(response.status, 409);
        assert.equal(response.ok, false);
        assert.equal(typeof response.json, 'function');
        assert.equal((await response.json()).detail, conflict);
    }],
    ['unknown measurement job through real client', async (h, before, mutation) => {
        mutation.measurement_job_id = 'missing-job';
        await assert.rejects(outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation), error => {
            assert.equal(error.status, 409);
            assert.equal(error.data.detail, conflict);
            return true;
        });
    }],
    ['cancelled measurement job through real client', async (h, before, mutation) => {
        await h.request(`/api/measurements/level-match/jobs/${mutation.measurement_job_id}/cancel`, {});
        await assert.rejects(outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation), error => {
            assert.equal(error.status, 409);
            assert.equal(error.data.detail, conflict);
            return true;
        });
    }],
    ['job values mismatch through real client', async (h, before, mutation) => {
        mutation.values = { main_l: { global: -1 } };
        await assert.rejects(outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation), error => {
            assert.equal(error.status, 409);
            assert.equal(error.data.detail, conflict);
            return true;
        });
    }],
    ['job mode mismatch through real client', async (h, before, mutation) => {
        mutation.mode = before.active_mode === 'surround' ? 'stereo' : 'surround';
        await assert.rejects(outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation), error => {
            assert.equal(error.status, 409);
            assert.equal(error.data.detail, conflict);
            return true;
        });
    }],
    ['normal stale client remains guarded without rebase', async (h, before, mutation) => {
        // A negative revision is client-invalid; a later revision still reaches the outer server guard.
        mutation.state_revision += 1;
        await assert.rejects(outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation), error => {
            assert.equal(error.status, 409);
            assert.equal(error.data.detail.code, 'revision-conflict');
            return true;
        });
    }],
    ['valid measured apply still commits', async (h, before, mutation) => {
        const applied = await outputState.applyMutation(h.ctx.fetch, before, h.catalog, mutation);
        assert.equal(applied.data.revision, before.revision + 1);
        const after = await h.catalog();
        for (const [role, slots] of Object.entries(mutation.values)) {
            assert.deepEqual(JSON.parse(JSON.stringify(after.modes[mutation.mode].processing[role].level_cal_db)),
                JSON.parse(JSON.stringify(slots)));
        }
        return true;
    }],
];

(async () => {
    let failures = 0;
    for (const tree of ['demo', 'demo/dist/demo']) {
        for (const [name, run] of cases) {
            try {
                const h = harness(tree);
                const job = await (await h.request('/api/measurements/level-match/start', { bank_id: 'global' })).json();
                const before = await h.catalog();
                const mutation = { kind: 'set_level_calibration', ...job.job.result, measurement_job_id: job.job.id };
                const commits = await run(h, before, mutation);
                if (!commits) assert.equal(JSON.stringify(await h.catalog()), JSON.stringify(before));
                console.log(`PASS ${tree}: ${name}`);
            } catch (error) {
                failures += 1;
                console.error(`FAIL ${tree}: ${name}`, error);
            }
        }
    }
    console.log(`${cases.length * 2} cases; ${failures} failures`);
    if (failures) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
