#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Surround upmixer controls in the Tone group: visible in Surround only,
// Center control follows the routed Center speaker, Surround control does
// not, values round-trip through collect/apply as whole numbers 0..10.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const Effects = require('../static/output_effects_ui.js');
const Controller = require('../static/output_system_controller.js');

function element(extra = {}) {
    const hidden = new Set(['hidden']);
    return {
        checked: false,
        value: '',
        classList: {
            toggle(name, force) { if (force) hidden.add(name); else hidden.delete(name); },
            contains: (name) => hidden.has(name),
        },
        ...extra,
    };
}

function elementsFixture() {
    return {
        effectsUpmixerRow: element(),
        effectsUpmixerEnabled: element(),
        effectsUpmixerCenter: element({ value: '6' }),
        effectsUpmixerCenterWrap: element(),
        effectsUpmixerSurround: element({ value: '6' }),
        effectsUpmixerSurroundWrap: element(),
    };
}

function catalog(mode, roles) {
    return { active_mode: mode, modes: { [mode]: { topology: { roles } } } };
}

function setup(state, elements) {
    Effects.init({
        getState: () => state,
        getElements: () => elements,
        getActiveEditing: () => new Set(),
    });
}

const hidden = (el) => el.classList.contains('hidden');

// Markup: ids and the 0..10 whole-number range, like Loudness strength.
for (const id of ['effects-upmixer-row', 'effects-upmixer-enabled', 'effects-upmixer-center',
    'effects-upmixer-center-wrap', 'effects-upmixer-surround', 'effects-upmixer-surround-wrap']) {
    assert.ok(indexSource.includes(`id="${id}"`), `index.html must contain ${id}`);
}
assert.match(indexSource, /id="effects-upmixer-center"[^>]*min="0" max="10" step="1"/);
assert.match(indexSource, /id="effects-upmixer-surround"[^>]*min="0" max="10" step="1"/);
assert.match(indexSource, /id="effects-loudness-strength"[^>]*min="0" max="10" step="1"/);
assert.ok(indexSource.indexOf('effects-upmixer-row') < indexSource.indexOf('effects-loudness-enabled'),
    'the upmixer row leads the Tone group');
assert.match(indexSource, /id="effects-upmixer-row"[^>]*class="[^"]*\bhidden\b|class="[^"]*\bhidden\b[^"]*" id="effects-upmixer-row"/,
    'the row starts hidden until the catalog says Surround');

// Strength normalisation.
assert.equal(Effects.normalizeEffectsUpmixerStrength('7'), 7);
assert.equal(Effects.normalizeEffectsUpmixerStrength(0), 0);
assert.equal(Effects.normalizeEffectsUpmixerStrength('0'), 0);
assert.equal(Effects.normalizeEffectsUpmixerStrength(-3), 0);
assert.equal(Effects.normalizeEffectsUpmixerStrength(''), 6);
assert.equal(Effects.normalizeEffectsUpmixerStrength(null, 6), 6);
assert.equal(Effects.normalizeEffectsUpmixerStrength(11), 10);
assert.equal(Effects.normalizeEffectsUpmixerStrength(4.6), 5);
assert.equal(Effects.normalizeEffectsUpmixerStrength('x', 6), 6);
assert.equal(Effects.normalizeEffectsUpmixerStrength(undefined), 6);

