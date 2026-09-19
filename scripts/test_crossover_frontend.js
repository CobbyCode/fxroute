#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Crossover = require('../static/crossover.js');
const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

const CAPS = {
    filter_families: { 'linkwitz-riley': [12, 24, 36, 48, 60, 72], butterworth: [6, 12, 24], bessel: [6, 12] },
};

assert.deepEqual(
    Crossover.orderedWays({ right_high: 1, left_low: 1, right_low: 1, left_high: 1, sub1: 1 }),
    ['left_low', 'left_high', 'right_low', 'right_high']);
assert.deepEqual(
    Crossover.orderedWays({ left_mid: 1, left_low_mid: 1, left_low: 1, left_high: 1 }),
    ['left_low', 'left_low_mid', 'left_mid', 'left_high']);

assert.deepEqual(Crossover.applicableFilters('left_low'), ['lowpass']);
assert.deepEqual(Crossover.applicableFilters('right_high'), ['highpass']);
assert.deepEqual(Crossover.applicableFilters('left_mid'), ['highpass', 'lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_low_mid'), ['highpass', 'lowpass']);

// Sub-owned Low high-pass: with routed subs and Main highpass on, the Low
// way additionally shows the sub crossover (read-only, owned by the Sub tile).
const bass80 = { frequency_hz: 80, main_highpass_enabled: true };
assert.deepEqual(Crossover.bassHighpass(bass80, ['sub1']), {
    family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80 });
assert.equal(Crossover.bassHighpass(bass80, []), null);
assert.equal(Crossover.bassHighpass({ frequency_hz: 80, main_highpass_enabled: false }, ['sub1']), null);
assert.equal(Crossover.bassHighpass({ frequency_hz: 80 }, ['sub1']), null);
assert.equal(Crossover.bassHighpass(bass80, ['sub1']).frequency_hz, 80);
assert.deepEqual(Crossover.derivedHighpassForRole('left_low', bass80, ['sub_l']), {
    family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80 });
assert.equal(Crossover.derivedHighpassForRole('right_low', bass80, []), null);
assert.equal(Crossover.derivedHighpassForRole('left_mid', bass80, ['sub_l']), null);
assert.equal(Crossover.derivedHighpassForRole('left_high', bass80, ['sub_l']), null);
assert.deepEqual(Crossover.applicableFilters('left_low', { bass: bass80, subRoles: ['sub1'] }),
    ['highpass', 'lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_low', { bass: bass80, subRoles: [] }), ['lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_mid', { bass: bass80, subRoles: ['sub1'] }),
    ['highpass', 'lowpass']);

assert.deepEqual(Crossover.slopesForFamily('linkwitz-riley', CAPS), [12, 24, 36, 48, 60, 72]);
assert.deepEqual(Crossover.slopesForFamily('nope', CAPS), []);

const two = Crossover.starterValues(2);
assert.equal(two.left_low.lowpass.frequency_hz, 2000);
assert.equal(two.left_low.lowpass.family, 'linkwitz-riley');
assert.equal(two.left_low.lowpass.slope_db_oct, 24);
assert.equal(two.left_low.highpass, undefined);
assert.equal(two.right_high.highpass.frequency_hz, 2000);
assert.equal(Object.keys(two).length, 4);

const three = Crossover.starterValues(3);
assert.equal(three.left_low.lowpass.frequency_hz, 300);
assert.equal(three.left_mid.highpass.frequency_hz, 300);
assert.equal(three.left_mid.lowpass.frequency_hz, 2500);
assert.equal(three.left_high.highpass.frequency_hz, 2500);

const four = Crossover.starterValues(4);
assert.equal(four.left_low.lowpass.frequency_hz, 200);
assert.equal(four.left_low_mid.highpass.frequency_hz, 200);
assert.equal(four.left_low_mid.lowpass.frequency_hz, 800);
assert.equal(four.left_mid.highpass.frequency_hz, 800);
assert.equal(four.left_mid.lowpass.frequency_hz, 3000);
assert.equal(four.left_high.highpass.frequency_hz, 3000);

assert.throws(() => Crossover.starterValues(5), /2-way|3-way|4-way/);

const processing = {
    left_low: { highpass: null, lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 310 } },
    left_mid: { highpass: null, lowpass: null },
    left_high: { highpass: { family: 'butterworth', slope_db_oct: 12, frequency_hz: 2500 }, lowpass: null },
    right_low: { highpass: null, lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 } },
    right_mid: { highpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 },
                 lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 2500 } },
    right_high: { highpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 2500 }, lowpass: null },
};
assert.deepEqual(Crossover.missingStarterRoles(processing, three), ['left_mid']);

assert.equal(Crossover.mirrorRole('left_low'), 'right_low');
assert.equal(Crossover.mirrorRole('right_high'), 'left_high');
assert.equal(Crossover.mirrorRole('left_low_mid'), 'right_low_mid');
assert.equal(Crossover.mirrorRole('sub1'), null);
assert.equal(Crossover.mirrorRole(null), null);

assert.equal(Crossover.clampLevelDb(99), 24);
assert.equal(Crossover.clampLevelDb(-99), -80);
assert.equal(Crossover.clampAlignmentMs(99), 40);
assert.equal(Crossover.clampAlignmentMs(-99), -40);
assert.equal(Crossover.clampFrequencyHz(5), 20);
assert.equal(Crossover.clampFrequencyHz(30000), 20000);

assert.match(indexSource, /crossover\.js\?v=\d+\.\d+\.\d+/);
assert.match(indexSource, /<canvas id="effects-crossover-graph"/);
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
// Starter values autofill the first valid 2/3/4-way config; no button, no status text.
assert.doesNotMatch(appSource, /effectsCrossoverStarter/);
assert.doesNotMatch(appSource, /Already initialized|All ways initialized|Starter values missing/);
assert.match(appSource, /maybeApplyCrossoverStarters/);
// L/R link mirrors only filter values, never trim.
assert.match(appSource, /mirrorRole/);
assert.match(indexSource, /Link L\/R/);
// Filter type Off plus compact headers.
assert.match(appSource, /off: 'Off'/);
assert.match(appSource, /-Way Stereo System/);
assert.match(appSource, /Crossover \$/);
assert.match(appSource, /Main HPF/);
for (const id of ['effects-crossover-card', 'effects-crossover-tabs', 'effects-crossover-graph',
    'effects-crossover-frequency-highpass', 'effects-crossover-frequency-lowpass',
    'effects-crossover-family-highpass', 'effects-crossover-slope-highpass',
    'effects-crossover-family-lowpass', 'effects-crossover-slope-lowpass',
    'effects-crossover-level',
    'effects-crossover-delay', 'effects-crossover-polarity', 'effects-crossover-link',
    'effects-crossover-summary']) {
    assert.match(indexSource, new RegExp(`id="${id}"`), `missing #${id}`);
}
assert.doesNotMatch(indexSource, /id="effects-crossover-starter"/, 'starter button is gone: first valid configs autofill');
assert.match(indexSource, /Link L\/R/);
console.log('crossover frontend tests: ok');
