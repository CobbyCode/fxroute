#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const index = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const flows = fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8');
const measurementCss = fs.readFileSync(path.join(root, 'static', 'css', '_measurement.css'), 'utf8');
const responsiveCss = fs.readFileSync(path.join(root, 'static', 'css', '_responsive.css'), 'utf8');

assert.match(index, /<h4 class="measurement-workflow-label">Measurements<\/h4>/);
assert.match(index, /id="measurement-sweep-toggle"[^>]*>Start Sweep<\/button>/);
assert.match(index, /id="measurement-sweep-menu"[^>]*class="[^"]*hidden[^"]*"/);
assert.doesNotMatch(index, /L \/ R \/ Stereo/);
assert.doesNotMatch(index, /measurement-workflow-menu-label/);
assert.doesNotMatch(index, /data-measurement-channel/, 'the area selector owns the scope; no output-channel chips');
assert.match(index, /id="measurement-area-row"[^>]*role="group"[^>]*aria-label="Selected measurement area"/);
assert.match(index, /<span class="measurement-area-label">Measuring area<\/span>/);
assert.match(index, /id="measurement-area-indicator"[^>]*aria-live="polite">Global<\/span>/);
assert.match(index, /id="measurement-sweep-start"[^>]*>Run Single Sweep<\/button>/);
assert.ok(index.indexOf('measurement-area-row') < index.indexOf('id="measurement-sweep-start"'), 'the area line sits above the single sweep action');
assert.match(app, /const area = measurementAreaFromCatalog\(\);/);
assert.match(app, /formData\.append\('channel', area \? area\.channel : \(state\.measurement\.selectedChannel \|\| 'left'\)\)/);
assert.match(app, /formData\.append\('measurement_bank', area\.bank_id\)/);
assert.match(app, /elements\.measurementAreaIndicator\.textContent = area \? area\.label : 'Global'/);
assert.doesNotMatch(app, /\[data-measurement-channel\]/, 'no leftover output-channel chip handlers');
assert.match(app, /void ensureMeasurementAreaCatalog\(\);/, 'opening the panel loads the area catalog');
assert.match(index, /id="measurement-repeat-start"[^>]*>Start LR Repeat<\/button>/);
assert.match(index, /id="measurement-repeat-note" class="measurement-repeat-help">Repeated L\/R sweeps for more precision\.<\/span>/);
// A one-sided area (fed by one input only) must disable the repeat and explain
// why in the note, while a running repeat stays cancellable.
assert.match(app, /function measurementRepeatBlockedReason\(\) \{/);
assert.match(app, /if \(!area \|\| area\.repeat_supported !== false\) return '';/);
assert.match(app, /const repeatBlockedReason = measurementRepeatBlockedReason\(\);/);
assert.match(app, /elements\.measurementRepeatStartBtn\.disabled = repeatBlockedReason && !lrActive/);
assert.match(app, /syncMeasurementRepeatNote\(lrActive, repeatBlockedReason\);/);
assert.match(app, /elements\.measurementRepeatNote\.textContent = lrActive \|\| !blockedReason/);
assert.match(app, /showToast\(repeatBlockedReason, 'warning'\)/);
assert.match(index, /id="measurement-hybrid-open"[^>]*>Advanced<\/button>/);
assert.match(index, /Combined Speaker and Room Measurement/);
assert.match(index, /id="measurement-spl-calibration-open"[^>]*>SPL Calibration<\/button>/);
assert.match(index, /Calibrate your loudness reference\./);
assert.ok(index.indexOf('id="measurement-repeat-start"') > index.indexOf('id="measurement-sweep-menu"'));
assert.ok(index.indexOf('id="measurement-hybrid-open"') > index.indexOf('id="measurement-sweep-menu"'));
assert.doesNotMatch(index, /subwoofer alignment/i);
assert.doesNotMatch(index, /for correction/i);
assert.doesNotMatch(index, /System Calibration/);
assert.doesNotMatch(flows, /System Calibration/);
assert.doesNotMatch(index, /Advanced Measurement/);
assert.doesNotMatch(flows, /Subwoofer Alignment/i);

assert.match(index, /id="effects-toggle-import"[^>]*>Import<\/button>/);
assert.match(app, /elements\.effectsToggleImportBtn\.textContent = shouldOpen \? 'Close Import' : 'Import';/);
assert.match(app, /elements\.toggleImportBtn\.textContent = 'Close Import';/);
assert.ok(!app.includes(`textContent = '${String.fromCharCode(0x2212)} Close'`));
assert.match(index, /id="toggle-import"[^>]*aria-expanded="false"[^>]*aria-controls="library-import-panel"/);
assert.doesNotMatch(responsiveCss, /#toggle-import::before/);
const mobileImportRule = responsiveCss.match(/\.library-toolbar #toggle-import\s*\{([\s\S]*?)\}/)?.[1] || '';
assert.ok(mobileImportRule, 'mobile library import rule is present');
assert.doesNotMatch(mobileImportRule, /font-size:\s*0(?:\s*;|\s*$)/);

assert.match(measurementCss, /\.measurement-workflow-menu-panel/);
assert.match(responsiveCss, /\.measurement-workflow-menu-panel[\s\S]*?position:\s*static/);

console.log('measurement UI structure tests: ok');
