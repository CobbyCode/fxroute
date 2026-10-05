#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Custom input mapping dialog: model, dialog flows and page contracts.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const CustomMapping = require(path.join(root, 'static', 'custom_mapping_ui.js'));

const POSITIONS = [
    ['FL', 'Front L'], ['FR', 'Front R'], ['FC', 'Center'], ['LFE', 'LFE'],
    ['SL', 'Side L'], ['SR', 'Side R'], ['BL', 'Rear L'], ['BR', 'Rear R'],
].map(([id, label]) => ({ id, label }));
const CAPTURE = {
    source: 'alsa_input.usb-Scarlett-00.pro-input-0',
    channels: Array.from({ length: 8 }, (_, index) => ({ index: index + 1, suffix: `AUX${index}` })),
};
const device = { card_base: 'usb-Scarlett-00', label: 'Scarlett 18i20' };
const context = (overrides = {}) => ({
    device, capture: CAPTURE, mapping: null, input_key: null, positions: POSITIONS, ...overrides,
});
const MAPPING = { name: 'External Upmixer', channels: { FL: 3, FR: 4, FC: 5, BR: 1 } };

let passed = 0;
function check(name, condition) {
    assert.ok(condition, name);
    passed += 1;
}

function fakeElement() {
    const classes = new Set();
    const listeners = {};
    return {
        value: '', disabled: false, textContent: '', innerHTML: '',
        classList: {
            add: (name) => classes.add(name), remove: (name) => classes.delete(name),
            toggle: (name, force) => { if (force) classes.add(name); else classes.delete(name); },
            contains: (name) => classes.has(name),
        },
        addEventListener: (type, handler) => { listeners[type] = handler; },
        querySelector: () => null,
        querySelectorAll: () => [],
        trigger: (type, event) => listeners[type](event),
    };
}

function harness({ ctx = context(), confirmAnswer = true, failWith = null } = {}) {
    const elements = {};
    for (const key of ['customMappingPanel', 'customMappingClose', 'customMappingSubtitle', 'customMappingName',
        'customMappingGrid', 'customMappingSave', 'customMappingDelete', 'customMappingStatus']) {
        elements[key] = fakeElement();
    }
    elements.customMappingPanel.classList.add('hidden');
    elements.customMappingDelete.classList.add('hidden');
    const log = { fetches: [], toasts: [], modal: [], refreshes: 0, confirms: [] };
    CustomMapping.init({
        getElements: () => elements,
        fetch: async (url, options = {}) => {
            log.fetches.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : undefined });
            if (failWith && options.method !== 'GET') {
                return { ok: false, status: 400, json: async () => ({ detail: failWith }) };
            }
            return { ok: true, status: 200, json: async () => ctx };
        },
        openModal: (panel, options) => log.modal.push(['open', panel, options]),
        closeModal: (panel) => log.modal.push(['close', panel]),
        confirm: (message) => { log.confirms.push(message); return confirmAnswer; },
        showToast: (message, type) => log.toasts.push([message, type]),
        refreshSources: async () => { log.refreshes += 1; },
    });
    return { elements, log };
}

