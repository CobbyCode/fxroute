#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const ui = require('../static/usb_gadget_ui.js');

function element() {
    const classes = new Set(['hidden']);
    return {value: '', disabled: false, textContent: '',
        classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x)},
        addEventListener() {}, querySelector() { return null; }};
}

async function run() {
    assert.equal(ui.configurationOption({supported: false}), '');
    assert.equal(ui.configurationOption(undefined), '');
    assert.match(ui.configurationOption({supported: true}), /USB Audio Gadget…/);
    const options = ui.inputOptions([
        {key: 'normal', label: 'Line in'},
        {key: 'usb1', label: 'Input 1–2', device_label: 'UAC2 Gadget', usb_gadget: true},
        {key: 'usb2', label: 'Input 3–4', device_label: 'UAC2 Gadget', usb_gadget: true},
        {key: 'custom', label: '<Custom>', device_label: 'UAC2 Gadget', usb_gadget: true},
    ]);
    assert.match(options, /value="external-input::normal"/);
    assert.equal((options.match(/<optgroup/g) || []).length, 1);
    assert.match(options, /&lt;Custom&gt;/);
    assert.match(options, /value="external-input::custom"/);

    const elements = {panel: element(), close: element(), select: element(), status: element()};
    let current = {supported: true, enabled: false, can_enable: true, helper_installed: true, message: 'Off'};
    let fail = false;
    let refreshes = 0;
    const requests = [];
    let modal;
    ui.init({getElements: () => elements,
        openModal: (panel, options) => { modal = options; }, closeModal() {},
        refreshSources: async () => { refreshes++; },
        fetch: async (url, options) => {
            requests.push({url, ...options});
            if (options.method === 'POST') {
                if (fail) return {ok: false, json: async () => ({detail: 'Controller occupied'})};
                current = {...current, enabled: JSON.parse(options.body).enabled, reboot_required: true,
                    message: 'Restart required to enable the USB device controller.'};
            }
            return {ok: true, json: async () => current};
        },
    });
    await ui.open();
    assert.equal(elements.panel.classList.contains('hidden'), false);
    assert.equal(elements.select.value, 'off');
    assert.equal(elements.select.disabled, false);
    await ui.save(true);
    assert.deepEqual(JSON.parse(requests.at(-1).body), {enabled: true});
    assert.equal(elements.select.value, 'on');
    assert.match(elements.status.textContent, /Restart required/);
    assert.equal(refreshes, 1);
    fail = true;
    await ui.save(false);
    assert.equal(elements.select.value, 'on');
    assert.match(elements.status.textContent, /occupied/);
    assert.equal(elements.select.disabled, false);
    fail = false;
    await ui.save(false);
    assert.equal(elements.select.value, 'off');
    modal.onEscape();
    assert.equal(elements.panel.classList.contains('hidden'), true);
    current = {...current, enabled: false, can_enable: false, helper_installed: false};
    await ui.open();
    assert.equal(elements.select.disabled, true);

    // Reopening during a pending mutation must re-read its eventual state.
    current = {...current, enabled: false, can_enable: true, helper_installed: true};
    let finish;
    ui.init({fetch: async (_url, options) => {
        if (options.method === 'POST') {
            await new Promise(resolve => { finish = resolve; });
            current = {...current, enabled: true};
        }
        return {ok: true, json: async () => current};
    }});
    await ui.open();
    const saving = ui.save(true);
    ui.close();
    await ui.open();
    finish();
    await saving;
    assert.equal(elements.select.value, 'on');
    assert.equal(elements.select.disabled, false);
    console.log('USB gadget UI: menu, grouping, toggle, errors, reboot and modal checks passed');
}
run().catch(error => { console.error(error); process.exit(1); });
