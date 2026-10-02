#!/usr/bin/env node
// Footer Auto toggle of the source switcher: which entries can be armed,
// how armed/engaged/active state renders, and the human return label.
// Also the App shortcut back to app playback.
// Tests the pure model extracted from static/app.js plus the markup and
// render wiring.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const uiSource = fs.readFileSync(path.join(root, 'static', 'playback_ui.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const styleSource = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');

function extractFunction(name, from = appSource) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(from);
    assert.ok(match, `missing ${name}`);
    const brace = from.indexOf('{', from.indexOf(')', match.index));
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < from.length; index += 1) {
        const character = from[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (character === '/' && from[index + 1] === '/') {
            index = from.indexOf('\n', index);
            continue;
        }
        if (`'"\``.includes(character)) quote = character;
        else if (character === '{') depth += 1;
        else if (character === '}' && --depth === 0) return from.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const formatLabels = /const SOURCE_AUTO_FORMAT_LABELS = \{[^}]*\};/s.exec(appSource);
assert.ok(formatLabels, 'missing SOURCE_AUTO_FORMAT_LABELS');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext([
    formatLabels[0],
    extractFunction('shortSourcePairLabel'),
    extractFunction('buildSourceSwitcherEntries'),
    extractFunction('findSourceSwitcherIndex'),
    extractFunction('sourceAutoReturnLabel'),
    extractFunction('sourceAutoModel'),
].join('\n'), sandbox);
const { buildSourceSwitcherEntries, sourceAutoModel } = sandbox;

const X = 'alsa_input.usb-Focusrite-00.pro-input-0|pair:AUX0,AUX1';
const Y = 'alsa_input.usb-Focusrite-00.pro-input-0|pair:AUX2,AUX3';
const inputs = [
    { key: X, pair_label: 'Input 1–2', device_label: 'Scarlett 16i16' },
    { key: Y, pair_label: 'Input 3–4', device_label: 'Scarlett 16i16' },
];
function overview(autoSelect, extra = {}) {
    return {
        mode: 'external-input',
        selected_input: { key: X },
        inputs,
        bluetooth: { selectable: true, connected_device: 'Phone' },
        stdin: { selectable: true, state: 'waiting' },
        auto_select: autoSelect,
        ...extra,
    };
}

// Armed entries are marked in the native dropdown; others stay unchanged.
const armedOverview = overview({ armed: [Y, 'stdin-input'], engaged: null, paused: false, inputs: {} });
const entries = buildSourceSwitcherEntries(armedOverview);
assert.equal(entries.find((entry) => entry.key === X).optionLabel, 'Input 1/2 — Scarlett 16i16');
assert.equal(entries.find((entry) => entry.key === Y).optionLabel, 'Input 3/4 — Scarlett 16i16 · Auto');
assert.equal(entries.find((entry) => entry.kind === 'stdin').optionLabel, 'STDIN — waiting · Auto');
assert.equal(entries.find((entry) => entry.kind === 'bluetooth').optionLabel, 'Bluetooth — Phone');
assert.ok(!buildSourceSwitcherEntries(overview(null)).some((entry) => entry.optionLabel.includes('Auto')),
    'no auto state: labels unchanged');

// Bluetooth has no signal probe; a missing input cannot be armed.
const bluetoothEntry = entries.find((entry) => entry.kind === 'bluetooth');
assert.equal(sourceAutoModel(armedOverview, bluetoothEntry).supported, false);
assert.equal(sourceAutoModel(armedOverview, { kind: 'external', key: X, unavailable: true }).supported, false);
assert.equal(sourceAutoModel(armedOverview, null).supported, false);

// Off, armed idle, armed with signal.
const xEntry = entries.find((entry) => entry.key === X);
const yEntry = entries.find((entry) => entry.key === Y);
let model = sourceAutoModel(armedOverview, xEntry);
assert.deepEqual([model.supported, model.armed, model.engaged, model.active], [true, false, false, false]);
assert.equal(model.tooltip, 'Auto-select this input on signal');
model = sourceAutoModel(armedOverview, yEntry);
assert.equal(model.armed, true);
assert.match(model.tooltip, /switches here when a signal is detected/);
const signalOverview = overview({ armed: [Y], engaged: null, paused: false,
    inputs: { [Y]: { active: true, format: 'ac3', lock: true } } });
model = sourceAutoModel(signalOverview, yEntry);
assert.equal(model.active, true);
assert.equal(model.tooltip, 'Auto · signal detected (Dolby Digital)');

const unlockedOverview = overview({ armed: [Y], engaged: null, paused: false,
    inputs: { [Y]: { active: false, format: null, lock: false } } });
assert.match(sourceAutoModel(unlockedOverview, yEntry).tooltip, /no digital lock/);

// Engaged: the tooltip names the return target like the switcher does.
const engagedOverview = (returnMode, returnKey) => overview({
    armed: [X], paused: false,
    engaged: { key: X, return_mode: returnMode, return_input_key: returnKey },
    inputs: { [X]: { active: true, format: 'pcm', lock: null } },
});
model = sourceAutoModel(engagedOverview('app-playback', null), xEntry);
assert.equal(model.engaged, true);
assert.equal(model.tooltip, 'Selected automatically · returns to App playback when the signal stops');
assert.match(sourceAutoModel(engagedOverview('external-input', Y), xEntry).tooltip, /returns to Input 3\/4 when/);
assert.match(sourceAutoModel(engagedOverview('bluetooth-input', null), xEntry).tooltip, /returns to Bluetooth when/);
assert.match(sourceAutoModel(engagedOverview('external-input', 'gone'), xEntry).tooltip, /returns to the previous input/);
assert.ok(!sourceAutoModel(engagedOverview('external-input', Y), xEntry).tooltip.includes('alsa_'),
    'no node names in the tooltip');

// Measurement pause is explained instead of a stale state.
const pausedOverview = overview({ armed: [X], engaged: null, paused: true, inputs: {} });
assert.match(sourceAutoModel(pausedOverview, xEntry).tooltip, /paused while a measurement/);

// Markup, wiring and styles.
assert.ok(/<button id="source-auto" class="source-auto-btn"[^>]*aria-pressed="false"/.test(indexSource),
    'footer switcher carries the Auto toggle');
assert.ok(uiSource.includes("sourceAuto.addEventListener('click', () => deps.toggleSourceAuto())"),
    'Auto toggle is wired in the playback UI');
assert.ok(appSource.includes("sourceAuto: document.getElementById('source-auto')"));
assert.ok(appSource.includes("fetch('/api/audio/source-auto'"), 'toggle posts to the auto-select endpoint');
assert.ok(/<button id="source-app" class="source-app-btn"[^>]*>App<\/button>\s*<button id="source-prev"/.test(indexSource),
    'footer switcher leads with the App shortcut');
assert.ok(uiSource.includes("sourceApp.addEventListener('click', () => deps.selectAppPlaybackSource())"),
    'App shortcut is wired in the playback UI');
assert.ok(appSource.includes("sourceApp: document.getElementById('source-app')"));
assert.ok(/function selectAppPlaybackSource\(\) \{[^}]*saveAudioSourceSelection\('app-playback'\)/s.test(appSource),
    'App shortcut selects app playback');
assert.ok(/auto_select: data\.auto_select \|\| null/.test(appSource), 'source state keeps the auto-select snapshot');
for (const selector of ['.source-app-btn,\n.source-auto-btn {', '.source-auto-btn.is-armed', '.source-auto-btn.has-signal::after',
    '.source-auto-btn.is-engaged::after']) {
    assert.ok(styleSource.includes(selector), `style.css missing ${selector}`);
}

// Render: armed/engaged/signal classes, aria-pressed and the tooltip.
function makeClassList() {
    return { toggled: {}, toggle(name, force) { this.toggled[name] = force; }, add() {}, remove() {} };
}
function makeButton(label) {
    const attrs = { 'aria-label': label };
    return {
        disabled: false,
        classList: makeClassList(),
        getAttribute(name) { return name in attrs ? attrs[name] : null; },
        setAttribute(name, value) { attrs[name] = String(value); },
    };
}
const renderSandbox = {
    state: { settings: { sourceMode: { ...engagedOverview('app-playback', null), pending: false } } },
    window: { __footerSource: 'local', FXRouteMeasurementJob: { hasActiveMeasurementJob: () => false } },
    document: { activeElement: null },
    escapeHtml: (text) => String(text ?? ''),
    PlaybackUI: { setFooterProgressState: () => {} },
    elements: {
        playbackBar: { classList: makeClassList() },
        sourceSwitcher: { classList: makeClassList() },
        transportControls: { classList: makeClassList() },
        queueStatus: { classList: makeClassList() },
        sourceSelect: { innerHTML: '', value: '', disabled: false, title: '' },
        sourcePrev: makeButton('Previous source'),
        sourceNext: makeButton('Next source'),
        sourceAuto: makeButton('Auto-select this input on signal'),
        sourceApp: makeButton('Switch to app playback'),
    },
};
vm.createContext(renderSandbox);
vm.runInContext([
    'let _sourceSelectSignature = null;',
    formatLabels[0],
    extractFunction('nonAppSourceModeActive'),
    extractFunction('shortSourcePairLabel'),
    extractFunction('buildSourceSwitcherEntries'),
    extractFunction('findSourceSwitcherIndex'),
    extractFunction('sourceSwitcherGuardReason'),
    extractFunction('sourceAutoReturnLabel'),
    extractFunction('sourceAutoModel'),
    extractFunction('renderSourceModeFooter'),
].join('\n'), renderSandbox);
vm.runInContext('renderSourceModeFooter()', renderSandbox);
const autoButton = renderSandbox.elements.sourceAuto;
assert.equal(autoButton.disabled, false);
assert.equal(autoButton.getAttribute('aria-pressed'), 'true');
assert.equal(autoButton.classList.toggled['is-armed'], true);
assert.equal(autoButton.classList.toggled['is-engaged'], true);
assert.equal(autoButton.classList.toggled['has-signal'], true);
assert.match(autoButton.getAttribute('data-tooltip'), /returns to App playback/);
const appButton = renderSandbox.elements.sourceApp;
assert.equal(appButton.disabled, false);
assert.equal(appButton.getAttribute('data-tooltip'), 'Back to app playback');

renderSandbox.state.settings.sourceMode = { ...armedOverview, mode: 'bluetooth-input', pending: false };
vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(autoButton.disabled, true, 'Bluetooth cannot be armed');
assert.equal(autoButton.getAttribute('aria-pressed'), 'false');

renderSandbox.state.settings.sourceMode = { ...armedOverview, pending: true };
vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(autoButton.disabled, true, 'parked while a switch is pending');
assert.equal(autoButton.getAttribute('data-tooltip'), 'Switching sources…');
assert.equal(appButton.disabled, true, 'App shortcut parked while a switch is pending');
assert.equal(appButton.getAttribute('data-tooltip'), 'Switching sources…');

console.log('test_footer_source_auto: ok');
