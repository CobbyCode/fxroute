#!/usr/bin/env node
// STDIN source entry: selectable mode with waiting/streaming status.
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

function extractFunction(name, from = appSource) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(from);
    assert.ok(match, `missing ${name}`);
    const brace = from.indexOf('{', match.index);
    let depth = 0;
    for (let index = brace; index < from.length; index += 1) {
        if (from[index] === '{') depth += 1;
        else if (from[index] === '}' && --depth === 0) return from.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = {
    state: { settings: { sourceMode: { pending: false } } },
    window: { FXRouteMeasurementJob: { hasActiveMeasurementJob: () => false } },
};
vm.createContext(sandbox);
vm.runInContext([
    extractFunction('buildSourceSwitcherEntries'),
    extractFunction('findSourceSwitcherIndex'),
    extractFunction('nonAppSourceModeActive'),
    extractFunction('isFooterSignalActive'),
    extractFunction('activateSourceSwitcherEntry'),
].join('\n'), sandbox);
sandbox.PlaybackCore = { isStreamingFooterSource: () => false, streamingFooterData: () => null };

const overview = {
    mode: 'stdin-input', inputs: [], bluetooth: {},
    stdin: { available: true, selectable: true, state: 'waiting', routed: false },
};
const entries = sandbox.buildSourceSwitcherEntries(overview);
assert.equal(entries.filter((entry) => entry.kind === 'stdin').length, 1);
assert.equal(entries.find((entry) => entry.kind === 'stdin').label, 'STDIN');
assert.equal(sandbox.findSourceSwitcherIndex(entries, overview),
    entries.findIndex((entry) => entry.kind === 'stdin'));
sandbox.state.settings.sourceMode = overview;
assert.equal(sandbox.nonAppSourceModeActive(), true);

// Waiting alone is not an active signal; routed streaming is.
sandbox.state.playback = { playing: false, paused: false };
sandbox.state.settings.sourceMode = overview;
assert.equal(sandbox.isFooterSignalActive(), false);
sandbox.state.settings.sourceMode = { ...overview,
    stdin: { ...overview.stdin, state: 'streaming', routed: true } };
assert.equal(sandbox.isFooterSignalActive(), true);

// An unconfirmed Bluetooth loss keeps the mode; its switcher entry stays too.
const bluetoothBlip = { mode: 'bluetooth-input', inputs: [], stdin: {},
    bluetooth: { selectable: false, state: 'unavailable' } };
const blipEntries = sandbox.buildSourceSwitcherEntries(bluetoothBlip);
assert.equal(blipEntries.filter((entry) => entry.kind === 'bluetooth').length, 1);
assert.equal(sandbox.findSourceSwitcherIndex(blipEntries, bluetoothBlip), 0);
assert.equal(sandbox.buildSourceSwitcherEntries({ ...bluetoothBlip, mode: 'app-playback' })
    .filter((entry) => entry.kind === 'bluetooth').length, 0);

// Settings select offers STDIN and posts the stdin mode.
assert.ok(indexSource.includes('id="settings-stdin-status"'), 'missing STDIN status element');
assert.ok(indexSource.includes('id="settings-source-status"'), 'missing source fetch status element');
assert.ok(appSource.includes("saveAudioSourceSelection('stdin-input')"), 'missing STDIN activation');
assert.ok(appSource.includes('stdin: data.stdin'), 'missing STDIN state sync');

// Live `source` push: the settings poll only runs while the dialog is open,
// so the WS case is what keeps tabs, footer gating and the switcher fresh.
const sourceCaseAt = appSource.indexOf("case 'source':");
assert.ok(sourceCaseAt >= 0, 'missing source WS case');
const sourceCase = appSource.slice(sourceCaseAt, appSource.indexOf('break;', sourceCaseAt));
assert.ok(sourceCase.includes('applySourceOverview(data)'), 'source push must go through the revision check');
assert.ok(sourceCase.includes('renderSourceState()'), 'source push must re-render the source UI');
assert.ok(!sourceCase.includes('renderSettingsPanel()'), 'source push must not re-render the whole settings panel');
const renderSourceState = extractFunction('renderSourceState');
for (const call of ['renderSourceSection()', 'applySourceModeUiState()', 'PlaybackUI.renderPeakWarningBadge()']) {
    assert.ok(renderSourceState.includes(call), `renderSourceState must call ${call}`);
}
assert.ok(extractFunction('renderSettingsPanel').includes('renderSourceSection()'),
    'the settings panel renders the same source section');
const onOpenAt = appSource.indexOf('socket.onopen');
const onOpen = appSource.slice(onOpenAt, appSource.indexOf('socket.onclose', onOpenAt));
assert.ok(onOpen.includes('fetchAudioSourceOverview()'), 'a (re)connect must recover missed source pushes');

// Revision order, epochs, full replacement, fetch errors and save ownership,
// run on the real code.
const emptySourceSync = () => ({
    epoch: null, retiredEpochs: [], revision: -1, overview: null,
    saveMode: null, saveSerial: 0, fetchError: null,
});
const sourceSandbox = {
    state: { settings: {
        sourceMode: { mode: 'app-playback', pending: false },
        sourceSync: emptySourceSync(),
    } },
    renders: 0,
    toasts: [],
    requests: [],
    Number,
    JSON,
    Array,
    Promise,
    Error,
};
sourceSandbox.renderSourceState = () => { sourceSandbox.renders += 1; };
sourceSandbox.showToast = (message, kind) => { sourceSandbox.toasts.push([kind, message]); };
sourceSandbox.fetch = (url, options = {}) => new Promise((resolve) => {
    sourceSandbox.requests.push({ url, method: options.method || 'GET', resolve });
});
vm.createContext(sourceSandbox);
const retiredEpochLimit = /const SOURCE_RETIRED_EPOCH_LIMIT = \d+;/.exec(appSource);
assert.ok(retiredEpochLimit, 'missing SOURCE_RETIRED_EPOCH_LIMIT');
vm.runInContext([
    retiredEpochLimit[0],
    extractFunction('applySourceOverview'),
    extractFunction('composeSourceModeState'),
    // extractFunction slices from `function`; restore the async keyword.
    `async ${extractFunction('saveAudioSourceSelection')}`,
    `async ${extractFunction('fetchAudioSourceOverview')}`,
].join('\n'), sourceSandbox);
const flush = () => new Promise((resolve) => setImmediate(resolve));
const respond = (request, data, ok = true) => request.resolve({ ok, json: async () => data });
const sourceMode = () => sourceSandbox.state.settings.sourceMode;
const full = (mode, revision, extra = {}) => ({
    mode, revision, modes: [{ key: mode }], inputs: [{ key: 'line::in' }], bluetooth: {},
    stdin: { selectable: true, state: 'waiting' }, notes: [], ...extra,
});

(async () => {
    assert.equal(sourceSandbox.applySourceOverview(full('stdin-input', 10)), true);
    assert.equal(sourceMode().mode, 'stdin-input');
    assert.equal(sourceSandbox.applySourceOverview(full('app-playback', 9)), false,
        'an overview that lost the race is dropped');
    assert.equal(sourceMode().mode, 'stdin-input');
    sourceSandbox.applySourceOverview({ mode: 'stdin-input', revision: 10 });
    assert.equal(sourceMode().inputs.length, 0, 'the newest overview replaces the state outright');

    // A save owns mode and pending until its own response lands.
    const save = sourceSandbox.saveAudioSourceSelection('external-input', 'line::in');
    assert.equal(sourceMode().mode, 'external-input');
    assert.equal(sourceMode().pending, true);
    const post = sourceSandbox.requests.find((request) => request.method === 'POST');
    sourceSandbox.applySourceOverview(full('stdin-input', 11, { stdin: { state: 'streaming' } }));
    assert.equal(sourceMode().mode, 'external-input', 'a pre-commit push must not revert the save');
    assert.equal(sourceMode().stdin.state, 'streaming', 'the push still updates the rest');
    const poll = sourceSandbox.fetchAudioSourceOverview();
    respond(sourceSandbox.requests.at(-1), full('external-input', 13));
    await poll;
    assert.equal(sourceMode().pending, true, 'a poll never releases an in-flight save');
    respond(post, full('external-input', 12));
    await save;
    assert.equal(sourceMode().pending, false);
    assert.equal(sourceMode().mode, 'external-input');
    assert.equal(sourceSandbox.state.settings.sourceSync.revision, 13,
        'the older save response does not replace the newer poll');

    // A failed save releases pending and falls back to the newest server mode.
    const failing = sourceSandbox.saveAudioSourceSelection('bluetooth-input');
    assert.equal(sourceMode().mode, 'bluetooth-input');
    respond(sourceSandbox.requests.at(-1), { detail: 'Bluetooth input is not currently available' }, false);
    await failing;
    await flush();
    assert.equal(sourceMode().pending, false);
    assert.equal(sourceMode().mode, 'external-input');
    assert.deepEqual(sourceSandbox.toasts.at(-1), ['error', 'Bluetooth input is not currently available']);
    assert.equal(sourceSandbox.requests.at(-1).method, 'GET', 'a failed save re-reads the server state');

    // A failed fetch keeps the last good snapshot and only records the error.
    respond(sourceSandbox.requests.at(-1), {}, false);
    await flush();
    assert.equal(sourceMode().mode, 'external-input', 'a failed fetch must not flip the mode');
    assert.equal(sourceMode().inputs.length, 1, 'a failed fetch must not wipe the switcher');
    assert.equal(sourceMode().fetch_error, 'Failed to fetch source mode');
    assert.deepEqual([...sourceMode().notes], [], 'server notes stay separate from the fetch error');
    assert.equal(sourceSandbox.state.settings.sourceSync.overview.mode, 'external-input');
    sourceSandbox.applySourceOverview(full('external-input', 14));
    assert.equal(sourceMode().fetch_error, null, 'the next good payload clears the error');

    // A restarted backend counts from 1 again under a new epoch; a straggler
    // from the replaced epoch can never re-base the client backwards.
    assert.equal(sourceSandbox.applySourceOverview({ ...full('external-input', 480), epoch: 'E1' }), true);
    assert.equal(sourceSandbox.applySourceOverview({ ...full('stdin-input', 3), epoch: 'E2' }), true,
        'a new epoch must not be rejected for its lower revision');
    assert.equal(sourceMode().mode, 'stdin-input');
    assert.equal(sourceSandbox.applySourceOverview({ ...full('app-playback', 481), epoch: 'E1' }), false,
        'a late payload from the retired epoch is refused');
    assert.equal(sourceMode().mode, 'stdin-input');
    assert.equal(sourceSandbox.state.settings.sourceSync.epoch, 'E2');
    assert.equal(sourceSandbox.applySourceOverview({ ...full('stdin-input', 4), epoch: 'E2' }), true,
        'the current epoch keeps its ordering after the straggler');
    assert.equal(sourceSandbox.applySourceOverview({ ...full('app-playback', 2), epoch: 'E2' }), false,
        'within one epoch the revision order still holds');

    // Without any snapshot, a failed first fetch knows only App playback.
    sourceSandbox.state.settings.sourceSync = emptySourceSync();
    const firstFetch = sourceSandbox.fetchAudioSourceOverview();
    respond(sourceSandbox.requests.at(-1), {}, false);
    await firstFetch;
    assert.equal(sourceMode().mode, 'app-playback');
    assert.deepEqual([...sourceMode().modes.map((entry) => entry.key)], ['app-playback']);
    assert.equal(sourceMode().fetch_error, 'Failed to fetch source mode');

    // The fetch error is rendered in the source section, and cleared again.
    const fakeElement = () => ({
        textContent: '', innerHTML: '', value: '', disabled: false, hidden: true,
        classList: { toggle(name, on) { if (name === 'hidden') this.owner.hidden = on; } },
    });
    const renderElements = {
        settingsSourceSelect: fakeElement(), settingsSourceStatus: fakeElement(),
        settingsBluetoothStatus: fakeElement(), settingsStdinStatus: fakeElement(),
    };
    Object.values(renderElements).forEach((element) => { element.classList.owner = element; });
    const renderSandbox = {
        state: sourceSandbox.state, elements: renderElements,
        escapeHtml: (text) => String(text), formatBluetoothModeStatus: () => 'idle',
        formatSampleRateKhz: (rate) => `${rate}`, Array,
    };
    vm.createContext(renderSandbox);
    vm.runInContext(extractFunction('renderSourceSection'), renderSandbox);
    renderSandbox.renderSourceSection();
    assert.equal(renderElements.settingsSourceStatus.hidden, false);
    assert.match(renderElements.settingsSourceStatus.textContent, /Failed to fetch source mode/);
    sourceSandbox.applySourceOverview(full('app-playback', 1));
    renderSandbox.renderSourceSection();
    assert.equal(renderElements.settingsSourceStatus.hidden, true);
    assert.equal(renderElements.settingsSourceStatus.textContent, '');

    // A confirmed-missing saved input stays the selected option, disabled.
    sourceSandbox.applySourceOverview(full('external-input', 2, {
        inputs: [{ key: 'umc::mic', label: 'UMC' }],
        selected_input: { key: 'scarlett::pair:3-4', label: 'Scarlett — Input 3–4',
            available: false, availability: 'unavailable' },
        current_input: null,
    }));
    renderSandbox.renderSourceSection();
    const selectHtml = renderElements.settingsSourceSelect.innerHTML;
    assert.match(selectHtml, /<option value="external-input::scarlett::pair:3-4" disabled>External input: Scarlett — Input 3–4 \(unavailable\)<\/option>/);
    assert.equal(renderElements.settingsSourceSelect.value, 'external-input::scarlett::pair:3-4');

    console.log('STDIN UI tests passed');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
