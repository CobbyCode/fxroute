#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Subwoofer tile status lifecycle is error-only by design: no Applying… or
// Saved hints are ever written; a failure reports its message persistently
// and the next successful commit clears it again.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function extract(pattern, label) {
    const match = source.match(pattern);
    assert.ok(match, label);
    return match[0];
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function makeContext(applyImpl) {
    const feedbackEl = { textContent: '', className: '' };
    const linkEl = { checked: true };
    const activeEditing = new Set([linkEl]);
    const context = {
        state: { outputSystem: { catalog: { active_mode: 'stereo-sub' } } },
        elements: { effectsSubwooferFeedback: feedbackEl, effectsSubwooferLink: linkEl },
        _activeEditing: activeEditing,
        window: { clearTimeout, setTimeout },
        applyOutputSystemMutation: applyImpl,
        console,
    };
    vm.createContext(context);
    vm.runInContext(extract(/function setSubwooferFeedback\([^]*?\n\}/, 'setSubwooferFeedback'), context);
    vm.runInContext(extract(/let _subwooferSavePromise = null;/, 'save promise'), context);
    vm.runInContext(extract(/let _subwooferLastRequestedSignature = '';?/, 'last signature'), context);
    vm.runInContext(extract(/function releaseSubwooferLinkGuard\(\)[^]*?\n\}/, 'link guard'), context);
    vm.runInContext(extract(/function beginSubwooferSave\(pending\)[^]*?\n\}/, 'beginSubwooferSave'), context);
    return { context, feedbackEl, activeEditing, linkEl };
}

function startSave(context, pending) {
    context.pending = pending;
    return vm.runInContext('beginSubwooferSave(pending)', context);
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const pending = { mode: 'stereo-sub', settings: { mode: 'stereo-sub' }, signature: '{"s":1}' };

(async () => {
    // 1. Success stays silent: no Applying… while in flight, no Saved hint
    // on commit; the guard still releases and the promise clears.
    {
        const gate = deferred();
        const { context, feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = startSave(context, pending);
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
        assert.equal(vm.runInContext('_subwooferSavePromise', context), null);
    }
    // 2. Rejection: error message + error class, promise cleared.
    {
        const gate = deferred();
        const { context, feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = startSave(context, pending);
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
        assert.equal(vm.runInContext('_subwooferSavePromise', context), null);
    }
    // 3. Superseded (null result): error state, rejection surfaces.
    {
        const { context, feedbackEl } = makeContext(async () => null);
        const run = startSave(context, pending);
        await assert.rejects(run, /superseded/);
        await tick();
        assert.match(feedbackEl.textContent, /superseded/);
        assert.match(feedbackEl.className, /error/);
    }
    // 4. Sequential mixed outcome: a settled failure is followed by a
    // fresh attempt (the chain resets once the promise clears); the newer
    // success clears the older error, so the tile never reports a failure
    // the latest commit fixed.
    {
        const firstGate = deferred();
        const secondGate = deferred();
        let calls = 0;
        const { context, feedbackEl } = makeContext(() => (++calls === 1 ? firstGate.promise : secondGate.promise));
        const first = startSave(context, pending);
        await tick();
        firstGate.reject(new Error('boom-first'));
        await assert.rejects(first, /boom-first/);
        await tick();
        assert.match(feedbackEl.textContent, /boom-first/);
        const second = startSave(context, pending);
        secondGate.resolve({ revision: 10 });
        await second;
        await tick();
        assert.equal(feedbackEl.textContent, '');
    }
    console.log('subwoofer feedback tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
