#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Measurement Assistant transient-status reset regression:
// Auto Sub Optimize starten -> abbrechen -> Fenster schliessen -> erneut
// oeffnen zeigte die alten cancelled-/completed-/error-/Progress-/Result-
// Meldungen weiter an. Ursache: der Poll-Loop schreibt statusText und den
// Inline-Status (#measurement-auto-sub-status) direkt, aber kein Render-
// Pfad stellt sie je wieder her; schliessen/oefnen ruehrte den State nicht
// an, ein Seiten-Refresh war noetig.
//
// Beim Oeffnen muss resetMeasurementTransientStatus() alte transiente
// Zustaende loeschen (statusText, autoSubResult, Inline-Status), solange
// kein Job laeuft. Ungespeicherte Messdaten (autoSubMeasurements,
// currentMeasurement) bleiben erhalten: das ist speicherbarer Inhalt,
// kein transienter Status. Ein laufender Job darf nie angeruehrt werden.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');

// The open branch of toggleMeasurementPanel() must run the reset before
// the first render of the freshly opened panel.
const toggleStart = appSource.indexOf('function toggleMeasurementPanel(');
assert.notEqual(toggleStart, -1, 'missing toggleMeasurementPanel');
const toggleEnd = appSource.indexOf('function getSelectedMeasurementInput(', toggleStart);
assert.notEqual(toggleEnd, -1, 'missing toggleMeasurementPanel end anchor');
const toggleBody = appSource.slice(toggleStart, toggleEnd);
assert.ok(
    toggleBody.includes('resetMeasurementTransientStatus();'),
    'toggleMeasurementPanel open path must call resetMeasurementTransientStatus()',
);

// Extract the default-text const and the reset function verbatim: balance
// braces from the function start, ignoring strings and line comments.
function extractResetFunction() {
    const marker = 'function resetMeasurementTransientStatus() {';
    const start = appSource.indexOf(marker);
    assert.notEqual(start, -1, 'missing resetMeasurementTransientStatus');
    let depth = 0;
    let quote = '';
    let escaped = false;
    let lineComment = false;
    for (let index = start; index < appSource.length; index += 1) {
        const char = appSource[index];
        const next = appSource[index + 1];
        if (lineComment) {
            if (char === '\n') lineComment = false;
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === '/' && next === '/') {
            lineComment = true;
            index += 1;
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}') {
            depth -= 1;
            if (depth === 0) return appSource.slice(start, index + 1);
        }
    }
    throw new Error('unterminated resetMeasurementTransientStatus block');
}

const constMarker = 'const MEASUREMENT_AUTO_SUB_STATUS_DEFAULT_TEXT =';
const constStart = appSource.indexOf(constMarker);
assert.notEqual(constStart, -1, 'missing status default-text const');
const constEnd = appSource.indexOf(';', constStart);
assert.notEqual(constEnd, -1, 'unterminated status default-text const');

const DEFAULT_TEXT = 'Scans sub delay around crossover, picks best alignment.';

function runReset(stateMeasurement, withStatusEl) {
    const statusEl = withStatusEl === false ? undefined : { textContent: stateMeasurement.__statusElText || '' };
    const sandbox = {
        state: { measurement: Object.assign({}, stateMeasurement) },
        elements: { measurementAutoSubStatus: statusEl },
    };
    delete sandbox.state.measurement.__statusElText;
    vm.createContext(sandbox);
    const script = `${appSource.slice(constStart, constEnd + 1)}\n${extractResetFunction()}\nresetMeasurementTransientStatus();`;
    vm.runInContext(script, sandbox);
    return { measurement: sandbox.state.measurement, statusEl };
}

// 1. Stale cancelled state is cleared on reopen.
{
    const { measurement, statusEl } = runReset({
        statusText: 'Auto Sub Optimize cancelled.',
        autoSubResult: null,
        autoSubMeasurements: [],
        __statusElText: 'Auto Sub Optimize cancelled.',
    });
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
    assert.equal(statusEl.textContent, DEFAULT_TEXT);
}

// 2. Stale completed state is cleared, unsaved graph data is kept.
{
    const kept = [{ id: 'autosub-baseline', traces: [{ channel: 'left' }] }];
    const { measurement, statusEl } = runReset({
        statusText: 'AutoSub 2.2 applied: Sub 1 1.23 ms (was 0.00 ms) · Sub 2 0.42 ms (was 0.00 ms) · Combined 91.2 %',
        autoSubResult: { winner: {}, mode: 'subwoofer-2.2' },
        autoSubMeasurements: kept,
        currentMeasurementName: 'AutoSub',
        __statusElText: 'Sub 1: 1.23 ms (91.2 %)',
    });
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
    assert.equal(statusEl.textContent, DEFAULT_TEXT);
    assert.equal(measurement.autoSubMeasurements, kept);
    assert.equal(measurement.currentMeasurementName, 'AutoSub');
}

// 3. A running AutoSub job is never touched.
{
    const { measurement, statusEl } = runReset({
        statusText: 'Auto Sub Optimize: running (3/8)',
        autoSubResult: null,
        autoSubInFlight: true,
        autoSubJobId: 'job-1',
        activeMeasurementKind: 'auto_sub',
        __statusElText: 'Optimizing Sub 1: 3/12 candidates (3/8 sweeps)',
    });
    assert.equal(measurement.statusText, 'Auto Sub Optimize: running (3/8)');
    assert.equal(statusEl.textContent, 'Optimizing Sub 1: 3/12 candidates (3/8 sweeps)');
}

// 4. A running single sweep is never touched.
{
    const { measurement } = runReset({
        statusText: 'Sweep running…',
        activeJobId: 'job-2',
        activeMeasurementKind: 'single',
        __statusElText: 'Sweep running…',
    });
    assert.equal(measurement.statusText, 'Sweep running…');
}

// 5. A stale sweep error is cleared even without the inline element.
{
    const { measurement } = runReset({
        statusText: 'Measurement failed',
        autoSubResult: null,
    }, false);
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
}

console.log('measurement transient status reset: OK');
