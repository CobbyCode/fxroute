#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tone effects (Upmixer, Loudness, Bass enhancer, Tone effect) share one
// layout: a header with the checkbox and the effect name, and one details
// block below it that holds every parameter and exists only while the
// effect is on.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const styleSource = fs.readFileSync(path.join(repoRoot, 'static', 'style.css'), 'utf8');
const Effects = require('../static/output_effects_ui.js');

const EFFECTS = [
    { key: 'upmixer', name: 'Upmixer', row: 'effects-upmixer-row', parameters: ['effects-upmixer-mode', 'effects-upmixer-fft-size', 'effects-upmixer-center', 'effects-upmixer-surround'] },
    { key: 'loudness', name: 'Loudness', row: 'effects-loudness-row', parameters: ['effects-loudness-strength', 'effects-loudness-fft-size'] },
    { key: 'bass', name: 'Bass enhancer', row: 'effects-bass-row', parameters: ['effects-bass-amount'] },
    { key: 'tone-effect', name: 'Tone effect', row: 'effects-tone-effect-row', parameters: ['effects-tone-effect-mode'] },
];

// The Tone group of the markup, split into its effect blocks.
const toneStart = indexSource.indexOf('<h4>Tone</h4>');
const tone = indexSource.slice(toneStart, indexSource.indexOf('</section>', toneStart));
const blocks = tone.split(/(?=<div class="effects-effect[ "])/).slice(1);
assert.equal(blocks.length, EFFECTS.length, 'the Tone group holds exactly the four effects');

