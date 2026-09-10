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

const KNOWN_ACTIONS = ['play-track', 'play-radio', 'search-radio-groove', 'refresh-library', 'search-library-jazz', 'play-qobuz', 'preset-plus6', 'dsp-demo', 'measurement-demo', 'cycle-providers', 'open-settings'];

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
        if (step.targetMobile) {
            assert.ok(selectorExistsIn(step.targetMobile, indexHtml),
                `step ${step.id}: mobile target ${step.targetMobile} must exist in static/index.html`);
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
    // The measurement step must come before the providers step so the
    // overlay is closed before the tour navigates back to the tabs.
    const measurementIndex = tour.steps.findIndex((s) => s.id === 'measurement');
    const providerIndex = tour.steps.findIndex((s) => s.id === 'qobuz');
    assert.ok(measurementIndex >= 0 && providerIndex > measurementIndex,
        'measurement step must precede the providers step (overlay is closed there)');

    // The tour must end on a full-card summary (no element highlight).
    const last = tour.steps[tour.steps.length - 1];
    assert.equal(last.target, null, 'the final step must be a summary card without a target');
    assert.ok(/fxroute/i.test(last.text), 'the summary must mention what fxroute does');

    // The tour is a walkthrough, not an app: no dynamic DOM building.
    assert.ok(!tourSource.includes('innerHTML'), 'tour must not build DOM via innerHTML');
    assert.ok(tourSource.includes('notour'), 'tour must honor the ?notour opt-out');

    // "Lightly active" tour: radio and library steps demonstrate what
    // their text describes (Groove Salad search, Jazz filter).
    const radioStep = tour.steps.find((s) => s.id === 'radio');
    const libraryStep = tour.steps.find((s) => s.id === 'library');
    assert.equal(radioStep.action, 'search-radio-groove', 'radio step must demo the Groove Salad search it describes');
    assert.equal(libraryStep.action, 'search-library-jazz', 'library step must demo the Jazz search/view cycle it describes');
    assert.ok(tourSource.includes("action === 'search-radio-groove'"), 'search-radio-groove action must be implemented');
    assert.ok(tourSource.includes("action === 'search-library-jazz'"), 'search-library-jazz action must be implemented');

    // DSP: A/B compare + output extras (limiter/headroom/loudness).
    const dspStep = tour.steps.find((s) => s.id === 'dsp');
    assert.equal(dspStep.action, 'dsp-demo', 'DSP step must demo A/B compare + output extras');
    assert.ok(tourSource.includes("action === 'dsp-demo'"), 'dsp-demo action must be implemented');

    // Measurement: opens the real Measurement assistant overlay and covers
    // sweeps, advanced measurements, convolver creation and SPL calibration.
    const measurementStep = tour.steps.find((s) => s.id === 'measurement');
    assert.ok(measurementStep, 'tour must have a measurement step');
    assert.equal(measurementStep.action, 'measurement-demo', 'measurement step must demo the measurement workflow');
    assert.equal(measurementStep.targetMobile, '#measurement-sweep-toggle',
        'measurement step must highlight the sweep toggle on narrow screens');
    assert.equal(dspStep.targetMobile, '#effects-compare-toggle',
        'DSP step must highlight the A/B toggle on narrow screens');
    assert.ok(tourSource.includes("action === 'measurement-demo'"), 'measurement-demo action must be implemented');
    assert.ok(tourSource.includes('effects-measure-open'), 'measurement-demo must open the panel via the Measure button');
    assert.ok(tourSource.includes('measurement-sweep-toggle'), 'measurement-demo must pulse the Start Sweep entry');
    assert.ok(tourSource.includes('measurement-spl-calibration-open'), 'measurement-demo must pulse the SPL Calibration entry');
    assert.ok(tourSource.includes('closeMeasurementPanel'), 'tour must close the measurement overlay between steps');
    const measurementCopy = `${measurementStep.title} ${measurementStep.text}`;
    assert.ok(/sweep/i.test(measurementCopy), 'measurement step must mention sweeps');
    assert.ok(/convolver/i.test(measurementCopy), 'measurement step must mention convolver creation');
    assert.ok(/spl/i.test(measurementCopy), 'measurement step must mention SPL calibration');
    assert.ok(/advanced/i.test(measurementCopy), 'measurement step must mention advanced measurements');

    // Providers step: cycle through Spotify/Qobuz/TIDAL so the multi-provider
    // claim is visible in the tabs/footer instead of only stated.
    const providerStep = tour.steps.find((s) => s.id === 'qobuz');
    assert.equal(providerStep.action, 'cycle-providers', 'providers step must cycle through the provider tabs');
    assert.ok(tourSource.includes("action === 'cycle-providers'"), 'cycle-providers action must be implemented');
    // Keep legacy qobuz action available for direct playback switching.
    assert.ok(tourSource.includes("action === 'play-qobuz'") || tourSource.includes('play-qobuz'), 'demo must still support qobuz playback');

    // The whole demo UI is English — the tour copy must be too (the card
    // labels as well as every step text).
    const tourCopy = tour.steps.map((s) => `${s.title} ${s.text}`).join(' ') + ' ' + tourSource;
    for (const german of ['Wiedergabe', 'Schritt', 'Zurück', 'Überspringen', 'Fertig', 'Weiter', 'Musikbibliothek', 'Einstellungen']) {
        assert.ok(!tourCopy.includes(german), `tour must not contain German copy: ${german}`);
    }

    // Card buttons must stay clickable: the card is a child of the backdrop
    // (pointer-events: none) and must opt back in explicitly.
    const cssSource = fs.readFileSync(path.join(root, 'demo', 'demo.css'), 'utf8');
    const cardRule = cssSource.split('.demo-tour-card')[1] || '';
    assert.ok(cardRule.includes('pointer-events: auto'),
        'demo-tour-card must re-enable pointer events (buttons unclickable otherwise)');
    // Tour must offer an explicit Close affordance (X button), not only Next/Skip.
    assert.ok(tourSource.includes('demo-tour-close'), 'tour card must have a Close/X button');
    assert.ok(cssSource.includes('demo-tour-close'), 'tour Close button must be styled');

    // Provider highlight pulse used by the settings step animation.
    assert.ok(cssSource.includes('demo-tour-pulse'), 'settings provider pulse animation must be styled');

    // English copy checks: new texts must keep claims accurate.
    assert.ok(!tourSource.includes('100+'), 'tour must not claim 100+ stations (demo has ~65)');
    assert.ok(!indexHtml.includes('id="station-search"') || tourSource.includes('station-search'),
        'radio step action must target the real station search input');

    // Summary must mention the key DSP strengths (convolver + measurement).
    assert.ok(/convolver/i.test(last.text) || /PEQ/i.test(last.text),
        'summary must mention convolver/PEQ as DSP strength');
    assert.ok(/measurement/i.test(last.text), 'summary must mention measurement');

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