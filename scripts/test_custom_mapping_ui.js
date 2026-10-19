#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Custom input mapping dialog: model, dialog flows and page contracts.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
const UMC_BASE = 'usb-Behringer-00';
const DEVICES = [
    { card_base: 'usb-Scarlett-00', label: 'Scarlett 18i20', channel_count: 8, has_mapping: false },
    { card_base: UMC_BASE, label: 'UMC204HD 192k', channel_count: 2, has_mapping: true },
];
const context = (overrides = {}) => ({
    device, capture: CAPTURE, mapping: null, input_key: null, positions: POSITIONS, devices: DEVICES, ...overrides,
});
const UMC_MAPPING = { name: 'Line in', channels: { FL: 2, FR: 1 } };
const UMC_CONTEXT = context({
    device: { card_base: UMC_BASE, label: 'UMC204HD 192k' },
    capture: { channels: [{ index: 1, suffix: 'MONO' }, { index: 2, suffix: 'MONO' }] },
    mapping: UMC_MAPPING, input_key: `custom-mapping::${UMC_BASE}`,
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

function harness({ ctx = context(), contexts = { [UMC_BASE]: UMC_CONTEXT }, confirmAnswer = true, failWith = null } = {}) {
    const elements = {};
    for (const key of ['customMappingPanel', 'customMappingClose', 'customMappingSubtitle', 'customMappingDevice',
        'customMappingName', 'customMappingGrid', 'customMappingSave', 'customMappingDelete', 'customMappingStatus']) {
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
            const named = (/[?&]device=([^&]*)/.exec(url) || [])[1];
            const base = named ? decodeURIComponent(named) : (options.body ? JSON.parse(options.body).device : null);
            const answer = base && base !== device.card_base ? contexts[base] : ctx;
            if (!answer) return { ok: false, status: 400, json: async () => ({ detail: `Unknown audio device: ${base}` }) };
            return { ok: true, status: 200, json: async () => answer };
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
    check('option labels name the input only', optionalOptions[3].label === 'Input 3');
    check('no capture yields only Off', CustomMapping.channelOptions(null, 'FC').length === 1);
    check('every option label is Off or "Input <number>"',
        optionalOptions.every((option) => /^(Off|Input \d+)$/.test(option.label)));
    check('option labels hide the PipeWire channel suffix',
        !optionalOptions.some((option) => /AUX\d|FL|FR|RL|RR/.test(option.label)));
    check('option values still carry the capture channel index',
        optionalOptions.map((option) => option.value).join(',') === ',1,2,3,4,5,6,7,8');

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
    check('the selection is marked', /<option value="3" selected>Input 3<\/option>/.test(grid));
    check('the rendered grid never shows a channel suffix', !/AUX\d/.test(grid));
    check('unmapped optional channels show Off', /<option value="" selected>Off<\/option>/.test(grid));
    check('cells carry their position for the change handler', /data-custom-mapping-position="BR"/.test(grid));
    check('labels are escaped', !/<script/.test(CustomMapping.renderGridHtml(
        [{ id: 'FL', label: '<script>' }], CAPTURE, {})));

    const deviceChoice = CustomMapping.deviceOptions(context());
    check('every device is a select option',
        deviceChoice.length === 2 && deviceChoice[0].value === 'usb-Scarlett-00' && deviceChoice[1].value === UMC_BASE);
    check('a device with a mapping is marked as saved',
        deviceChoice[0].label === 'Scarlett 18i20' && deviceChoice[1].label === 'UMC204HD 192k \u00b7 saved');
    check('no devices yields no options', CustomMapping.deviceOptions(context({ devices: [] })).length === 0
        && CustomMapping.deviceOptions(null).length === 0);

    // ---- open
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING, input_key: 'custom-mapping::usb' }) });
        await CustomMapping.open();
        check('open loads the mapping endpoint', log.fetches[0].url === '/api/audio/custom-mapping' && log.fetches[0].method === 'GET');
        check('open shows the dialog through the shared modal manager',
            !elements.customMappingPanel.classList.contains('hidden') && log.modal[0][0] === 'open'
            && log.modal[0][2].initialFocus === elements.customMappingName);
        check('open fills the name', elements.customMappingName.value === 'External Upmixer');
        check('open fills the device select and marks the default device',
            (elements.customMappingDevice.innerHTML.match(/<option/g) || []).length === 2
            && /<option value="usb-Scarlett-00" selected>Scarlett 18i20<\/option>/.test(elements.customMappingDevice.innerHTML)
            && elements.customMappingDevice.disabled === false);
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
        check('no device is reported', /No audio device with input channels/.test(elements.customMappingStatus.textContent));
    }
    {
        const { elements } = harness({ ctx: context({ device: null, capture: null, devices: [] }) });
        await CustomMapping.open();
        check('without devices the select is empty and disabled',
            elements.customMappingDevice.innerHTML === '' && elements.customMappingDevice.disabled === true
            && elements.customMappingSave.disabled === true);
    }

    // ---- device choice
    {
        const { elements, log } = harness();
        await CustomMapping.open();
        CustomMapping.bind();
        elements.customMappingDevice.trigger('change', { target: { value: UMC_BASE } });
        await new Promise((resolve) => setImmediate(resolve));
        const switched = log.fetches.at(-1);
        check('choosing a device loads that device', switched.method === 'GET'
            && switched.url === `/api/audio/custom-mapping?device=${encodeURIComponent(UMC_BASE)}`);
        check('the dialog shows the chosen device',
            /UMC204HD 192k/.test(elements.customMappingSubtitle.textContent)
            && (elements.customMappingGrid.innerHTML.match(/<select/g) || []).length === 8
            && /<option value="usb-Behringer-00" selected>/.test(elements.customMappingDevice.innerHTML)
            && elements.customMappingName.value === 'Line in');
        check('the chosen device offers its own inputs only',
            !/Input 3/.test(elements.customMappingGrid.innerHTML)
            && /<option value="2" selected>Input 2<\/option>/.test(elements.customMappingGrid.innerHTML));
        check('the chosen devices saved mapping offers Delete', !elements.customMappingDelete.classList.contains('hidden'));
        await CustomMapping.save();
        const put = log.fetches.find((call) => call.method === 'PUT');
        check('save names the chosen device', put.body.device === UMC_BASE
            && put.body.name === 'Line in' && JSON.stringify(put.body.channels) === JSON.stringify({ FL: 2, FR: 1 }));
    }
    {
        const { elements, log } = harness();
        await CustomMapping.open();
        CustomMapping.bind();
        elements.customMappingDevice.trigger('change', { target: { value: UMC_BASE } });
        await new Promise((resolve) => setImmediate(resolve));
        await CustomMapping.remove();
        const removal = log.fetches.find((call) => call.method === 'DELETE');
        check('delete names the chosen device', removal.url === `/api/audio/custom-mapping?device=${encodeURIComponent(UMC_BASE)}`);
    }
    {
        const { elements, log } = harness({ contexts: {} });
        await CustomMapping.open();
        CustomMapping.bind();
        elements.customMappingDevice.trigger('change', { target: { value: 'usb-Gone-00' } });
        await new Promise((resolve) => setImmediate(resolve));
        check('a device that vanished keeps the shown device and says why',
            /Unknown audio device/.test(elements.customMappingStatus.textContent)
            && /<option value="usb-Scarlett-00" selected>/.test(elements.customMappingDevice.innerHTML)
            && (elements.customMappingGrid.innerHTML.match(/<select/g) || []).length === 8);
        check('the dialog stays usable after a failed device switch', elements.customMappingDevice.disabled === false);
        void log;
    }
    {
        // An older device response arriving after a newer choice must not win.
        const { elements } = harness();
        const resolvers = [];
        CustomMapping.init({
            fetch: (url, options = {}) => (options.method === 'GET' && /device=/.test(url)
                ? new Promise((resolve) => resolvers.push((answer) => resolve({ ok: true, status: 200, json: async () => answer })))
                : Promise.resolve({ ok: true, status: 200, json: async () => context() })),
        });
        await CustomMapping.open();
        CustomMapping.bind();
        elements.customMappingDevice.trigger('change', { target: { value: UMC_BASE } });
        await CustomMapping.open();
        resolvers[0](UMC_CONTEXT);
        await new Promise((resolve) => setImmediate(resolve));
        check('a late device response of a closed session is ignored',
            /<option value="usb-Scarlett-00" selected>/.test(elements.customMappingDevice.innerHTML)
            && elements.customMappingName.value === '');
    }

    // ---- save
    {
        const { elements, log } = harness({ ctx: context({ mapping: MAPPING }) });
        await CustomMapping.open();
        elements.customMappingName.value = ' Upmixer ';
        await CustomMapping.save();
        const put = log.fetches.find((call) => call.method === 'PUT');
        check('save sends the device and the whole mapping', JSON.stringify(put.body)
            === JSON.stringify({ device: 'usb-Scarlett-00', name: 'Upmixer', channels: { FL: 3, FR: 4, FC: 5, BR: 1 } }));
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
        check('Delete removes the mapping of the shown device, closes and refreshes',
            log.fetches.some((call) => call.method === 'DELETE'
                && call.url === '/api/audio/custom-mapping?device=usb-Scarlett-00') && log.toasts.at(-1)[0] === 'Custom mapping deleted'
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
    check('the device select is labelled', /<label for="custom-mapping-device">Device<\/label>\s*<select id="custom-mapping-device" class="url-input">/.test(html));
    for (const id of ['custom-mapping-close', 'custom-mapping-subtitle', 'custom-mapping-device', 'custom-mapping-name', 'custom-mapping-grid',
        'custom-mapping-save', 'custom-mapping-delete', 'custom-mapping-status']) {
        check(`page has #${id}`, html.includes(`id="${id}"`));
        const camel = id.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
        check(`app.js resolves #${id}`, appJs.includes(`${camel}: document.getElementById('${id}')`));
    }
    check('the module loads before app.js with a versioned URL',
        /<script src="\/static\/custom_mapping_ui\.js\?v=\d+\.\d+\.\d+"><\/script>[\s\S]*<script src="\/static\/app\.js\?v=\d+\.\d+\.\d+">/.test(html));
    const sourceSelect = fakeElement();
    const sourceSection = appJs.slice(appJs.indexOf('function renderSourceSection()'),
        appJs.indexOf('// Select-model for the Music Library selector'));
    vm.runInNewContext(sourceSection + '\nrenderSourceSection();', {
        state: {settings: {sourceMode: {}}}, elements: {settingsSourceSelect: sourceSelect},
        window: {FXRouteUsbGadget: require('../static/usb_gadget_ui.js')},
    });
    check('the Input select offers Custom Mapping after the source choices',
        sourceSelect.innerHTML.endsWith('<option value="custom-mapping">Custom Mapping…</option>'));
    check('rendering configuration choices keeps App playback selected', sourceSelect.value === 'app-playback');
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
