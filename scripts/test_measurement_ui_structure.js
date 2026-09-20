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
assert.match(index, /id="measurement-sweep-start"[^>]*class="btn-secondary"[^>]*>Run Single Sweep<\/button>/);
assert.match(index, /id="measurement-sweep-side-row"[^>]*aria-label="Single sweep side"/);
assert.match(index, /data-sweep-side="left"[^>]*>Left<\/button>/);
assert.match(index, /data-sweep-side="stereo"[^>]*>Stereo<\/button>/);
assert.match(index, /data-sweep-side="right"[^>]*>Right<\/button>/);
assert.doesNotMatch(index, /data-measurement-channel/, 'sides are area-scoped, not output-channel chips');
assert.ok(index.indexOf('measurement-area-row') < index.indexOf('id="measurement-sweep-start"'), 'the area line sits above the single sweep action');
assert.match(app, /const area = measurementAreaFromCatalog\(\);/);
assert.match(app, /area\.sides\.includes\(state\.measurement\.sweepSide\)/);
assert.match(app, /formData\.append\('measurement_bank', area\.bank_id\);\n    \}/);
assert.doesNotMatch(app, /state\.measurement\.selectedChannel/, 'the area is the only sweep scope; the channel fallback is gone');
assert.match(app, /elements\.measurementAreaIndicator\.textContent = area \? area\.label : 'Global'/);
assert.doesNotMatch(app, /\[data-measurement-channel\]/, 'no leftover output-channel chip handlers');
assert.match(app, /void ensureMeasurementAreaCatalog\(\);/, 'opening the panel loads the area catalog');
assert.match(app, /state\.measurement\.sweepSide/, 'the single-sweep side persists in measurement state');
assert.match(app, /area\.sides\.includes\(state\.measurement\.sweepSide\)/, 'a stored side only applies inside a stereo area');
assert.match(app, /data-sweep-side/, 'side chips drive the single sweep');
// Saved results show the frozen area they were captured in (legacy results
// without a target stay unlabelled, never silently "Global").
assert.match(app, /function measurementAreaBadge\(measurement\) \{/);
assert.match(app, /target\.schema !== 'fxroute\.measurement-target'\) return null;/);
assert.match(app, /target\.legacy \|\| target\.schema/);
assert.match(app, /measurement-area-badge\$\{areaBadge\.stale \? ' is-stale' : ''\}/);
assert.match(app, /Measured area: \$\{areaBadge\.title\}/);
// A summed-sub bank is mono on the preset side: per-side PEQ takes and a
// Both convolver take cannot compile there, so the UI disables them.
assert.match(app, /function measurementBankSumsBothInputs\(\) \{/);
assert.match(app, /function syncMeasurementSummedSubTakeModes\(\) \{/);
assert.match(app, /if \(mode !== 'both' && measurementBankSumsBothInputs\(\)\)/);
assert.match(app, /if \(mode === 'both' && measurementBankSumsBothInputs\(\)\)/);
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
// Speaker Auto Alignment is compact: two equal buttons side by side, one
// short note below, status and results underneath. No sequence line and no
// long explanation paragraphs.
assert.match(index, /id="measurement-speaker-align-left"[^>]*>Align Left<\/button>/);
assert.match(index, /id="measurement-speaker-align-right"[^>]*>Align Right<\/button>/);
// Horizontal pattern like the other actions: button pair left, one-line
// note to its right inside a speaker row wrapper.
assert.match(index, /class="measurement-workflow-speaker-row"/);
assert.ok(index.indexOf('measurement-workflow-speaker-buttons') < index.indexOf('Aligns each way of the selected speaker with the microphone fixed.'),
    'the note sits right of the button pair');
assert.match(index, /Aligns each way of the selected speaker with the microphone fixed\./);
// The sentence lives exactly once in the static note; the status line stays
// empty while idle so it never renders twice.
assert.equal(index.split('Aligns each way of the selected speaker with the microphone fixed.').length - 1, 1);
assert.doesNotMatch(flows, /Aligns each way of the selected speaker/);
assert.doesNotMatch(app, /Aligns each way of the selected speaker/);
assert.doesNotMatch(index, /measurement-speaker-align-sequence/);
assert.doesNotMatch(index, /Place the mic in front/);
assert.doesNotMatch(index, /then repeat to verify/);
assert.doesNotMatch(flows, /measurementSpeakerAlignSequence/);
assert.doesNotMatch(flows, /then repeat to verify/);
assert.match(measurementCss, /\.measurement-workflow-speaker-buttons/);
assert.match(measurementCss, /\.measurement-workflow-speaker-buttons[\s\S]*?width:\s*190px/);
assert.match(measurementCss, /\.measurement-workflow-speaker-row[\s\S]*?grid-template-columns:\s*auto minmax\(0, 1fr\)/);
// Shared vertical rhythm (heading → buttons → divider): the heading row is
// bottom-aligned so the Setup button cannot float the Measurements label,
// and idle speaker-align status/results collapse instead of reserving
// phantom flex gaps below the buttons.
assert.match(measurementCss, /\.measurement-workflow-heading[\s\S]*?align-items:\s*flex-end/);
assert.match(measurementCss, /#measurement-speaker-align-status:empty/);
assert.match(measurementCss, /#measurement-speaker-align-results:empty/);
// Calibration keeps the same button → divider distance as the other
// sections: the status line margin sums the section list gap and the
// section divider margin.
assert.match(measurementCss, /#measurement-setup-status[\s\S]*?margin-top:\s*calc\(0\.5rem \+ 0\.35rem\)/);
assert.match(responsiveCss, /\.measurement-workflow-speaker-row[\s\S]*?flex-direction:\s*column/);
assert.match(responsiveCss, /\.measurement-workflow-speaker-buttons[\s\S]*?flex:\s*1 1 0/);
assert.doesNotMatch(index, /measurement-workflow-speaker-cancel/);
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
