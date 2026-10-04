#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const ui = require('../static/output_state.js');
const select = { innerHTML: '', value: '' };
ui.renderModeSelect(select, null, 'surround');
assert.match(select.innerHTML, /value="surround" selected/);
assert.equal(ui.roleLabel('main_l', 'surround'), 'Front L');
assert.equal(ui.roleLabel('main_r', 'surround'), 'Front R');
assert.equal(ui.roleLabel('side_l'), 'Side L');
assert.equal(ui.roleLabel('rear_r'), 'Rear R');
assert.equal(ui.roleLabel('sub1'), 'Sub 1');
assert.equal(ui.topologySummary({ mode: 'surround', sub_mode: 'dual-mono' }), 'Surround · Dual-mono subs');
assert.match(ui.modeHint('surround', { sub_roles: [] }, 10), /LFE.*Front L\/R/);
const view = ui.subwooferView({ active_mode: 'surround', modes: { surround: {
    topology: { sub_roles: ['sub1', 'sub2'], sub_mode: 'dual-mono', roles: ['main_l', 'main_r', 'sub1', 'sub2'] },
    processing: { sub1: { level_db: -3 }, sub2: { level_db: -6 } }, bass_management: { frequency_hz: 80 }
} } });
assert.equal(view.mode, 'subwoofer-2.2');
assert.equal(view.crossover.stereo, false);
console.log('Surround UI tests passed');
