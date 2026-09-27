#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the output effects module: effects fetch/render,
// output PEQ band model and editor, preset creation/switching and effects
// extras incl. persistence live in static/output_effects_ui.js; the former
// compare/combine/bank/import shims are gone from app.js and their call
// sites point at window.FXRouteBankUI (owner) directly.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const bankSource = fs.readFileSync(path.join(repoRoot, 'static', 'output_bank_ui.js'), 'utf8');
const subSource = fs.readFileSync(path.join(repoRoot, 'static', 'subwoofer_ui.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const Effects = require('../static/output_effects_ui.js');

const MOVED = [
    'updateEffectsPeqDisclosureLabel', 'formatPeqSummaryBand', 'updateEffectsPeqSummary',
    'clearEffectsPeqStatusOnCollapse', 'setupEffectsActions',
    'fetchEffects', 'defaultPeqBand', 'isPeqGainBand', 'isPeqDelayBand', 'getPeqBandFallback',
    'normalizePeqEqMode', 'getDefaultPeqDraft', 'resetPeqDraft', 'addPeqBandPair', 'removePeqBand',
    'ensurePeqBandExists', 'getOtherPeqSide', 'getPeqLinkedSpecialType', 'getPeqBandPair',
    'syncLinkedPeqSpecialBand', 'normalizeLinkedPeqSpecialBands', 'updatePeqBand',
    'syncLinkedPeqSpecialBandValueInDom', 'renderPeqBandColumn', 'renderPeqBands',
    'readPeqNumberInput', 'collectPeqBandsFromDom', 'validatePeqBands', 'getPeqGainTotal',
    'createPeqPreset', 'renderEffects', 'switchEffectsPreset',
    'normalizeEffectsHeadroomGainDb', 'normalizeEffectsAutogainTargetDb',
    'normalizeEffectsLoudnessFftSize', 'normalizeEffectsLoudnessStrength',
    'normalizeEffectsToneEffectMode', 'applyEffectsExtras', 'updateEffectsExtrasUi',
    'loadSavedEffectsExtras', 'describeEffectsExtras', 'saveEffectsExtrasDebounced',
    'collectEffectsExtras', 'buildEffectsExtrasSaveBody', '_doSaveEffectsExtras',
    'setEffectsExtrasFeedback',
];

// Former thin shims: removed from app.js, real code already lives in
// output_bank_ui.js (or subwoofer_ui.js for the subwoofer wiring entry).
const DROPPED_SHIMS = [
    'normalizeEffectsCompareSelection', 'resolveEffectsCompareState', 'saveEffectsCompareState',
    'getDefaultEffectsCombineDraft', 'normalizeEffectsCombineDraft', 'setEffectsImportPanelOpen',
    'outputSystemBankBinding', 'outputSystemCombineBank', 'visiblePresetEntriesForBank',
    'visiblePresetNamesForBank', 'appendBankBindingFields', 'bankBindingJson',
    'measurementPeqParams', 'requireConcreteFilterBank', 'renderEffectsBankSelector',
    'syncBankActionButtons', 'renderBankImportTarget',
    'readTextFile', 'getDualFilterFileKind', 'populateDualFilterTextareaFromFile',
    'createDualFilterPreset', 'wireSubwooferControls', 'wireBankUi',
    'importRewPeqPreset', 'getEmptyEffectsCompareState', 'getEffectsCompareState',
    'getEffectiveEffectsCompareSide', 'setEffectsCompareLoadBusy', 'getEffectsChainLabelForPreset',
    'getCompactDisplayName', 'renderPresetDownloadLink', 'renderEffectsCompare',
    'getEffectsCombineValidationState', 'renderEffectsCombine', 'createCombinedEffectsPreset',
    'loadEffectsComparePreset', 'handleEffectsCompareSelectionChange',
    'getEffectsCompareToggleTarget', 'toggleComparePreset', 'setupEffectsCompareActions',
    'detectEffectsImportType', 'updateEffectsImportUi', 'handleEffectsImportFileChange',
    'submitEffectsImport', 'importEffectsPresetJson', 'importEffectsPresetBundle',
    'createConvolverPreset', 'deleteEffectsPreset',
];

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('output_effects_ui.js?v=') < indexSource.indexOf('app.js?v='),
    'output_effects_ui.js must load before app.js',
);
assert.match(indexSource, /output_effects_ui\.js\?v=\d+\.\d+\.\d+/);
for (const name of MOVED) {
    assert.equal(typeof Effects[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in output_effects_ui.js`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$])${name}\\(`),
        `no bare app.js call: ${name} goes through EffectsUI.`,
    );
}
for (const name of DROPPED_SHIMS) {
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `shim removed from app.js: ${name}`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$])${name}\\(`),
        `no bare app.js call: ${name} goes through its owner module`,
    );
    if (name === 'wireSubwooferControls') {
        assert.match(subSource, new RegExp(`function ${name}\\(`), 'subwoofer_ui.js owns the wiring');
    } else {
        assert.match(bankSource, new RegExp(`function ${name}\\(`), `output_bank_ui.js owns ${name}`);
    }
}

// app.js wiring reaches the module through the EffectsUI alias and dropped
// shims are rewired to the owning modules.
for (const snippet of [
    'const EffectsUI = window.FXRouteEffectsUI || {};',
    'window.FXRouteEffectsUI?.init({',
    'EffectsUI.setupEffectsActions()',
    'EffectsUI.fetchEffects()',
    'EffectsUI.renderEffects()',
    'EffectsUI.collectEffectsExtras()',
    'isPeqCreateInFlight: () => peqCreateInFlight',
    'getActiveEditing: () => _activeEditing',
    'window.FXRouteBankUI.appendBankBindingFields(',
    'window.FXRouteBankUI.renderEffectsBankSelector(',
    'window.FXRouteBankUI.requireConcreteFilterBank(',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}
// The moved setupEffectsActions must call the bank/subwoofer owners directly.
const effectsSource = fs.readFileSync(path.join(repoRoot, 'static', 'output_effects_ui.js'), 'utf8');
assert.match(effectsSource, /root\.FXRouteBankUI\.wireBankUi\(/, 'module wires bank UI via owner');
assert.match(effectsSource, /root\.FXRouteSubwooferUI\.wireSubwooferControls\(/, 'module wires subwoofer via owner');
assert.match(effectsSource, /deps\.getState\(\)/, 'state goes through injected getter');
assert.match(effectsSource, /deps\.getElements\(\)/, 'elements go through injected getter');
assert.doesNotMatch(effectsSource, /(?<![\w.$])state\./, 'no raw app state access');
assert.doesNotMatch(effectsSource, /(?<![\w.$])elements\./, 'no raw app elements access');

// Pure helper contracts stay intact.
assert.equal(Effects.normalizeEffectsHeadroomGainDb(0), 0);
assert.equal(Effects.normalizeEffectsHeadroomGainDb('-4'), -4);
assert.equal(Effects.normalizeEffectsHeadroomGainDb(5), -3);
assert.equal(Effects.normalizeEffectsHeadroomGainDb('x'), -3);
assert.equal(Effects.normalizeEffectsAutogainTargetDb(-15), -15);
assert.equal(Effects.normalizeEffectsAutogainTargetDb(0), -12);
assert.equal(Effects.normalizeEffectsLoudnessFftSize(4096), 4096);
assert.equal(Effects.normalizeEffectsLoudnessFftSize(3000), 4096);
assert.equal(Effects.normalizeEffectsLoudnessStrength('med'), 7);
assert.equal(Effects.normalizeEffectsLoudnessStrength('999'), 10);
assert.equal(Effects.normalizeEffectsLoudnessStrength(3.6), 4);
assert.equal(Effects.normalizeEffectsToneEffectMode('Maximizer'), 'maximizer');
assert.equal(Effects.normalizeEffectsToneEffectMode('bogus'), 'crystalizer');
assert.equal(Effects.normalizePeqEqMode('fir'), 'FIR');
assert.equal(Effects.normalizePeqEqMode('x'), 'IIR');
assert.deepEqual(Effects.defaultPeqBand(), {
    filterType: 'bell', frequencyHz: 1000, gainDb: 0, q: 1, delayMs: 0,
});
const draft = Effects.getDefaultPeqDraft();
assert.equal(draft.presetName, '');
assert.equal(draft.eqMode, 'IIR');
assert.equal(draft.loadAfterCreate, false);
assert.deepEqual(draft.leftBands, [Effects.defaultPeqBand()]);
assert.equal(Effects.isPeqGainBand({ filterType: 'GAIN' }), true);
assert.equal(Effects.isPeqDelayBand({ filterType: 'bell' }), false);
assert.equal(Effects.getPeqBandFallback('frequencyHz', {}), 1000);
assert.equal(Effects.getPeqBandFallback('q', { q: 'x' }), 1);
assert.equal(Effects.validatePeqBands('Left', [Effects.defaultPeqBand()]), null);
assert.match(
    Effects.validatePeqBands('Left', [{ ...Effects.defaultPeqBand(), frequencyHz: 5 }]),
    /frequency must be between 20 and 20000 Hz/,
);
assert.match(
    Effects.validatePeqBands('Right', [{ filterType: 'delay', delayMs: 999, frequencyHz: 1000, gainDb: 0, q: 1 }]),
    /delay must be between 0 and 500 ms/,
);
assert.equal(
    Effects.getPeqGainTotal([{ filterType: 'gain', gainDb: 3 }, { filterType: 'gain', gainDb: 2, enabled: false }, { filterType: 'bell', gainDb: 5 }]),
    3,
);
assert.equal(
    Effects.describeEffectsExtras({
        limiterEnabled: false, headroomEnabled: false, autogainEnabled: false,
        loudnessEnabled: false, toneEffectEnabled: false,
    }),
    'Limiter OFF • Headroom OFF • Autogain OFF • Loudness OFF • Tone OFF',
);
// The extras API merges only explicit fields; loudnessEnabled is the
// transition signal and is dropped when it matches the known server state.
const bodyDropped = Effects.buildEffectsExtrasSaveBody(
    { loudnessEnabled: true, limiterEnabled: false },
    { loudness: { enabled: true } },
);
assert.equal('loudnessEnabled' in bodyDropped, false);
assert.equal(bodyDropped.limiterEnabled, false);
const bodyKept = Effects.buildEffectsExtrasSaveBody(
    { loudnessEnabled: true },
    { loudness: { enabled: false } },
);
assert.equal(bodyKept.loudnessEnabled, true);
assert.equal('loudnessEnabled' in Effects.buildEffectsExtrasSaveBody({ loudnessEnabled: true }, null), true);

// PEQ Q shares the backend bound (LSP Q port, 0.1..100): an imported REW
// band with Q above 20 stays editable, anything beyond the bound does not.
const bellWithQ = (q) => [{ filterType: 'bell', frequencyHz: 688, gainDb: 7, q }];
assert.equal(Effects.validatePeqBands('Left', bellWithQ(45.76)), null);
assert.equal(Effects.validatePeqBands('Left', bellWithQ(100)), null);
assert.equal(Effects.validatePeqBands('Left', bellWithQ(0.707)), null);
assert.match(Effects.validatePeqBands('Left', bellWithQ(100.5)), /Q must be between 0\.1 and 100/);
assert.match(Effects.validatePeqBands('Left', bellWithQ(0.09)), /Q must be between 0\.1 and 100/);

console.log('output effects frontend: ok');
