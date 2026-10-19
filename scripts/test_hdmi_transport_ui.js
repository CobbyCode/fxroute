#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// HDMI layout selector and the receiver channel labels of the routing grid.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Hdmi = require('../static/hdmi_transport_ui.js');
const OutputState = require('../static/output_state.js');

const indexSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

function output(overrides = {}) {
    return {
        key: 'alsa_output.pci-0000_00_1f.3.hdmi-surround',
        hdmi: {
            transport: '5.1',
            desired_transport: '5.1',
            receiver: { name: 'marantz-AVR', lpcm_channels: 8, lpcm_rates: [32000, 44100, 192000] },
            transports: [
                { id: 'stereo', label: 'Stereo', channels: 2, available: true, reason: null },
                { id: '5.1', label: '5.1', channels: 6, available: true, reason: null },
                { id: '7.1', label: '7.1', channels: 8, available: true, reason: null },
            ],
            ...overrides,
        },
    };
}

function element() {
    return {
        innerHTML: '', value: '', disabled: false, textContent: '',
        classList: { hidden: false, toggle(name, on) { if (name === 'hidden') this.hidden = !!on; } },
    };
}

// Non-HDMI outputs never show the selector.
assert.deepEqual(Hdmi.view({ key: 'alsa_output.usb-X.analog-stereo' }), { visible: false });
assert.deepEqual(Hdmi.view(null), { visible: false });

{
    const model = Hdmi.view(output());
    assert.equal(model.value, '5.1');
    assert.deepEqual(model.options.map(item => [item.value, item.label, item.disabled]),
        [['stereo', 'Stereo', false], ['5.1', '5.1', false], ['7.1', '7.1', false]]);
    assert.equal(model.hint, 'marantz-AVR · LPCM up to 8 ch, 32–192 kHz');
}

{
    // A stereo TV: the surround layouts stay visible but cannot be chosen.
    const model = Hdmi.view(output({
        transport: 'stereo', desired_transport: '7.1',
        receiver: { name: 'TV', lpcm_channels: 2, lpcm_rates: [48000] },
        transports: [
            { id: 'stereo', label: 'Stereo', channels: 2, available: true, reason: null },
            { id: '5.1', label: '5.1', channels: 6, available: false, reason: 'TV accepts at most 2 LPCM channels' },
            { id: '7.1', label: '7.1', channels: 8, available: false, reason: 'TV accepts at most 2 LPCM channels' },
        ],
    }));
    assert.deepEqual(model.options.map(item => item.disabled), [false, true, true]);
    assert.equal(model.options[2].label, '7.1 (unavailable)');
    assert.equal(model.options[2].title, 'TV accepts at most 2 LPCM channels');
    assert.match(model.hint, /^TV · LPCM up to 2 ch, 48 kHz · 7\.1 chosen; plays Stereo/);
}

{
    // A pending change shows the requested layout and locks the select.
    const elements = { group: element(), select: element(), hint: element() };
    Hdmi.render(elements, output(), { pending: '7.1' });
    assert.equal(elements.group.classList.hidden, false);
    assert.equal(elements.select.value, '7.1');
    assert.equal(elements.select.disabled, true);
    Hdmi.render(elements, { key: 'other' }, {});
    assert.equal(elements.group.classList.hidden, true);
}

{
    const calls = [];
    const fetchImpl = async (url, options) => {
        calls.push([url, JSON.parse(options.body)]);
        return { ok: false, json: async () => ({ detail: '7.1 is not available: No receiver is connected' }) };
    };
    Hdmi.save(fetchImpl, 'alsa_output.pci-0000_00_1f.3.hdmi-stereo', '7.1').then(
        () => assert.fail('expected a rejection'),
        (error) => {
            assert.equal(error.message, '7.1 is not available: No receiver is connected');
            assert.deepEqual(calls, [['/api/audio/outputs/transport',
                { key: 'alsa_output.pci-0000_00_1f.3.hdmi-stereo', transport: '7.1' }]]);
        });
}

// Routing grid: "Out 5 · Center" from the device's channel labels.
assert.equal(OutputState.routingOutputLabel(4, ['Front L', 'Front R', 'Surround L', 'Surround R', 'Center', 'LFE']),
    'Out 5 · Center');
assert.equal(OutputState.routingOutputLabel(2, []), 'Out 3');
assert.equal(OutputState.routingOutputLabel(2, undefined), 'Out 3');
{
    global.document = { activeElement: null };
    const grid = { dataset: {}, innerHTML: '', contains: () => false };
    const catalog = {
        device: { channel_labels: ['Front L', 'Front R', 'Rear L', 'Rear R', 'Center', 'LFE', 'Side L', 'Side R'] },
        capabilities: { roles: { surround: ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1', 'sub2'] } },
    };
    OutputState.renderRoutingGrid(grid, catalog, 'surround',
        ['main_l', 'main_r', 'rear_l', 'rear_r', 'center', 'sub1', 'side_l', 'side_r'], 8, false);
    assert.match(grid.innerHTML, /<label for="settings-routing-out-6">Out 6 · LFE<\/label>/);
    assert.match(grid.innerHTML, /aria-label="Output 8 · Side R role"/);
    assert.match(grid.innerHTML, /<option value="sub1" selected>Sub 1<\/option>/);
}

// The page loads the module and offers the selector next to the device.
assert.match(indexSource, /<script src="\/static\/hdmi_transport_ui\.js\?v=\d+\.\d+\.\d+"><\/script>/);
assert.match(indexSource, /id="settings-hdmi-transport-select"/);

console.log('test_hdmi_transport_ui: ok');
