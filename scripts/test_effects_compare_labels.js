#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// All Banks compare tile labels: in aggregate mode both disabled selects
// show one static "All Banks · A/B" label and the chain line names the
// involved area banks; non-aggregate mode keeps the real preset pickers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const ui = require('../static/output_state.js');

// Match a top-level function from its declaration to its column-0 closing
// brace (same proven extraction as test_paired_bank_frontend.js; handles
// multi-line signatures with default params containing braces).
function extractFunction(name) {
    const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
    assert.ok(match, `function ${name} found in app.js`);
    return match[0];
}

const bank = (id, label) => ({
    id, ...(label ? { label } : {}), preset_a: 'Neutral', preset_b: null,
    active_side: null, can_a: true, can_b: false,
});

function makeContext(selectedBank, banks) {
    const config = {
        selected_bank: selectedBank,
        banks,
        topology: { roles: ['main_l', 'main_r', 'sub1'] },
        all_banks: { preset: null, preset_a: 'Neutral', preset_b: null,
            active_side: null, can_a: true, can_b: false },
    };
    const catalog = { revision: 7, active_mode: 'stereo-sub',
        modes: { 'stereo-sub': config } };
    const context = {
        state: {
            outputSystem: { catalog, busy: false },
            dsp: { presets: [{ name: 'Neutral' }], compare: {}, active_preset: '' },
        },
        elements: {
            effectsCompareRow: { style: {} },
            effectsCompareA: { innerHTML: '' },
            effectsCompareB: { innerHTML: '' },
            effectsCompareActive: { innerHTML: '', textContent: '' },
            effectsCompareChain: { textContent: '' },
            effectsCompareToggle: { textContent: '' },
        },
        effectsCompareLoadInFlight: false,
        outputSystemModule: () => ui,
        escapeHtml: (text) => String(text ?? '').replace(/&/g, '&amp;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
        renderPresetDownloadLink: (name) => String(name || ''),
        getEffectsChainLabelForPreset: () => '',
        renderEffectsBankSelector() {},
        setEffectsCompareLoadBusy() {},
        syncBankActionButtons() {},
        document: { querySelectorAll: () => [], getElementById: () => null },
        window: {},
        console,
    };
    vm.createContext(context);
    for (const name of ['getEmptyEffectsCompareState', 'normalizeEffectsCompareSelection',
        'getEffectiveEffectsCompareSide', 'getEffectsCompareState',
        'visiblePresetEntriesForBank', 'renderEffectsCompare']) {
        vm.runInContext(extractFunction(name), context);
    }
    return context;
}

const banks = {
    global: bank('global', 'Global'),
    main: bank('main', 'Main L/R'),
    sub1: bank('sub1'),
};

// Aggregate: static A/B labels plus involved bank names on the chain line.
{
    const context = makeContext('all', banks);
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(context.elements.effectsCompareA.innerHTML, '<option>All Banks · A</option>');
    assert.equal(context.elements.effectsCompareB.innerHTML, '<option>All Banks · B</option>');
    assert.equal(context.elements.effectsCompareChain.textContent, 'Main L/R · Sub 1');
    assert.match(context.elements.effectsCompareToggle.textContent, /Switch all to/);
}

// Unlabeled banks fall back to roleLabel for the chain line.
{
    const context = makeContext('all', banks);
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(context.elements.effectsCompareChain.textContent, 'Main L/R · Sub 1');
}

// No area banks (Global only): the chain line says so instead of a count.
{
    const context = makeContext('all', { global: bank('global', 'Global') });
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(context.elements.effectsCompareChain.textContent, 'No area banks');
}

// Non-aggregate: real preset pickers stay in place.
{
    const context = makeContext('main', banks);
    vm.runInContext('renderEffectsCompare()', context);
    assert.match(context.elements.effectsCompareA.innerHTML, /<option value="Neutral" selected>Neutral<\/option>/);
    assert.match(context.elements.effectsCompareB.innerHTML, /Select preset…/);
    assert.notEqual(context.elements.effectsCompareA.innerHTML, '<option>All Banks · A</option>');
}

console.log('effects compare labels tests passed');
