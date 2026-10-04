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

// Surround offers no Global bank; All Banks and area banks stay.
const surroundConfig = { topology: { mode: 'surround', roles: ['main_l', 'main_r', 'rear_l', 'rear_r'] },
    banks: { main: { label: 'Front L/R' }, rear: { label: 'Rear L/R' } } };
assert.deepEqual(ui.bankOptions(surroundConfig).map(entry => entry.id), ['all', 'main', 'rear']);
assert.equal(ui.bankSelectorVisible({ topology: { mode: 'surround', roles: ['main_l', 'main_r'] } }), true);
assert.equal(ui.bankSelectorVisible({ topology: { mode: 'stereo', roles: ['main_l', 'main_r'] } }), false);
assert.deepEqual(ui.bankOptions({ topology: { mode: 'stereo-sub', roles: ['main_l'] }, banks: {} }).map(entry => entry.id),
    ['global', 'all']);

// Speaker groups reuse the sub link scheme: linked groups share the crossover.
assert.deepEqual(ui.surroundGroups({ roles: ['left_low', 'right_low', 'center', 'rear_l', 'rear_r', 'sub1'] }),
    ['front', 'center', 'rear']);
const bass = { frequency_hz: 80, family: 'linkwitz-riley', slope_db_oct: 24, main_highpass_enabled: true,
    sub_link: false, sub_filters: { rear: { frequency_hz: 120, family: 'butterworth', slope_db_oct: 12, highpass_enabled: false } } };
assert.deepEqual(ui.bassCrossoverForGroup(bass, 'rear'),
    { family: 'butterworth', slope_db_oct: 12, frequency_hz: 120, highpass_enabled: false });
assert.deepEqual(ui.bassCrossoverForGroup(bass, 'front'),
    { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80, highpass_enabled: true });
assert.equal(ui.bassCrossoverForGroup({ ...bass, sub_link: true }, 'rear').frequency_hz, 80);

function surroundCatalog(bassManagement) {
    return { active_mode: 'surround', revision: 3, capabilities: { filter_families: { 'linkwitz-riley': [12, 24], butterworth: [6, 12] } },
        modes: { surround: { topology: { mode: 'surround', sub_roles: ['sub1', 'sub2'], sub_mode: 'dual-mono',
            roles: ['main_l', 'main_r', 'rear_l', 'rear_r', 'sub1', 'sub2'] },
        processing: { sub1: {}, sub2: {} }, bass_management: bassManagement } } };
}
const groupView = ui.subwooferView(surroundCatalog(bass));
assert.equal(groupView.crossover.surround, true);
assert.deepEqual(groupView.crossover.groups.map(group => [group.id, group.name, group.frequency_hz, group.highpass_enabled]),
    [['front', 'Front L/R', 80, true], ['rear', 'Rear L/R', 120, false]]);

