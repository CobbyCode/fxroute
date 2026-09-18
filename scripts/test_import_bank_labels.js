#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Import panel follows the bank model: stereo banks describe the stereo and
// per-side L/R files they accept; mono banks show a single mono field that
// routes every supported format (mono IR, REW text, preset, bundle).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ui = require('../static/output_state.js');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function extract(name) {
    const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
    assert.ok(match, name);
    return match[0];
}

const bank = (id, label, roles, channel_mode = 'stereo') => ({
    id, label, roles, channel_mode, preset: 'Neutral', preset_a: 'Neutral',
    preset_b: null, active_side: 'A', can_a: true, can_b: false,
});
const catalog = {
    revision: 7, active_mode: 'stereo-sub',
    modes: { 'stereo-sub': {
        selected_bank: 'main',
        banks: {
            global: bank('global', 'Global', ['global']),
            main: bank('main', 'Main L/R', ['main_l', 'main_r']),
            sub1: bank('sub1', 'Sub 1', ['sub1'], 'mono'),
        },
        topology: { roles: ['main_l', 'main_r', 'sub1'] },
        all_banks: {},
    } },
};
const mode = () => catalog.modes['stereo-sub'];

function trackingNode() {
    const node = {
        textContent: '', innerHTML: '', placeholder: '', accept: '', value: '', files: [],
        toggled: {},
        classList: null,
        closest() { return trackingNode(); },
    };
    node.classList = {
        toggle(key, value) { node.toggled[key] = value; },
        add() {}, remove() {}, contains() { return false; },
    };
    return node;
}

const nodes = {};
const documentStub = {
    getElementById: (id) => nodes[id] || (nodes[id] = trackingNode()),
    querySelector: (sel) => nodes[sel] || (nodes[sel] = trackingNode()),
};
const hiddenOf = (key) => !!nodes[key]?.toggled?.hidden;

const context = {
    state: { outputSystem: { catalog } },
    elements: {
        effectsRewRightText: trackingNode(),
        effectsRewLeftText: trackingNode(),
        effectsRewLeftFile: trackingNode(),
        effectsRewRightFile: trackingNode(),
        effectsImportFile: trackingNode(),
        effectsRewDualCreatePresetBtn: trackingNode(),
    },
    outputSystemModule: () => ui,
    measurementAreaFromCatalog: () => ui.measurementArea(context.state.outputSystem.catalog),
    updateEffectsImportUi: () => {},
    document: documentStub,
    showToast: (message) => { throw new Error(message); },
    console,
};
vm.createContext(context);
vm.runInContext('let _bankImportChannelMode = null;', context);
vm.runInContext(extract('renderBankImportTarget'), context);

// Stereo bank: file area visible with explicit formats, per-side L/R labels.
mode().selected_bank = 'main';
vm.runInContext('renderBankImportTarget();', context);
assert.equal(hiddenOf('effects-import-file-head'), false);
assert.equal(hiddenOf('effects-import-area'), false);
assert.equal(documentStub.getElementById('effects-import-split-title').textContent, 'Left / Right');
assert.match(documentStub.getElementById('effects-import-split-meta').textContent, /per side/);
assert.match(documentStub.getElementById('effects-import-split-meta').textContent, /\.txt/);
assert.ok(!context.elements.effectsRewLeftFile.accept.includes('.json'));
assert.match(documentStub.querySelector('#effects-rew-left-area .upload-area-text').innerHTML, /Left .*\.irs/);
assert.equal(context.elements.effectsRewLeftText.placeholder, 'Paste Left REW text');

// Mono bank: a single mono field, stereo area hidden, every format named.
mode().selected_bank = 'sub1';
vm.runInContext('renderBankImportTarget();', context);
assert.equal(hiddenOf('effects-import-file-head'), true);
assert.equal(hiddenOf('effects-import-area'), true);
assert.equal(documentStub.getElementById('effects-import-split-title').textContent, 'Mono filter');
assert.match(documentStub.getElementById('effects-import-split-meta').textContent, /Mono/);
assert.match(documentStub.getElementById('effects-import-split-meta').textContent, /preset|bundle/);
assert.ok(context.elements.effectsRewLeftFile.accept.includes('.json'));
assert.ok(context.elements.effectsRewLeftFile.accept.includes('.zip'));
assert.match(documentStub.querySelector('#effects-rew-left-area .upload-area-text').innerHTML, /\.json/);
assert.equal(context.elements.effectsRewLeftText.placeholder, 'Paste mono REW text');

