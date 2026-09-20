#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Subwoofer tile status lifecycle: Applying… -> Saved on commit,
// Applying… -> error state on failure. Guards the regression where the
// tile stayed on Applying… forever because no path ever set terminal
// feedback after beginSubwooferSave resolved.
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
    vm.runInContext(extract(/let _subwooferFeedbackTimer = null;/, 'feedback timer'), context);
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
    // 1. Success: Applying… while the commit is in flight, then Saved.
    {
        const gate = deferred();
        const { context, feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = startSave(context, pending);
        await tick();
        assert.equal(feedbackEl.textContent, 'Applying…');
        // The in-flight link toggle stays guarded until the save landed.
        assert.equal(activeEditing.has(linkEl), true);
        gate.resolve({ revision: 9 });
        assert.deepEqual(await run, { revision: 9 });
        await tick();
        assert.equal(feedbackEl.textContent, 'Saved');
        assert.match(feedbackEl.className, /success/);
        assert.equal(activeEditing.has(linkEl), false);
        assert.equal(vm.runInContext('_subwooferSavePromise', context), null);
    }
    // 2. Rejection: error message + error class, promise cleared.
    {
        const gate = deferred();
        const { context, feedbackEl, activeEditing, linkEl } = makeContext(() => gate.promise);
        const run = startSave(context, pending);
        await tick();
        assert.equal(feedbackEl.textContent, 'Applying…');
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
    // 4. Sequential saves: older run can never overwrite the newer feedback.
    {
        const gate = deferred();
        const { context, feedbackEl } = makeContext(() => gate.promise);
        const first = startSave(context, pending);
        const second = startSave(context, pending);
        await tick();
        assert.equal(feedbackEl.textContent, 'Applying…');
        gate.resolve({ revision: 10 });
        await first;
        await second;
        await tick();
        assert.equal(feedbackEl.textContent, 'Saved');
        assert.match(feedbackEl.className, /success/);
    }
    console.log('subwoofer feedback tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
