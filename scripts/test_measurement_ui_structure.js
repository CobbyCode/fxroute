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
assert.match(index, /id="measurement-start"[^>]*>LR Stereo<\/button>/);
assert.match(index, /id="measurement-repeat-start"[^>]*>Start LR Repeat<\/button>/);
assert.match(index, /id="measurement-hybrid-open"[^>]*>System Calibration<\/button>/);
assert.ok(index.indexOf('id="measurement-start"') > index.indexOf('id="measurement-sweep-menu"'));
assert.ok(index.indexOf('id="measurement-repeat-start"') > index.indexOf('id="measurement-sweep-menu"'));
assert.ok(index.indexOf('id="measurement-hybrid-open"') > index.indexOf('id="measurement-sweep-menu"'));
assert.match(index, /Speaker and room measurements for system calibration\./);
assert.doesNotMatch(index, /subwoofer alignment/i);
assert.doesNotMatch(index, /Advanced Measurement/);
assert.doesNotMatch(flows, /Advanced Measurement/);

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
