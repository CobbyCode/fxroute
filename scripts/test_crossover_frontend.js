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

// Type/slope travel with the sub crossover, and only a true Stereo pair
// resolves per side while it is unlinked; Dual-Mono stays on the shared one.
const shaped = { frequency_hz: 90, main_highpass_enabled: true, family: 'butterworth', slope_db_oct: 36 };
assert.equal(Crossover.filterLabel('butterworth', 36), 'BW36');
assert.deepEqual(Crossover.bassHighpass(shaped, ['sub1']),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
const unlinked = { ...shaped, sub_link: false, sub_filters: {
    left: { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 } } };
assert.deepEqual(Crossover.bassCrossoverForSide(unlinked, 'left'),
    { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 });
// No right override: that side falls back to the shared crossover.
assert.deepEqual(Crossover.bassCrossoverForSide(unlinked, 'right'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub_l', 'sub_r'], 'left_low'),
    { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub_l', 'sub_r'], 'right_high'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.equal(Crossover.sideForRole('sub_l'), 'left');
assert.equal(Crossover.sideForRole('sub_r'), 'right');
// A coupled pair and a Dual-Mono pair both keep the shared crossover, even
// when the stored state still carries side overrides from an earlier unlink.
assert.deepEqual(Crossover.bassHighpass({ ...unlinked, sub_link: true }, ['sub_l', 'sub_r'], 'left_low'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub1', 'sub2'], 'left_low'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.sharedBassCrossover(unlinked),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });

// Linked way tabs: left/right pairs merge into one L/R tab per way;
// single-sided ways keep their own tab.
assert.equal(Crossover.wayKey('left_low_mid'), 'low_mid');
assert.equal(Crossover.wayLabel('low'), 'Low');
assert.equal(Crossover.wayLabel('low_mid'), 'Low Mid');
assert.deepEqual(
    Crossover.pairedWays(['left_low', 'left_high', 'right_low', 'right_high']),
    [{ way: 'low', left: 'left_low', right: 'right_low' },
     { way: 'high', left: 'left_high', right: 'right_high' }]);
assert.deepEqual(
    Crossover.pairedWays(['left_low', 'right_low', 'left_high']),
    [{ way: 'low', left: 'left_low', right: 'right_low' },
     { way: 'high', left: 'left_high', right: null }]);

function stubTabs() {
    return { innerHTML: '', dataset: {}, addEventListener() {} };
}
{
    // Linked: two tabs for a 2-way pair, canonical left data values, and
    // either counterpart counts as active.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs,
        ['left_low', 'left_high', 'right_low', 'right_high'], 'right_low', () => {}, true);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 2);
    assert.match(tabs.innerHTML, /L\/R · Low/);
    assert.match(tabs.innerHTML, /L\/R · High/);
    assert.match(tabs.innerHTML, /data-crossover-way="left_low"/);
    assert.doesNotMatch(tabs.innerHTML, /data-crossover-way="right_low"/);
    assert.match(tabs.innerHTML, /data-crossover-way="left_low" class="crossover-tab is-active"/);
}
{
    // Linked with a single-sided way: pair tab plus lone L tab.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs, ['left_low', 'right_low', 'left_high'], 'left_high', () => {}, true);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 2);
    assert.match(tabs.innerHTML, /L\/R · Low/);
    assert.match(tabs.innerHTML, /L · High/);
}
{
    // Unlinked render is unchanged: one tab per side.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs,
        ['left_low', 'left_high', 'right_low', 'right_high'], 'right_low', () => {}, false);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 4);
    assert.match(tabs.innerHTML, /R · Low/);
    assert.match(tabs.innerHTML, /data-crossover-way="right_low" class="crossover-tab is-active"/);
}

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

assert.equal(Crossover.starterFrequency(2, 'left_low', 'lowpass'), 2000);
assert.equal(Crossover.starterFrequency(2, 'right_high', 'highpass'), 2000);
assert.equal(Crossover.starterFrequency(3, 'left_mid', 'lowpass'), 2500);
assert.equal(Crossover.starterFrequency(4, 'right_mid', 'highpass'), 800);
assert.equal(Crossover.starterFrequency(2, 'left_low', 'highpass'), null);
assert.equal(Crossover.starterFrequency(5, 'left_low', 'lowpass'), null);

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
// Link L/R is off by default: unchecked box, false initial state, and
// strict true-checks so an unset state never links.
assert.doesNotMatch(indexSource, /id="effects-crossover-link" checked/);
assert.match(appSource, /linkLR:\s*false/);
assert.match(appSource, /state\.crossover\.linkLR === true/);
assert.doesNotMatch(appSource, /state\.crossover\.linkLR !== false/);
// Trim stays side-specific and is hidden while linked: unlinked shows all
// trim values, linked shows only the shared crossover parameters.
assert.match(indexSource, /id="effects-crossover-trim-group"/);
assert.match(appSource, /effectsCrossoverTrimGroup/);
assert.match(appSource, /Trim[\s\S]*hidden while linked|hidden while linked/);
// Filter type Off plus compact headers.
assert.match(appSource, /off: 'Off'/);
assert.match(appSource, /-Way Stereo System/);
assert.match(appSource, /Crossover \$/);
assert.match(appSource, /Main HPF/);
for (const id of ['effects-crossover-card', 'effects-crossover-tabs', 'effects-crossover-graph',
    'effects-crossover-frequency-highpass', 'effects-crossover-frequency-lowpass',
    'effects-crossover-family-highpass', 'effects-crossover-slope-highpass',
    'effects-crossover-family-lowpass', 'effects-crossover-slope-lowpass',
    'effects-crossover-trim-group',
    'effects-crossover-level',
    'effects-crossover-delay', 'effects-crossover-polarity', 'effects-crossover-link',
    'effects-crossover-summary']) {
    assert.match(indexSource, new RegExp(`id="${id}"`), `missing #${id}`);
}
assert.doesNotMatch(indexSource, /id="effects-crossover-starter"/, 'starter button is gone: first valid configs autofill');
assert.match(indexSource, /Link L\/R/);
// Way tabs and the L/R link share one row above the graph; no separate
// actions row below Trim remains.
assert.match(indexSource, /class="crossover-tabrow"/);
assert.doesNotMatch(indexSource, /crossover-actions/);
// A linked save carries only the shared crossover filters; trim fields are
// added back only when unlinked.
assert.match(appSource, /if\s*\(!linked\)\s*\{\s*\n.*mutation\.level_db/s);
assert.match(appSource, /effectsCrossoverTrimGroup.*linkedCrossover \? 'none' : ''/);
{
    const tabrow = indexSource.indexOf('class="crossover-tabrow"');
    const tabs = indexSource.indexOf('id="effects-crossover-tabs"');
    const link = indexSource.indexOf('id="effects-crossover-link"');
    const graph = indexSource.indexOf('id="effects-crossover-graph"');
    assert.ok(tabrow >= 0 && tabrow < tabs && tabs < link && link < graph,
        'order must read tab row, way tabs, link, graph');
}
console.log('crossover frontend tests: ok');