// Tile: unlinked groups show one tab per configured group and save only the served group.
if (typeof globalThis.window === 'undefined') globalThis.window = {};
globalThis.window.requestAnimationFrame = (fn) => { fn(); return 1; };
globalThis.window.cancelAnimationFrame = () => {};
globalThis.window.setTimeout = setTimeout;
globalThis.window.clearTimeout = clearTimeout;
const SubwooferUI = require('../static/subwoofer_ui.js');
function classList() {
    const set = new Set();
    return { add: c => set.add(c), remove: c => set.delete(c), contains: c => set.has(c), has: c => set.has(c),
        toggle: (c, force) => (force ?? !set.has(c)) ? set.add(c) : set.delete(c) };
}
function el(value = '', extra = {}) {
    const listeners = {};
    return { value, textContent: '', innerHTML: '', checked: false, dataset: {}, classList: classList(),
        setAttribute() {}, addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        emit(type) { for (const fn of listeners[type] || []) fn({ target: this }); }, ...extra };
}
const groupTabs = ['front', 'center', 'side', 'rear'].map(group => el('', { dataset: { subGroup: group } }));
const elements = {
    effectsSubwooferCard: el(), effectsSubwooferSharedCrossover: el(), effectsSubwooferLeftCrossover: el(),
    effectsSubwooferRightCrossover: el(), effectsSubwooferGlobalLabel: el(), effectsSubwooferTabRow: el(),
    effectsSubwooferSideTabs: el(), effectsSubwooferTabLeft: el(), effectsSubwooferTabRight: el(),
    effectsSubwooferTabBoth: el(), effectsSubwooferGroupTabs: groupTabs, effectsSubwooferLinkWrap: el(),
    effectsSubwooferLinkText: el(), effectsSubwooferLink: el('', { checked: false }),
    effectsSubwooferMainHighpassLabel: el(), effectsSubwooferLeftMainHighpassLabel: el(),
    effectsSubwooferFrequencyNumber: el('80'), effectsSubwooferFamily: el('linkwitz-riley'), effectsSubwooferSlope: el('24'),
    effectsSubwooferLeftFrequency: el('80'), effectsSubwooferLeftFamily: el('linkwitz-riley'), effectsSubwooferLeftSlope: el('24'),
    effectsSubwooferRightFrequency: el('80'), effectsSubwooferRightFamily: el('linkwitz-riley'), effectsSubwooferRightSlope: el('24'),
    effectsSubwooferMainHighpass: el('on'), effectsSubwooferLeftMainHighpass: el('on'), effectsSubwooferRightMainHighpass: el('on'),
    effectsSubwooferRouting: el(), effectsSubwooferModeBadge: el(), effectsSubwooferLevel: el('0'), effectsSubwooferDelay: el('0'),
    effectsSubwooferPolarity: el('normal'), effectsSubwooferSub2Level: el('0'), effectsSubwooferSub2Delay: el('0'),
    effectsSubwooferSub2Polarity: el('normal'), effectsSubwooferSub2Fields: [], effectsSubwooferFeedback: el(),
};
const state = { outputSystem: { catalog: surroundCatalog(bass) } };
const mutations = [];
SubwooferUI.init({ getState: () => state, getElements: () => elements, getActiveEditing: () => new Set(),
    applyMutation: async (kind, settings) => { mutations.push([kind, settings]); return { ok: true }; } });
SubwooferUI.setSubwooferSelectedGroup('rear');
SubwooferUI.renderSubwooferPanel();
assert.equal(elements.effectsSubwooferTabRow.classList.has('hidden'), false);
assert.deepEqual(groupTabs.map(tab => tab.classList.has('hidden')), [false, true, true, false]);
assert.equal(groupTabs[3].classList.has('is-active'), true);
assert.equal(elements.effectsSubwooferSharedCrossover.classList.has('hidden'), true);
assert.equal(elements.effectsSubwooferLeftCrossover.classList.has('hidden'), false);
assert.equal(elements.effectsSubwooferGlobalLabel.textContent, 'Rear L/R · Crossover');
assert.equal(elements.effectsSubwooferLeftFrequency.value, '120');
assert.equal(elements.effectsSubwooferLeftMainHighpass.value, 'off');
assert.equal(elements.effectsSubwooferLinkText.textContent, 'Link groups');
assert.match(elements.effectsSubwooferRouting.textContent, /Rear 120 Hz BW12 HPF off/);
elements.effectsSubwooferLeftFrequency.value = '110';
assert.deepEqual(SubwooferUI.collectSurroundBassDraft().sub_filters,
    { rear: { family: 'butterworth', slope_db_oct: 12, frequency_hz: 110, highpass_enabled: false } });

// Linked groups show one shared block and mirror it into every configured group.
state.outputSystem.catalog = surroundCatalog({ ...bass, sub_link: true });
elements.effectsSubwooferLink.checked = true;
SubwooferUI.renderSubwooferPanel();
assert.equal(elements.effectsSubwooferSharedCrossover.classList.has('hidden'), false);
assert.deepEqual(groupTabs.map(tab => tab.classList.has('hidden')), [true, true, true, true]);
assert.equal(elements.effectsSubwooferTabBoth.textContent, 'All speakers');
assert.equal(elements.effectsSubwooferMainHighpassLabel.textContent, 'Speaker highpass');
const linked = SubwooferUI.collectSurroundBassDraft();
assert.equal(linked.sub_link, true);
assert.deepEqual(Object.keys(linked.sub_filters), ['front', 'rear']);
assert.deepEqual(linked.sub_filters.rear, { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80, highpass_enabled: true });
(async () => {
    await SubwooferUI.saveSubwooferDebounced(0);
    assert.equal(mutations.length, 1);
    assert.equal(mutations[0][0], 'set_subwoofers');
    assert.deepEqual(Object.keys(mutations[0][1].sub_filters), ['front', 'rear']);
    assert.deepEqual(Object.keys(mutations[0][1].processing), ['sub1', 'sub2']);
    console.log('Surround UI tests passed');
})().catch((error) => { console.error(error); process.exit(1); });
