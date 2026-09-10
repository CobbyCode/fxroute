#!/usr/bin/env node
// Demo tour contract: the guided walkthrough (demo/tour.js) must stay in
// sync with the real frontend markup it highlights.
//
// * The tour ships its steps as plain data (window.FXROUTE_DEMO_TOUR) so
//   this test can validate them without a DOM.
// * Every target/tab selector must exist in the canonical static/index.html
//   (a renamed element silently breaks the step).
// * The tour is reachable from the demo banner ("Tour ansehen").
// * Actions reference real demo hooks (state API, preset load) and the
//   module never builds DOM from dynamic strings (no innerHTML).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const tourSource = fs.readFileSync(path.join(root, 'demo', 'tour.js'), 'utf8');
const bootSource = fs.readFileSync(path.join(root, 'demo', 'boot.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

const KNOWN_ACTIONS = ['play-track', 'preset-plus6', 'open-settings'];

function selectorExistsIn(selector, html) {
    const sel = String(selector || '').trim();
    if (!sel) return true;
    if (sel.startsWith('#')) {
        return html.includes(`id="${sel.slice(1)}"`);
    }
    if (sel.startsWith('.')) {
        const cls = sel.slice(1);
        return new RegExp(`class="[^"]*\\b${cls}\\b`).test(html);
    }
    return html.includes(`<${sel}`);
}

(async () => {
    // Steps data must be loadable without a DOM (it is defined before any
    // document access, so the vm context needs only `window`).
    const ctx = { window: {} };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(tourSource, ctx);
    const tour = ctx.window.FXROUTE_DEMO_TOUR;
    assert.ok(tour && Array.isArray(tour.steps), 'tour must expose FXROUTE_DEMO_TOUR.steps');
    assert.ok(tour.steps.length >= 6, 'tour must cover the main surfaces (6+ steps)');

    const ids = new Set();
    for (const step of tour.steps) {
        assert.ok(step && typeof step === 'object', 'every step must be an object');
        assert.ok(step.id && /^[a-z0-9-]+$/.test(step.id), `step id must be slug-like: ${step.id}`);
        assert.ok(!ids.has(step.id), `duplicate step id: ${step.id}`);
        ids.add(step.id);
        assert.ok(step.title && step.title.trim().length >= 3, `step ${step.id} needs a title`);
        assert.ok(step.text && step.text.trim().length >= 40, `step ${step.id} needs a real explanation`);
        if (step.target) {
            assert.ok(selectorExistsIn(step.target, indexHtml),
                `step ${step.id}: target ${step.target} must exist in static/index.html`);
        }
        if (step.tab) {
            assert.ok(selectorExistsIn(step.tab, indexHtml),
                `step ${step.id}: tab ${step.tab} must exist in static/index.html`);
        }
        if (step.action) {
            assert.ok(KNOWN_ACTIONS.includes(step.action),
                `step ${step.id}: unknown action ${step.action}`);
        }
    }
    // The tour must end on a full-card summary (no element highlight).
    const last = tour.steps[tour.steps.length - 1];
    assert.equal(last.target, null, 'the final step must be a summary card without a target');
    assert.ok(/fxroute/i.test(last.text), 'the summary must mention what fxroute does');

    // The tour is a walkthrough, not an app: no dynamic DOM building.
    assert.ok(!tourSource.includes('innerHTML'), 'tour must not build DOM via innerHTML');
    assert.ok(tourSource.includes('notour'), 'tour must honor the ?notour opt-out');

    // Actions must reach the real demo hooks: state API for playback and
    // the interceptor for the DSP preset switch.
    const actionLines = [tourSource, bootSource].join('\n');
    assert.ok(actionLines.includes('FXROUTE_DEMO_STATE'), 'tour actions must use the demo state API');
    assert.ok(actionLines.includes('/api/dsp/presets/load'), 'tour must switch presets via the demo API');

    // Reachable from the demo banner: boot.js creates the tour trigger.
    assert.ok(bootSource.includes('demo-tour-start'),
        'boot.js must add the "Tour ansehen" link (id=demo-tour-start) to the demo banner');

    console.log(`ok demo tour (${tour.steps.length} steps)`);
})().catch((err) => {
    console.error(err);
    process.exit(1);
});