// Switching modes clears a stale file selection from the other mode only;
// unrelated re-renders keep a chosen file.
context.elements.effectsRewLeftFile.value = 'stale.wav';
vm.runInContext('renderBankImportTarget();', context);
assert.equal(context.elements.effectsRewLeftFile.value, 'stale.wav');
mode().selected_bank = 'main';
vm.runInContext('renderBankImportTarget();', context);
assert.equal(context.elements.effectsRewLeftFile.value, '');
assert.equal(hiddenOf('effects-import-area'), false);

// Mono routing: one field reaches the matching endpoint with its bank.
class FakeFormData {
    constructor() { this.fields = {}; }
    append(key, value) { (this.fields[key] = this.fields[key] || []).push(value); }
    delete(key) { delete this.fields[key]; }
}
const requests = [];
const toasts = [];
context.elements.effectsRewDualPresetName = { value: 'Mono Test', focus() {} };
context.elements.effectsRewDualCreatePresetBtn = { disabled: false };
context.elements.effectsStatus = { innerHTML: '' };
context.collectEffectsExtras = () => ({});
context.fetchEffects = async () => {};
context.fetchOutputSystemCatalog = async () => {};
context.showToast = (message, kind) => { toasts.push({ message, kind }); };
context.FormData = FakeFormData;
context.File = File;
context.document = documentStub;
context.fetch = async (url, options) => {
    requests.push({ url, fields: options.body.fields });
    return { ok: true, json: async () => ({ status: 'ok', preset: { name: 'Mono Test' } }) };
};
for (const name of ['escapeHtml', 'outputSystemBankBinding', 'appendBankBindingFields',
    'requireConcreteFilterBank', 'measurementBankSumsBothInputs', 'measurementPeqParams',
    'getDualFilterFileKind', 'createDualFilterPreset']) {
    vm.runInContext(extract(name), context);
}
function dualState({ text = '', file = null }) {
    context.elements.effectsRewLeftText.value = text;
    context.elements.effectsRewRightText.value = '';
    context.elements.effectsRewLeftFile.files = file ? [file] : [];
    context.elements.effectsRewLeftFile.value = file ? file.name : '';
    context.elements.effectsRewRightFile.files = [];
    context.elements.effectsRewDualPresetName.value = 'Mono Test';
    toasts.length = 0;
}
(async () => {
    mode().selected_bank = 'sub1';
    dualState({ file: new File(['{}'], 'mono.json', { type: 'application/json' }) });
    await context.createDualFilterPreset();
    assert.equal(requests.at(-1).url, '/api/dsp/presets/import-json');
    assert.equal(requests.at(-1).fields.bank_id[0], 'sub1');
    assert.equal(requests.at(-1).fields.file[0].name, 'mono.json');
    assert.match(toasts.at(-1).message, /Imported preset: Mono Test/);

    dualState({ file: new File(['PK'], 'mono.zip', { type: 'application/zip' }) });
    await context.createDualFilterPreset();
    assert.equal(requests.at(-1).url, '/api/dsp/presets/import-bundle');
    assert.match(toasts.at(-1).message, /Imported preset bundle: Mono Test/);

    dualState({ file: new File([new Uint8Array(44)], 'mono.wav', { type: 'audio/wav' }) });
    await context.createDualFilterPreset();
    assert.equal(requests.at(-1).url, '/api/dsp/presets/create-with-ir');
    assert.match(toasts.at(-1).message, /Created mono filter preset: Mono Test/);

    dualState({ text: '20 0 0' });
    await context.createDualFilterPreset();
    assert.equal(requests.at(-1).url, '/api/dsp/presets/import-rew-peq');
    assert.equal(requests.at(-1).fields.file[0].name, 'Mono Test.txt');

    // A stereo bank keeps preset files in its dedicated file area.
    mode().selected_bank = 'main';
    const before = requests.length;
    dualState({ file: new File(['{}'], 'stereo.json', { type: 'application/json' }) });
    await context.createDualFilterPreset();
    assert.equal(requests.length, before);
    assert.match(toasts.at(-1).message, /Stereo file area/);
    console.log('import bank label and routing tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
