#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Active-side deletion must not depend on the measurement target or DSP view.
const assert = require('node:assert/strict');
require('../static/output_state.js');
const { escapeHtml } = require('../static/ui_helpers.js');
const BankUI = require('../static/output_bank_ui.js');
const Controller = require('../static/output_system_controller.js');

function element() {
    const classes = new Set(['hidden']);
    const listeners = {};
    return { disabled: false, textContent: '', dataset: {}, innerHTML: '',
        classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
            add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
            contains(name) { return classes.has(name); } },
        setAttribute() {}, focus() {}, querySelector() { return null; },
        addEventListener(type, handler) { listeners[type] = handler; },
        async fire(type, target = this) { await listeners[type]?.({ target }); },
    };
}

function context({ a = {}, b = {}, side = 'A', expert = false } = {}) {
    const neutral = { front: 'Neutral', center: 'Neutral', side: 'Neutral', rear: 'Neutral' };
    const bank = { id: 'global', label: 'Global', preset_a: typeof a === 'string' ? a : { ...neutral, ...a },
        preset_b: typeof b === 'string' ? b : { ...neutral, ...b }, active_side: side, can_a: true, can_b: true };
    bank.preset = bank[side === 'B' ? 'preset_b' : 'preset_a'];
    const state = { dsp: { available: true, presets: [] }, measurement: { globalGroup: 'front' },
        outputSystem: { expert, catalog: { revision: 7, active_mode: 'surround', modes: { surround: {
            selected_bank: 'global', banks: { global: bank }, topology: {
                roles: ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1'],
            },
        } } } } };
    const elements = Object.fromEntries(['effectsDeleteBtn', 'effectsDeletePanel', 'effectsDeleteList',
        'effectsDeleteDescription', 'effectsDeleteConfirm', 'effectsDeleteCancel'].map(name => [name, element()]));
    let inputs = [];
    let html = '';
    Object.defineProperty(elements.effectsDeleteList, 'innerHTML', {
        get: () => html,
        set(value) {
            html = value;
            inputs = [...value.matchAll(/<input\b[^>]*data-delete-index="(\d+)"[^>]*>/g)]
                .map(([tag, index]) => ({ dataset: { deleteIndex: index }, checked: /\bchecked\b/.test(tag), disabled: false }));
        },
    });
    elements.effectsDeleteList.querySelectorAll = () => inputs;
    const requests = [], events = [], toasts = [];
    const deleted = new Set();
    let closeOpenerDisabled = null;
    globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
    globalThis.FXRouteModal = { open(panel, options) { events.push('open'); this.options = options; },
        close() { events.push('close'); closeOpenerDisabled = elements.effectsDeleteBtn.disabled; } };
    BankUI.init({ getState: () => state, getElements: () => elements, escapeHtml,
        measurementArea: () => ({ group: 'front', available: true }),
        showToast: (message, type) => toasts.push([message, type]),
        confirmDialog() { throw new Error('Browser confirmation must not be used'); },
        fetchEffects: async () => { events.push('effects'); },
        refreshCatalog: async () => {
            events.push('catalog');
            for (const selection of [bank.preset, bank.preset_a, bank.preset_b]) {
                if (selection && typeof selection === 'object') {
                    for (const group of Object.keys(selection)) if (deleted.has(selection[group])) selection[group] = 'Neutral';
                }
            }
        },
    });
    globalThis.fetch = async (url, options) => {
        assert.equal(url, '/api/dsp/presets/delete');
        const body = JSON.parse(options.body);
        requests.push(body);
        deleted.add(body.preset_name);
        return { ok: true, json: async () => ({ status: 'ok' }) };
    };
    BankUI.wireBankUi();
    return { state, bank, elements, requests, events, toasts, deleted, closeOpenerDisabled: () => closeOpenerDisabled, inputs: () => inputs,
        open: () => BankUI.deleteEffectsPreset(),
        confirm: () => elements.effectsDeleteConfirm.fire('click'),
        cancel: () => elements.effectsDeleteCancel.fire('click'),
        async select(groups) {
            for (const input of inputs) input.checked = groups.includes(Number(input.dataset.deleteIndex));
            await elements.effectsDeleteList.fire('change');
        },
    };
}

