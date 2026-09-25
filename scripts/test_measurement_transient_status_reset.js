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
// Zustaende loeschen (statusText, Progress-State, autoSubResult, Speaker-
// Align-Ergebnis), solange kein Job laeuft. Die Feature-Zeilen (Sweep,
// Auto Sub, Speaker Align) werden aus dem Progress-State gerendert und
// fallen damit beim naechsten Render auf ihren Idle-Hinweis zurueck; der
// Reset schreibt sie nicht selbst. Ungespeicherte Messdaten
// (autoSubMeasurements, currentMeasurement) bleiben erhalten: das ist
// speicherbarer Inhalt, kein transienter Status. Ein laufender Job darf
// nie angeruehrt werden.

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
const toggleEnd = appSource.indexOf('function drawMeasurementPeqOverlay(', toggleStart);
assert.notEqual(toggleEnd, -1, 'missing toggleMeasurementPanel end anchor');
const toggleBody = appSource.slice(toggleStart, toggleEnd);
assert.ok(
    toggleBody.includes('resetMeasurementTransientStatus();'),
    'toggleMeasurementPanel open path must call resetMeasurementTransientStatus()',
);

// Extract the reset function verbatim: balance braces from the function
// start, ignoring strings and line comments.
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

// The idle note lives once in the page shell; no JS copy of it.
assert.doesNotMatch(appSource, /Scans sub delay around crossover/);

function runReset(stateMeasurement, withResultsEl) {
    const resultsEl = withResultsEl === false ? undefined : { innerHTML: stateMeasurement.__resultsHtml || '' };
    const sandbox = {
        state: { measurement: Object.assign({}, stateMeasurement) },
        elements: { measurementSpeakerAlignResults: resultsEl },
    };
    delete sandbox.state.measurement.__resultsHtml;
    vm.createContext(sandbox);
    const script = `${extractResetFunction()}\nresetMeasurementTransientStatus();`;
    vm.runInContext(script, sandbox);
    return { measurement: sandbox.state.measurement, resultsEl };
}

// 1. Stale cancelled state is cleared on reopen.
{
    const { measurement } = runReset({
        statusText: 'Auto Sub cancelled.',
        autoSubResult: null,
        autoSubMeasurements: [],
    });
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
    assert.equal(measurement.progressKind, '');
    assert.equal(measurement.progressText, '');
}

// 2. Stale completed state is cleared, unsaved graph data is kept.
{
    const kept = [{ id: 'autosub-baseline', traces: [{ channel: 'left' }] }];
    const { measurement } = runReset({
        statusText: 'Auto Sub 2.2 applied: Sub 1 1.23 ms (was 0.00 ms) · Sub 2 0.42 ms (was 0.00 ms) · Combined 91.2 %',
        autoSubResult: { winner: {}, mode: 'subwoofer-2.2' },
        autoSubMeasurements: kept,
        currentMeasurementName: 'AutoSub',
    });
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
    assert.equal(measurement.autoSubMeasurements, kept);
    assert.equal(measurement.currentMeasurementName, 'AutoSub');
}

// 3. A running AutoSub job is never touched.
{
    const { measurement } = runReset({
        statusText: '',
        autoSubResult: null,
        autoSubInFlight: true,
        autoSubJobId: 'job-1',
        activeMeasurementKind: 'auto_sub',
        progressKind: 'auto_sub',
        progressText: 'Optimizing Sub 1: 3/12 candidates (3/8 sweeps)',
    });
    assert.equal(measurement.progressKind, 'auto_sub');
    assert.equal(measurement.progressText, 'Optimizing Sub 1: 3/12 candidates (3/8 sweeps)');
}

// 4. A running single sweep is never touched.
{
    const { measurement } = runReset({
        statusText: 'Could not cancel the sweep: request failed',
        activeJobId: 'job-2',
        activeMeasurementKind: 'single',
        progressKind: 'sweep',
        progressText: 'Running sweep…',
    });
    assert.equal(measurement.statusText, 'Could not cancel the sweep: request failed');
    assert.equal(measurement.progressText, 'Running sweep…');
}

// 4b. A stale Speaker Align outcome and result table are cleared.
{
    const { measurement, resultsEl } = runReset({
        statusText: 'Speaker Align Left verified and applied.',
        speakerAlignResult: { confirmed: true },
        speakerAlignResults: { left: { confirmed: true } },
        __resultsHtml: '<table></table>',
    });
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.speakerAlignResult, null);
    assert.equal(measurement.speakerAlignResults, null);
    assert.equal(resultsEl.innerHTML, '');
}

// 5. A stale sweep error is cleared even without the inline element.
{
    const { measurement } = runReset({
        statusText: 'Sweep failed.',
        autoSubResult: null,
    }, false);
    assert.equal(measurement.statusText, '');
    assert.equal(measurement.autoSubResult, null);
}

console.log('measurement transient status reset: OK');