(async () => {
    // ---- pure model
    const fixedOptions = CustomMapping.channelOptions(CAPTURE, 'FL');
    check('required positions have no Off option', fixedOptions.length === 8 && fixedOptions[0].value === '1');
    const optionalOptions = CustomMapping.channelOptions(CAPTURE, 'FC');
    check('optional positions start with Off', optionalOptions[0].label === 'Off' && optionalOptions.length === 9);
    check('option labels name the input and its channel', optionalOptions[3].label === 'Input 3 · AUX2');
    check('no capture yields only Off', CustomMapping.channelOptions(null, 'FC').length === 1);

    check('a new mapping starts on the first two inputs',
        JSON.stringify(CustomMapping.initialSelection(context())) === JSON.stringify({ FL: '1', FR: '2' }));
    check('a single-channel device preselects nothing',
        Object.keys(CustomMapping.initialSelection(context({ capture: { channels: [{ index: 1, suffix: 'FL' }] } }))).length === 0);
    check('a stored mapping is loaded as strings',
        JSON.stringify(CustomMapping.initialSelection(context({ mapping: MAPPING })))
        === JSON.stringify({ FL: '3', FR: '4', FC: '5', BR: '1' }));
    const shrunk = context({ capture: { channels: CAPTURE.channels.slice(0, 4) }, mapping: MAPPING });
    check('stored inputs beyond the live channel count are dropped',
        JSON.stringify(CustomMapping.initialSelection(shrunk)) === JSON.stringify({ FL: '3', FR: '4', BR: '1' }));

    check('a valid selection has no problem', CustomMapping.validateSelection({ FL: '1', FR: '2' }, POSITIONS) === '');
    check('Front R is required', CustomMapping.validateSelection({ FL: '1' }, POSITIONS) === 'Front R must be mapped.');
    check('Front L is required', CustomMapping.validateSelection({ FR: '1' }, POSITIONS) === 'Front L must be mapped.');
    check('an input can feed one channel only', CustomMapping.validateSelection({ FL: '1', FR: '2', BR: '2' }, POSITIONS)
        === 'Input 2 is used for both Front R and Rear R.');

    const payload = CustomMapping.selectionPayload('  Upmixer ', { FL: '1', FR: '2', FC: '', BR: '8' });
    check('payload trims the name, skips Off and sends numbers',
        payload.name === 'Upmixer' && JSON.stringify(payload.channels) === JSON.stringify({ FL: 1, FR: 2, BR: 8 }));

    const grid = CustomMapping.renderGridHtml(POSITIONS, CAPTURE, { FL: '3', FR: '4' });
    check('one cell per program channel', (grid.match(/settings-routing-cell/g) || []).length === 8);
    check('the selection is marked', /<option value="3" selected>Input 3 · AUX2<\/option>/.test(grid));
    check('unmapped optional channels show Off', /<option value="" selected>Off<\/option>/.test(grid));
    check('cells carry their position for the change handler', /data-custom-mapping-position="BR"/.test(grid));
    check('labels are escaped', !/<script/.test(CustomMapping.renderGridHtml(
        [{ id: 'FL', label: '<script>' }], CAPTURE, {})));

    // ---- open
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING, input_key: 'custom-mapping::usb' }) });
        await CustomMapping.open();
        check('open loads the mapping endpoint', log.fetches[0].url === '/api/audio/custom-mapping' && log.fetches[0].method === 'GET');
        check('open shows the dialog through the shared modal manager',
            !elements.customMappingPanel.classList.contains('hidden') && log.modal[0][0] === 'open'
            && log.modal[0][2].initialFocus === elements.customMappingName);
        check('open fills the name', elements.customMappingName.value === 'External Upmixer');
        check('open names the device', /Scarlett 18i20/.test(elements.customMappingSubtitle.textContent));
        check('open renders the grid', (elements.customMappingGrid.innerHTML.match(/<select/g) || []).length === 8);
        check('Delete is offered for a saved mapping', !elements.customMappingDelete.classList.contains('hidden'));
        check('Save is enabled for a valid mapping', elements.customMappingSave.disabled === false);
        log.modal[0][2].onEscape();
        check('Escape closes the dialog', elements.customMappingPanel.classList.contains('hidden')
            && log.modal.at(-1)[0] === 'close');
    }
    {
        const { elements } = harness();
        await CustomMapping.open();
        check('Delete is hidden without a saved mapping', elements.customMappingDelete.classList.contains('hidden'));
        check('a new mapping starts with an empty name', elements.customMappingName.value === '');
    }
    {
        const { elements } = harness({ ctx: context({ capture: null }) });
        await CustomMapping.open();
        check('a device without inputs disables Save', elements.customMappingSave.disabled === true);
        check('a device without inputs says so', /no input channels/.test(elements.customMappingStatus.textContent));
    }
    {
        const { elements } = harness({ ctx: context({ device: null, capture: null }) });
        await CustomMapping.open();
        check('no selected device is reported', /No audio output device/.test(elements.customMappingStatus.textContent));
    }

    // ---- save
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING }) });
        await CustomMapping.open();
        elements.customMappingName.value = ' Upmixer ';
        await CustomMapping.save();
        const put = log.fetches.find((call) => call.method === 'PUT');
        check('save sends the whole mapping', JSON.stringify(put.body)
            === JSON.stringify({ name: 'Upmixer', channels: { FL: 3, FR: 4, FC: 5, BR: 1 } }));
        check('save confirms, closes and refreshes the inputs', log.toasts.at(-1)[0] === 'Custom mapping saved'
            && elements.customMappingPanel.classList.contains('hidden') && log.refreshes === 1);
    }
    {
        const { elements, log } = harness({ failWith: 'Front R must be mapped' });
        await CustomMapping.open();
        await CustomMapping.save();
        check('a rejected save keeps the dialog open and shows the reason',
            !elements.customMappingPanel.classList.contains('hidden')
            && elements.customMappingStatus.textContent === 'Front R must be mapped'
            && log.refreshes === 0 && log.toasts.length === 0);
        check('the dialog is usable again after a rejected save', elements.customMappingSave.disabled === false);
    }
    {
        const { elements, log } = harness();
        await CustomMapping.open();
        CustomMapping.bind();
        elements.customMappingGrid.trigger('change', {
            target: { dataset: { customMappingPosition: 'FR' }, value: '1' },
        });
        check('a duplicate input is reported and blocks Save',
            /Input 1 is used for both Front L and Front R/.test(elements.customMappingStatus.textContent)
            && elements.customMappingSave.disabled === true);
        await CustomMapping.save();
        check('an invalid selection is never sent', !log.fetches.some((call) => call.method === 'PUT'));
        elements.customMappingGrid.trigger('change', {
            target: { dataset: { customMappingPosition: 'FR' }, value: '2' },
        });
        check('fixing the selection re-enables Save', elements.customMappingSave.disabled === false
            && elements.customMappingStatus.textContent === '');
    }

    // ---- delete
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING }), confirmAnswer: false });
        await CustomMapping.open();
        await CustomMapping.remove();
        check('Delete asks first and a decline changes nothing',
            log.confirms.length === 1 && !log.fetches.some((call) => call.method === 'DELETE'));
        elements.customMappingPanel.classList.add('hidden');
    }
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING }) });
        await CustomMapping.open();
        await CustomMapping.remove();
        check('Delete removes the mapping, closes and refreshes',
            log.fetches.some((call) => call.method === 'DELETE') && log.toasts.at(-1)[0] === 'Custom mapping deleted'
            && elements.customMappingPanel.classList.contains('hidden') && log.refreshes === 1);
    }
    {
        const { log } = harness();
        await CustomMapping.open();
        await CustomMapping.remove();
        check('Delete without a saved mapping does nothing', !log.fetches.some((call) => call.method === 'DELETE'));
    }

    // ---- page contracts
    check('the dialog uses the shared overlay skeleton',
        /<div id="custom-mapping-panel" class="manage-overlay custom-mapping-overlay hidden">\s*<div class="manage-overlay-backdrop"><\/div>\s*<section class="manage-dialog" role="dialog" aria-modal="true" aria-labelledby="custom-mapping-title">/.test(html));
    for (const id of ['custom-mapping-close', 'custom-mapping-subtitle', 'custom-mapping-name', 'custom-mapping-grid',
        'custom-mapping-save', 'custom-mapping-delete', 'custom-mapping-status']) {
        check(`page has #${id}`, html.includes(`id="${id}"`));
        const camel = id.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
        check(`app.js resolves #${id}`, appJs.includes(`${camel}: document.getElementById('${id}')`));
    }
    check('the module loads before app.js with a versioned URL',
        /<script src="\/static\/custom_mapping_ui\.js\?v=\d+\.\d+\.\d+"><\/script>[\s\S]*<script src="\/static\/app\.js\?v=\d+\.\d+\.\d+">/.test(html));
    check('the Input select offers the configuration entry last',
        /STDIN<\/option>`,\s*'<option value="custom-mapping">Custom Mapping…<\/option>',\s*\];/.test(appJs));
    check('choosing the entry restores the selection before opening the dialog',
        /value === 'custom-mapping'\) \{[^}]*renderSourceSection\(\);\s*void window\.FXRouteCustomMapping\.open\(\);/.test(appJs));
    check('the entry is never saved as a source selection',
        !/saveAudioSourceSelection\('custom-mapping'/.test(appJs));
    check('the dialog stacks above the settings dialog',
        /\.custom-mapping-overlay\s*\{[^}]*z-index:\s*calc\(var\(--z-overlay\) \+ 10\)/.test(css));

    console.log(`test_custom_mapping_ui.js: ${passed} checks passed`);
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