// Visibility by mode and routing.
{
    const elements = elementsFixture();
    const state = { outputSystem: { catalog: null } };
    setup(state, elements);
    elements.effectsUpmixerEnabled.checked = true;

    Effects.updateEffectsUpmixerUi();
    assert.ok(hidden(elements.effectsUpmixerRow), 'no catalog: hidden');

    state.outputSystem.catalog = catalog('stereo', ['main_l', 'main_r']);
    Effects.updateEffectsUpmixerUi();
    assert.ok(hidden(elements.effectsUpmixerRow), 'Stereo: hidden');

    state.outputSystem.catalog = catalog('stereo-sub', ['main_l', 'main_r', 'sub_l', 'sub_r']);
    Effects.updateEffectsUpmixerUi();
    assert.ok(hidden(elements.effectsUpmixerRow), 'Stereo + Sub: hidden');

    state.outputSystem.catalog = catalog('surround', ['main_l', 'main_r', 'center', 'rear_l', 'rear_r', 'sub1']);
    Effects.updateEffectsUpmixerUi();
    assert.ok(!hidden(elements.effectsUpmixerRow), 'Surround: visible');
    assert.ok(!hidden(elements.effectsUpmixerCenterWrap), 'Center routed: Center control visible');
    assert.ok(!hidden(elements.effectsUpmixerSurroundWrap), 'Surround control visible');

    // Front + Rear + subs, no Center: only the Center control goes away.
    state.outputSystem.catalog = catalog('surround', ['main_l', 'main_r', 'rear_l', 'rear_r', 'sub1', 'sub2']);
    Effects.updateEffectsUpmixerUi();
    assert.ok(!hidden(elements.effectsUpmixerRow), 'no Center: upmixer stays available');
    assert.ok(hidden(elements.effectsUpmixerCenterWrap), 'no Center: Center control hidden');
    assert.ok(!hidden(elements.effectsUpmixerSurroundWrap), 'no Center: Surround control stays');

    // Disabled: the row stays, the controls fold away.
    elements.effectsUpmixerEnabled.checked = false;
    Effects.updateEffectsUpmixerUi();
    assert.ok(!hidden(elements.effectsUpmixerRow));
    assert.ok(hidden(elements.effectsUpmixerCenterWrap) && hidden(elements.effectsUpmixerSurroundWrap));
}

// collect / apply round trip.
{
    const elements = {
        ...elementsFixture(),
        effectsHeadroomGainDb: element({ value: '-3' }),
        effectsAutogainTargetDb: element({ value: '-12' }),
        effectsLoudnessStrength: element({ value: '10' }),
        effectsLoudnessFftSize: element({ value: '4096' }),
        effectsBassAmount: element({ value: '0' }),
        effectsToneEffectMode: element({ value: 'crystalizer' }),
    };
    setup({ outputSystem: { catalog: catalog('surround', ['main_l', 'main_r', 'center']) } }, elements);
    Effects.applyEffectsExtras({ upmixerEnabled: true, upmixerCenter: 8, upmixerSurround: '3' });
    assert.equal(elements.effectsUpmixerEnabled.checked, true);
    assert.equal(elements.effectsUpmixerCenter.value, 8);
    assert.equal(elements.effectsUpmixerSurround.value, 3);
    assert.ok(!hidden(elements.effectsUpmixerCenterWrap));
    const collected = Effects.collectEffectsExtras();
    assert.equal(collected.upmixerEnabled, true);
    assert.equal(collected.upmixerCenter, 8);
    assert.equal(collected.upmixerSurround, 3);
    elements.effectsUpmixerCenter.value = '99';
    elements.effectsUpmixerSurround.value = '';
    const clamped = Effects.collectEffectsExtras();
    assert.equal(clamped.upmixerCenter, 10);
    assert.equal(clamped.upmixerSurround, 6);

    // 0 is a real value for either share and survives apply/collect on its own.
    for (const [center, surround] of [[0, 5], [5, 0], [0, 0]]) {
        Effects.applyEffectsExtras({ upmixerEnabled: true, upmixerCenter: center, upmixerSurround: surround });
        assert.equal(elements.effectsUpmixerCenter.value, center);
        assert.equal(elements.effectsUpmixerSurround.value, surround);
        const zero = Effects.collectEffectsExtras();
        assert.equal(zero.upmixerEnabled, true, 'a zero strength does not switch the upmixer off');
        assert.equal(zero.upmixerCenter, center);
        assert.equal(zero.upmixerSurround, surround);
        assert.ok(!hidden(elements.effectsUpmixerSurroundWrap), 'Surround control stays visible at 0');
        assert.ok(!hidden(elements.effectsUpmixerCenterWrap), 'Center control stays visible at 0');
    }
}

// The catalog refresh re-evaluates the extras UI (mode switches).
(async () => {
    let updates = 0;
    Controller.init({
        getState: () => ({ outputSystem: { catalog: null, busy: false } }),
        ensureOutputBoxes: () => {},
        outputSystemModule: () => ({ fetchCatalog: async () => catalog('surround', ['main_l', 'main_r']) }),
        updateEffectsExtras: () => { updates += 1; },
    });
    await Controller.fetchOutputSystemCatalog(true);
    assert.equal(updates, 1, 'catalog refresh must update the extras UI once');
    console.log('upmixer UI frontend checks passed');
})();