EFFECTS.forEach((effect, position) => {
    const block = blocks[position];
    assert.ok(block.includes(`id="${effect.row}"`), `${effect.name}: effect ${position + 1} is ${effect.row}`);
    // Header: checkbox and name, nothing else.
    const header = block.match(/<label class="checkbox-row effects-effect-header">([\s\S]*?)<\/label>/);
    assert.ok(header, `${effect.name}: header label`);
    assert.equal(header[1].replace(/\s+/g, ' ').trim(), `<input type="checkbox" id="effects-${effect.key}-enabled"> ${effect.name}`,
        `${effect.name}: the header is the checkbox and the name`);
    // Details: one block, hidden until the effect is on.
    const details = block.match(new RegExp(`<div class="effects-effect-details hidden" id="effects-${effect.key}-details">`));
    assert.ok(details, `${effect.name}: details block starts hidden`);
    const detailsAt = block.indexOf(details[0]);
    const outside = block.slice(0, detailsAt).replace(header[0], '');
    assert.ok(!/<(input|select|button|option)\b/.test(outside), `${effect.name}: no parameter outside the details`);
    const inside = block.slice(detailsAt);
    const controls = [...inside.matchAll(/<(?:input|select)\b[^>]*\bid="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(controls, effect.parameters, `${effect.name}: the details hold exactly its parameters`);
    // Every parameter is a labelled inline control placed on a line of the details.
    const lines = inside.split('class="effects-effect-line"').slice(1);
    assert.ok(lines.length >= 1, `${effect.name}: parameters sit on lines`);
    const labelled = [...inside.matchAll(/<label for="([^"]+)"[^>]*class="effects-inline-control[^"]*"[^>]*>\s*<span class="effects-inline-control-label">[^<]+<\/span>/g)].map(match => match[1]);
    assert.deepEqual(labelled, effect.parameters, `${effect.name}: every parameter has a label`);
});

// Only the new pattern remains: the former per-effect layouts are gone.
for (const legacy of ['effects-extras-row-helpers effects-loudness-row', 'effects-loudness-controls', 'effects-extras-row effects-bass-row', 'effects-bass-controls',
    'effects-upmixer-controls', 'effects-upmixer-line', 'effects-loudness-strength-wrap', 'effects-loudness-fft-wrap',
    'effects-tone-effect-wrap']) {
    assert.ok(!indexSource.includes(legacy), `index.html no longer uses ${legacy}`);
}
for (const legacy of ['.effects-loudness-controls', '.effects-bass-row', '.effects-bass-controls', '.effects-upmixer-controls', '.effects-upmixer-line']) {
    assert.ok(!styleSource.includes(legacy), `style.css no longer styles ${legacy}`);
}

// Style facts: the details block is a stack of wrapping lines, hidden by the shared .hidden rule,
// and narrow cards stack labels above their controls instead of overflowing.
assert.match(styleSource, /\.hidden\s*\{\s*display:\s*none\s*!important/);
assert.match(styleSource, /\.effects-effect-details\s*\{[^}]*flex-direction:\s*column/);
assert.match(styleSource, /\.effects-effect-details\s*\{[^}]*container-type:\s*inline-size/);
assert.match(styleSource, /\.effects-effect-line\s*\{[^}]*flex-wrap:\s*wrap/);
assert.match(styleSource, /@container \(max-width: 27rem\)\s*\{\s*\.effects-effect-line \.effects-inline-control\s*\{[^}]*flex-direction:\s*column/);
// Fixed label/control grid: one label cell width and one control cell width for every line.
assert.match(styleSource, /\.effects-effect-details\s*\{[^}]*--effects-label-w:[^;]+;[^}]*--effects-control-w:/);
assert.match(styleSource, /\.effects-effect-line \.effects-inline-control-label\s*\{[^}]*flex:\s*0 0 var\(--effects-label-w\)/);
assert.match(styleSource, /\.effects-effect-line \.stepper-control\s*\{[^}]*width:\s*var\(--effects-control-w\)/);
assert.match(styleSource, /\.effects-effect-line \.stepper-control:has\(\.stepper-unit\)\s*\{[^}]*min-width:\s*var\(--effects-control-w\)/);
assert.match(styleSource, /#tab-effects \.effects-effect-line select\.url-input\s*\{[^}]*min-width:\s*var\(--effects-control-w\)/);

// Behaviour: the checkbox decides whether the details exist.
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
const elements = {
    effectsLoudnessEnabled: element(), effectsLoudnessDetails: element(),
    effectsBassEnabled: element(), effectsBassDetails: element(),
    effectsToneEffectEnabled: element(), effectsToneEffectDetails: element(),
    effectsUpmixerRow: element(), effectsUpmixerEnabled: element(), effectsUpmixerDetails: element(),
};
Effects.init({
    getState: () => ({ outputSystem: { catalog: { active_mode: 'surround', modes: { surround: { topology: { roles: ['main_l', 'main_r', 'center'] } } } } } }),
    getElements: () => elements,
    getActiveEditing: () => new Set(),
});
const hidden = (el) => el.classList.contains('hidden');
for (const [enabled, details] of [['effectsLoudnessEnabled', 'effectsLoudnessDetails'], ['effectsBassEnabled', 'effectsBassDetails'],
    ['effectsToneEffectEnabled', 'effectsToneEffectDetails'], ['effectsUpmixerEnabled', 'effectsUpmixerDetails']]) {
    Effects.updateEffectsExtrasUi();
    assert.ok(hidden(elements[details]), `${details}: hidden while off`);
    elements[enabled].checked = true;
    Effects.updateEffectsExtrasUi();
    assert.ok(!hidden(elements[details]), `${details}: shown while on`);
    for (const other of ['effectsLoudnessDetails', 'effectsBassDetails', 'effectsToneEffectDetails', 'effectsUpmixerDetails']) {
        if (other !== details && !elements[other.replace('Details', 'Enabled')].checked) assert.ok(hidden(elements[other]), `${other}: untouched`);
    }
    elements[enabled].checked = false;
    Effects.updateEffectsExtrasUi();
    assert.ok(hidden(elements[details]), `${details}: hidden again when switched off`);
}
console.log('effects tone layout frontend checks passed');
