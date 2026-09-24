#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Subwoofer tile status lifecycle is error-only by design: no Applying… or
// Saved hints are ever written; a failure reports its message persistently
// and the next successful commit clears it again. Runs against the real
// static/subwoofer_ui.js save queue; app.js keeps a delegating wrapper.
const assert = require('node:assert/strict');

const SubwooferUI = require('../static/subwoofer_ui.js');

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function makeContext(applyImpl) {
    const feedbackEl = { textContent: '', className: '' };
    const linkEl = { checked: true };
    const activeEditing = new Set([linkEl]);
    SubwooferUI.init({
        getState: () => ({ outputSystem: { catalog: { active_mode: 'stereo-sub' } } }),
        getElements: () => ({ effectsSubwooferFeedback: feedbackEl, effectsSubwooferLink: linkEl }),
        getActiveEditing: () => activeEditing,
        applyMutation: applyImpl,
    });
    return { feedbackEl, activeEditing, linkEl };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const pending = { mode: 'stereo-sub', settings: { mode: 'stereo-sub' }, signature: '{"s":1}' };

(async () => {
    // 1. Success stays silent: no Applying… while in flight, no Saved hint
    // on commit; the guard still releases.
    {
        const gate = deferred();
        const { feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = SubwooferUI.beginSubwooferSave(pending);
        await tick();
        assert.equal(feedbackEl.textContent, '');
        assert.doesNotMatch(feedbackEl.className, /success|error/);
        // The in-flight link toggle stays guarded until the save landed.
        assert.equal(activeEditing.has(linkEl), true);
        gate.resolve({ revision: 9 });
        assert.deepEqual(await run, { revision: 9 });
        await tick();
        assert.equal(feedbackEl.textContent, '');
        assert.doesNotMatch(feedbackEl.className, /success|error/);
        assert.equal(activeEditing.has(linkEl), false);
    }
    // 2. Rejection: error message + error class.
    {
        const gate = deferred();
        const { feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = SubwooferUI.beginSubwooferSave(pending);
        await tick();
        assert.equal(feedbackEl.textContent, '');
        gate.reject(new Error('boom-423'));
        await assert.rejects(run, /boom-423/);
        await tick();
        assert.match(feedbackEl.textContent, /boom-423/);
        assert.match(feedbackEl.className, /error/);
        // A failed save must release the guard too, or the tile would stay
        // un-editable.
        assert.equal(activeEditing.has(linkEl), false);
    }
    // 3. Superseded (null result): error state, rejection surfaces.
    {
        const { feedbackEl } = makeContext(async () => null);
        const run = SubwooferUI.beginSubwooferSave(pending);
        await assert.rejects(run, /superseded/);
        await tick();
        assert.match(feedbackEl.textContent, /superseded/);
        assert.match(feedbackEl.className, /error/);
    }
    // 4. Sequential mixed outcome: a settled failure is followed by a
    // fresh attempt; the newer success clears the older error, so the tile
    // never reports a failure the latest commit fixed.
    {
        const firstGate = deferred();
        const secondGate = deferred();
        let calls = 0;
        const { feedbackEl } = makeContext(() => (++calls === 1 ? firstGate.promise : secondGate.promise));
        const first = SubwooferUI.beginSubwooferSave(pending);
        await tick();
        firstGate.reject(new Error('boom-first'));
        await assert.rejects(first, /boom-first/);
        await tick();
        assert.match(feedbackEl.textContent, /boom-first/);
        const second = SubwooferUI.beginSubwooferSave(pending);
        secondGate.resolve({ revision: 10 });
        await second;
        await tick();
        assert.equal(feedbackEl.textContent, '');
    }
    console.log('subwoofer feedback tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
