#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Receiver labels and free routing remain available without a transport selector.
const assert = require('node:assert/strict');
const OutputState = require('../static/output_state.js');

assert.equal(OutputState.routingOutputLabel(4, ['Front L', 'Front R', 'Surround L', 'Surround R', 'Center', 'LFE']),
    'Out 5 · Center');
assert.equal(OutputState.routingOutputLabel(2, []), 'Out 3');
assert.equal(OutputState.routingOutputLabel(2, undefined), 'Out 3');
global.document = { activeElement: null };
const grid = { dataset: {}, innerHTML: '', contains: () => false };
const catalog = {
    device: { channels: 8, channel_labels: ['Front L', 'Front R', 'Rear L', 'Rear R', 'Center', 'LFE', 'Side L', 'Side R'] },
    capabilities: { roles: { surround: ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1', 'sub2'] } },
};
assert.equal(OutputState.modeSelectorVisible(catalog.device.channels), true);
OutputState.renderRoutingGrid(grid, catalog, 'surround',
    ['main_l', 'main_r', 'rear_l', 'rear_r', 'center', 'sub1', 'off', 'off'], 8, false);
assert.match(grid.innerHTML, /<label for="settings-routing-out-6">Out 6 · LFE<\/label>/);
assert.match(grid.innerHTML, /aria-label="Output 8 · Side R role"/);
assert.match(grid.innerHTML, /<option value="side_r">Side R<\/option>/);
assert.match(grid.innerHTML, /<option value="sub1" selected>Sub 1<\/option>/);
console.log('test_hdmi_transport_ui: ok');
