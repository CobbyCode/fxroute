#!/usr/bin/env node
// Footer source switcher for bluetooth-input / external-input modes:
// compact ‹ label › stepping over the real overview sources, no technical
// node names, measurement-guarded. Tests the pure switcher model extracted
// from static/app.js plus the footer markup contract in static/index.html.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing ${name}`);
    const brace = appSource.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    let lineComment = false;
    let blockComment = false;
    for (let index = brace; index < appSource.length; index += 1) {
        const character = appSource[index];
        const nextCharacter = appSource[index + 1];
        if (lineComment) {
            if (character === '\n') lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === '*' && nextCharacter === '/') {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (character === '/' && nextCharacter === '/') {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === '/' && nextCharacter === '*') {
            blockComment = true;
            index += 1;
            continue;
        }
        if (`'"\``.includes(character)) quote = character;
        else if (character === '{') depth += 1;
        else if (character === '}' && --depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = { state: { settings: { sourceMode: { pending: false } } } };
vm.createContext(sandbox);
vm.runInContext([
    extractFunction('shortSourcePairLabel'),
    extractFunction('buildSourceSwitcherEntries'),
    extractFunction('findSourceSwitcherIndex'),
    extractFunction('cycleSourceSwitcherIndex'),
    extractFunction('nonAppSourceModeActive'),
    extractFunction('isFooterSignalActive'),
    extractFunction('sourceSwitcherGuardReason'),
    extractFunction('activateSourceSwitcherEntry'),
    extractFunction('stepSourceSwitcher'),
].join('\n'), sandbox);

const {
    shortSourcePairLabel,
    buildSourceSwitcherEntries,
    findSourceSwitcherIndex,
    cycleSourceSwitcherIndex,
    isFooterSignalActive,
} = sandbox;

// Pair labels use an en dash on the backend; the footer shows a slash.
assert.equal(shortSourcePairLabel('Input 1–2'), 'Input 1/2');
assert.equal(shortSourcePairLabel('Input 11–12'), 'Input 11/12');
assert.equal(shortSourcePairLabel('Input 3-4'), 'Input 3/4');
assert.equal(shortSourcePairLabel('Bluetooth'), 'Bluetooth');

const scarlettInputs = Array.from({ length: 9 }, (_, pair) => ({
    key: `alsa_input.scarlett::pair${pair}`,
    pair_label: `Input ${2 * pair + 1}–${2 * pair + 2}`,
    label: `Scarlett 16i16 4th Gen Multichannel — Input ${2 * pair + 1}–${2 * pair + 2}`,
    device_label: 'Scarlett 16i16 4th Gen Multichannel',
}));
const bluetoothOverview = {
    mode: 'bluetooth-input',
    inputs: scarlettInputs,
    selected_input: null,
    current_input: null,
    bluetooth: {
        selectable: true,
        active_session: { device_name: 'ZENBOOK', active_codec: 'aac' },
    },
};

// Bluetooth first, then every real pair in overview order; no node names.
const entries = buildSourceSwitcherEntries(bluetoothOverview);
assert.equal(entries.length, 10, 'bluetooth + 9 scarlett pairs');
assert.equal(
    JSON.stringify(entries[0]),
    JSON.stringify({ kind: 'bluetooth', key: 'bluetooth-input', label: 'Bluetooth', sub: 'ZENBOOK · aac', optionLabel: 'Bluetooth — ZENBOOK · aac' }),
);
assert.equal(entries[1].label, 'Input 1/2');
assert.equal(entries[1].kind, 'external');
assert.equal(entries[9].label, 'Input 17/18');
for (const entry of entries) {
    assert.ok(!entry.label.includes('alsa_'), `no technical name in label: ${entry.label}`);
    assert.ok(!entry.optionLabel.includes('alsa_'), `no technical name in option: ${entry.optionLabel}`);
}
assert.ok(entries[1].optionLabel.includes('Scarlett'), 'dropdown keeps the human device context');

// Current index follows the mode: bluetooth mode pins Bluetooth.
assert.equal(findSourceSwitcherIndex(entries, bluetoothOverview), 0);
const externalOverview = {
    ...bluetoothOverview,
    mode: 'external-input',
    selected_input: scarlettInputs[1],
    current_input: scarlettInputs[1],
};
assert.equal(findSourceSwitcherIndex(entries, externalOverview), 2, 'Input 3/4 is index 2 behind Bluetooth');

// Cycling wraps across Bluetooth → Input 1/2 → … → Input 17/18 → Bluetooth.
assert.equal(cycleSourceSwitcherIndex(entries, 0, 1), 1);
assert.equal(cycleSourceSwitcherIndex(entries, 9, 1), 0);
assert.equal(cycleSourceSwitcherIndex(entries, 0, -1), 9);
assert.equal(cycleSourceSwitcherIndex(entries, 2, -1), 1);
assert.equal(cycleSourceSwitcherIndex([], 0, 1), -1);

// Stepping with an unresolvable current entry (no selection cached yet)
// must still land on a real source instead of doing nothing.
const stepCalls = [];
sandbox.saveAudioSourceSelection = (mode, key) => { stepCalls.push([mode, key]); return Promise.resolve(); };
sandbox.state.settings = {
    sourceMode: {
        mode: 'external-input',
        pending: false,
        inputs: scarlettInputs,
        selected_input: null,
        current_input: null,
        bluetooth: { selectable: true, active_session: { device_name: 'ZENBOOK', active_codec: 'aac' } },
    },
};
vm.runInContext('stepSourceSwitcher(1)', sandbox);
assert.deepEqual(stepCalls, [['bluetooth-input', undefined]], 'forward step from unresolvable current lands on the first entry');
vm.runInContext('stepSourceSwitcher(-1)', sandbox);
assert.deepEqual(
    stepCalls[1],
    ['external-input', scarlettInputs[8].key],
    'backward step from unresolvable current wraps to the last entry',
);

// Without Bluetooth availability the switcher is inputs only.
const noBt = buildSourceSwitcherEntries({ ...bluetoothOverview, bluetooth: { selectable: false } });
assert.equal(noBt.length, 9);
assert.equal(noBt[0].label, 'Input 1/2');
assert.equal(findSourceSwitcherIndex(noBt, { ...externalOverview, bluetooth: { selectable: false } }), 1);

// Signal-active gating for the footer meter: line sources keep the DSP path
// live while app playback is paused, so the meter must not stay dark there.
sandbox.window = { __footerSource: 'local' };
sandbox.isStreamingFooterSource = () => false;
sandbox.streamingFooterData = () => null;
sandbox.state.playback = { playing: false, paused: true };
sandbox.state.settings = { sourceMode: { mode: 'external-input', pending: false } };
assert.equal(isFooterSignalActive(), true, 'external input counts as live signal while app playback is paused');
sandbox.state.settings.sourceMode.mode = 'bluetooth-input';
assert.equal(isFooterSignalActive(), true, 'bluetooth input counts as live signal while app playback is paused');
sandbox.state.settings.sourceMode.mode = 'app-playback';
assert.equal(isFooterSignalActive(), false, 'paused app playback keeps the meter dark');
sandbox.state.playback = { playing: true, paused: false };
assert.equal(isFooterSignalActive(), true, 'playing app playback keeps the meter lit');

// Footer markup contract: switcher lives in the playback bar center.
for (const snippet of [
    'id="source-switcher"',
    'id="source-prev"',
    'id="source-select"',
    'id="source-next"',
    'id="playback-bar"',
]) {
    assert.ok(indexSource.includes(snippet), `static/index.html missing ${snippet}`);
}
// In source modes the track/metadata block is hidden by CSS so the switcher
// absorbs its column; meter and volume keep their grid areas everywhere.
const styleSource = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
assert.ok(
    /\.playback-bar\.source-mode\s*\{[^}]*grid-template-areas:\s*"transport meter volume"/s.test(styleSource),
    'source-mode footer must remap the grid without the track column',
);
assert.ok(
    /\.playback-bar\.source-mode \.track-info\s*\{\s*display:\s*none/s.test(styleSource),
    'source-mode footer must hide the track/metadata block',
);
// Option writes must be stable across polls: rebuilding the native select
// on every status update closes its popup instantly (no usable dropdown).
function makeClassList() {
    return { toggled: {}, added: [], removed: [], toggle(name, force) { this.toggled[name] = force; }, add(name) { this.added.push(name); }, remove(name) { this.removed.push(name); } };
}
const renderSandbox = {
    state: {
        settings: { sourceMode: { ...bluetoothOverview, pending: false } },
        measurement: {},
    },
    window: { __footerSource: 'local' },
    document: { activeElement: null },
    elements: {
        playbackBar: { classList: makeClassList() },
        sourceSwitcher: { classList: makeClassList() },
        transportControls: { classList: makeClassList() },
        queueStatus: { classList: makeClassList() },
        seekRow: { classList: makeClassList() },
        sourceSelect: {
            writes: 0,
            html: '',
            value: '',
            disabled: false,
            title: '',
            set innerHTML(next) { this.writes += 1; this.html = next; },
            get innerHTML() { return this.html; },
        },
        sourcePrev: { disabled: false, title: '', getAttribute() { return 'Previous source'; } },
        sourceNext: { disabled: false, title: '', getAttribute() { return 'Next source'; } },
    },
};
vm.createContext(renderSandbox);
// escapeHtml is not extractor-safe (regex literal with a quote); the render
// test only needs its identity behavior for plain labels, stubbed here.
renderSandbox.escapeHtml = (text) => String(text ?? '');
const selectSigDecl = /let _sourceSelectSignature = null;/.exec(appSource);
assert.ok(selectSigDecl, 'missing _sourceSelectSignature module state');
vm.runInContext([
    selectSigDecl[0],
    extractFunction('isStreamingFooterSource'),
    extractFunction('hasActiveMeasurementJob'),
    extractFunction('nonAppSourceModeActive'),
    extractFunction('shortSourcePairLabel'),
    extractFunction('buildSourceSwitcherEntries'),
    extractFunction('findSourceSwitcherIndex'),
    extractFunction('sourceSwitcherGuardReason'),
    extractFunction('setFooterProgressState'),
    extractFunction('renderSourceModeFooter'),
].join('\n'), renderSandbox);

vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(renderSandbox.elements.sourceSelect.writes, 1, 'first render builds the options');
assert.ok(renderSandbox.elements.sourceSelect.html.includes('Input 1/2'), 'options carry the pair labels');
assert.equal(renderSandbox.elements.sourceSelect.value, 'bluetooth-input', 'bluetooth preselected in bluetooth mode');
assert.equal(renderSandbox.elements.playbackBar.classList.toggled['source-mode'], true, 'bar carries source-mode class');
assert.equal(renderSandbox.elements.transportControls.classList.toggled.hidden, true, 'transport parked in source mode');

// Stale streaming ownership must not suppress the switcher: a retained
// Spotify Paused context (backend pauses Spotify on source switch, which
// keeps footer context) previously flashed the app footer back.
renderSandbox.window.__footerSource = 'spotify';
vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(renderSandbox.elements.playbackBar.classList.toggled['source-mode'], true, 'source mode survives stale spotify ownership');
assert.equal(renderSandbox.elements.sourceSwitcher.classList.toggled.hidden, false, 'switcher stays visible with stale spotify ownership');
assert.equal(renderSandbox.elements.transportControls.classList.toggled.hidden, true, 'transport stays parked with stale spotify ownership');
renderSandbox.window.__footerSource = 'local';

// reconcileFooterSource must force local ownership in source modes so the
// streaming early-return in updatePlaybackUI never hijacks the footer.
const reconcileSource = extractFunction('reconcileFooterSource');
assert.ok(reconcileSource.includes('nonAppSourceModeActive()'), 'reconcile must consult the source mode');
assert.ok(reconcileSource.includes('source-mode-owns-footer'), 'reconcile must pin ownership in source modes');

// First paint after entering a source mode must render the switcher
// immediately: updatePlaybackUI must call renderSourceModeFooter() from the
// non-streaming body without a streaming-ownership gate — reconcile above
// pinned ownership to local before the gate would even be evaluated, so any
// streaming condition there only delays the first paint by one poll.
const updateFnSource = extractFunction('updatePlaybackUI');
const updateCallSite = updateFnSource.match(/if \(nonAppSourceModeActive\(\)\) \{\s*renderSourceModeFooter\(\);\s*\}/);
assert.ok(updateCallSite, 'updatePlaybackUI must render the source footer ungated by streaming ownership');
assert.ok(
    !/nonAppSourceModeActive\(\) && !isStreamingFooterSource/.test(updateFnSource),
    'updatePlaybackUI must not gate the source footer on streaming ownership',
);

vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(renderSandbox.elements.sourceSelect.writes, 1, 'identical poll must not rebuild the options');

vm.runInContext(
    `state.settings.sourceMode = { mode: 'external-input', inputs: ${JSON.stringify(scarlettInputs)},
        selected_input: ${JSON.stringify(scarlettInputs[1])}, current_input: ${JSON.stringify(scarlettInputs[1])},
        bluetooth: { selectable: true, active_session: null }, pending: false };
     renderSourceModeFooter()`,
    renderSandbox,
);
assert.equal(renderSandbox.elements.sourceSelect.writes, 2, 'source change rebuilds the options once');
assert.equal(renderSandbox.elements.sourceSelect.value, scarlettInputs[1].key, 'Input 3/4 selected after switch');

// An open popup (focused select) is never rebuilt, even with new data.
renderSandbox.document.activeElement = renderSandbox.elements.sourceSelect;
vm.runInContext(
    `state.settings.sourceMode = { mode: 'external-input', inputs: ${JSON.stringify(scarlettInputs)},
        selected_input: ${JSON.stringify(scarlettInputs[2])}, current_input: ${JSON.stringify(scarlettInputs[2])},
        bluetooth: { selectable: true, active_session: null }, pending: false };
     renderSourceModeFooter()`,
    renderSandbox,
);
assert.equal(renderSandbox.elements.sourceSelect.writes, 2, 'focused select keeps its popup');
renderSandbox.document.activeElement = null;
vm.runInContext('renderSourceModeFooter()', renderSandbox);
assert.equal(renderSandbox.elements.sourceSelect.writes, 3, 'pending change applies after the popup closes');
assert.equal(renderSandbox.elements.sourceSelect.value, scarlettInputs[2].key, 'Input 5/6 selected afterwards');

// Changed cached assets must be versioned so browsers fetch the new footer.
for (const pattern of [/\/static\/app\.js\?v=\d+\.\d+\.\d+/, /\/static\/style\.css\?v=\d+\.\d+\.\d+/]) {
    assert.ok(pattern.test(indexSource), `static/index.html missing versioned asset for ${pattern}`);
}

console.log('PASS  scripts/test_footer_source_switcher.js (bluetooth/external footer switcher model)');