const realFetch = globalThis.fetch;
async function run() {
    // Controller busy release must restore Delete after bank/output mutations.
    {
        const c = context({ a: { rear: 'Rear A' }, expert: true });
        Controller.init({ getState: () => c.state,
            renderBankSelector: () => {}, renderCompare: () => BankUI.syncEffectsDeleteButton(),
            syncCompareBusy: () => BankUI.setEffectsCompareLoadBusy(false) });
        globalThis.fetch = async url => ({ ok: true, json: async () => url.endsWith('/apply')
            ? { revision: 8, live_applied: true } : c.state.outputSystem.catalog });
        await Controller.applyOutputSystemMutation('select_bank', { mode: 'surround', bank_id: 'global' }, false);
        assert.equal(c.state.outputSystem.busy, false);
        assert.equal(c.elements.effectsDeleteBtn.disabled, false, 'Controller completion re-enables Delete');
    }
    // Regression: neutral Front must not disable deleting the active Rear.
    for (const expert of [false, true]) {
        const c = context({ a: { rear: 'Rear A' }, b: { front: 'Front B' }, expert });
        BankUI.syncEffectsDeleteButton();
        assert.equal(c.elements.effectsDeleteBtn.disabled, false, 'Rear is deletable with neutral Front');
        await c.open();
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), false);
        assert.match(c.elements.effectsDeleteList.innerHTML, /Rear/);
        assert.match(c.elements.effectsDeleteList.innerHTML, /Rear A/);
        assert.doesNotMatch(c.elements.effectsDeleteList.innerHTML, /Front B|<input/);
        assert.equal(c.elements.effectsDeleteConfirm.textContent, 'Delete');
        assert.deepEqual(c.requests, [], 'opening does not delete');
        await c.confirm();
        assert.deepEqual(c.requests, [{ preset_name: 'Rear A', bank_mode: 'surround', bank_id: 'global' }]);
        assert.equal(c.bank.preset_b.front, 'Front B', 'inactive-side preset survives');
        assert.equal(c.elements.effectsDeleteBtn.disabled, true);
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), true);
        assert.deepEqual(c.events, ['open', 'effects', 'catalog', 'close']);
    }

    // Every group can be selected alone from all four active presets in B.
    for (const [index, name] of [[0, 'Front B'], [1, 'Center B'], [2, 'Side B'], [3, 'Rear B']]) {
        const c = context({ a: { rear: 'Rear A' }, side: 'B',
            b: { front: 'Front B', center: 'Center B', side: 'Side B', rear: 'Rear B' } });
        await c.open();
        assert.equal(c.inputs().length, 4);
        assert.ok(c.inputs().every(input => input.checked));
        assert.equal(c.elements.effectsDeleteConfirm.textContent, 'Delete (4)');
        assert.doesNotMatch(c.elements.effectsDeleteList.innerHTML, /Rear A/);
        await c.select([]);
        assert.equal(c.elements.effectsDeleteConfirm.textContent, 'Delete (0)');
        assert.equal(c.elements.effectsDeleteConfirm.disabled, true);
        await c.confirm();
        assert.deepEqual(c.requests, []);
        await c.select([index]);
        assert.equal(c.elements.effectsDeleteConfirm.textContent, 'Delete (1)');
        assert.equal(c.elements.effectsDeleteConfirm.disabled, false);
        await c.confirm();
        assert.deepEqual(c.requests.map(item => item.preset_name), [name]);
        assert.equal(Object.values(c.bank.preset_b).filter(value => value !== 'Neutral').length, 3);
        assert.equal(c.bank.preset_a.rear, 'Rear A');
        assert.equal(c.elements.effectsDeleteBtn.disabled, false);
    }

    // All, subset and no selections remain independent of the measured group.
    for (const [indices, expected] of [[[0, 1, 2, 3], ['Front', 'Center', 'Side', 'Rear']], [[1, 3], ['Center', 'Rear']]]) {
        const c = context({ a: { front: 'Front', center: 'Center', side: 'Side', rear: 'Rear' } });
        await c.open();
        await c.select(indices);
        await c.confirm();
        assert.deepEqual(c.requests.map(item => item.preset_name), expected);
        assert.equal(c.elements.effectsDeleteBtn.disabled, expected.length === 4);
        if (expected.length < 4) assert.equal(c.closeOpenerDisabled(), false, 'Enable opener before modal focus restoration');
    }

    // Cancel, Escape, Direct, Neutral, aggregates and unavailable DSP never delete.
    for (const escape of [false, true]) {
        const c = context({ a: { rear: 'Rear' } });
        await c.open();
        if (escape) globalThis.FXRouteModal.options.onEscape(); else await c.cancel();
        await c.confirm();
        assert.deepEqual(c.requests, []);
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), true);
    }
    for (const a of ['Direct', 'Neutral', {}]) {
        const c = context({ a, b: { rear: 'Inactive' } });
        BankUI.syncEffectsDeleteButton();
        assert.equal(c.elements.effectsDeleteBtn.disabled, true);
        await c.open();
        assert.deepEqual(c.events, []);
    }
    for (const change of [c => { c.state.dsp.available = false; },
        c => { c.state.outputSystem.catalog.modes.surround.selected_bank = 'all'; },
        c => { c.bank.active_side = null; }]) {
        const c = context({ a: { rear: 'Rear' } });
        change(c);
        BankUI.syncEffectsDeleteButton();
        assert.equal(c.elements.effectsDeleteBtn.disabled, true);
        await c.open();
        assert.deepEqual(c.events, []);
    }

    // Escaping names prevents markup injection.
    {
        const c = context({ a: { rear: '<img src=x> "Rear"' } });
        await c.open();
        assert.match(c.elements.effectsDeleteList.innerHTML, /&lt;img src=x&gt; &quot;Rear&quot;/);
        await c.cancel();
    }
    // A side, bank, candidate or routing change requires reviewing a fresh dialog.
    for (const change of [c => { c.state.outputSystem.catalog.active_mode = 'stereo'; },
        c => { c.state.outputSystem.catalog.modes.surround.selected_bank = 'all'; },
        c => { c.bank.active_side = 'B'; },
        c => { c.bank.preset_a.rear = 'New rear'; },
        c => { c.state.outputSystem.catalog.modes.surround.topology.roles = ['main_l', 'main_r']; }]) {
        const c = context({ a: { rear: 'Rear' } });
        await c.open();
        change(c);
        await c.confirm();
        assert.deepEqual(c.requests, [], 'a context change requires reopening the dialog');
        assert.match(c.toasts.at(-1)[0], /changed/i);
    }

    // In-flight submission is locked, including cancellation.
    {
        const c = context({ a: { front: 'Front', rear: 'Rear' } });
        await c.open();
        let release;
        const pending = new Promise(resolve => { release = resolve; });
        let count = 0;
        globalThis.fetch = async () => {
            count++;
            await pending;
            return { ok: false, json: async () => ({ detail: 'Delete refused' }) };
        };
        const first = c.confirm();
        assert.equal(c.elements.effectsDeleteConfirm.disabled, true);
        assert.equal(c.elements.effectsDeleteCancel.disabled, true);
        await c.confirm();
        await c.cancel();
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), false);
        release();
        await first;
        assert.equal(count, 1);
        assert.match(c.toasts.at(-1)[0], /Delete refused/);
        assert.ok(c.events.includes('catalog'));
        assert.equal(c.elements.effectsDeleteBtn.disabled, false);
    }
    // A failed second delete leaves the first deleted, refreshes, and stops the batch.
    {
        const c = context({ a: { front: 'Front', center: 'Center', rear: 'Rear' } });
        await c.open();
        globalThis.fetch = async (url, options) => {
            const body = JSON.parse(options.body);
            c.requests.push(body);
            const ok = c.requests.length === 1;
            if (ok) c.deleted.add(body.preset_name);
            return { ok, json: async () => ok ? {} : { detail: 'Center refused' } };
        };
        await c.confirm();
        assert.deepEqual(c.requests.map(item => item.preset_name), ['Front', 'Center']);
        assert.equal(c.bank.preset_a.front, 'Neutral');
        assert.equal(c.bank.preset_a.center, 'Center');
        assert.equal(c.bank.preset_a.rear, 'Rear');
        assert.equal(c.elements.effectsDeleteBtn.disabled, false);
        assert.match(c.toasts.at(-1)[0], /Center refused/);
        assert.deepEqual(c.events, ['open', 'effects', 'catalog', 'close']);
    }
    // The modal stays locked until effects and catalog refresh have both completed.
    {
        const c = context({ a: { rear: 'Rear' } });
        let finishEffects, finishCatalog;
        const effects = new Promise(resolve => { finishEffects = resolve; });
        const catalog = new Promise(resolve => { finishCatalog = resolve; });
        BankUI.init({ fetchEffects: () => effects, refreshCatalog: () => catalog });
        await c.open();
        const pending = c.confirm();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(c.elements.effectsDeleteCancel.disabled, true);
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), false);
        finishEffects();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), false);
        assert.equal(c.elements.effectsDeleteConfirm.disabled, true);
        finishCatalog();
        await pending;
        assert.equal(c.elements.effectsDeletePanel.classList.contains('hidden'), true);
    }
    console.log('preset delete dialog tests passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { globalThis.fetch = realFetch; });
