// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute crossover/speaker tile helpers.
 * Pure way ordering, control enablement, starter sets and response-data
 * selection plus thin DOM renderers for the Crossover card. The response
 * graph itself is painted on canvas by the app in subwoofer-preview style.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCrossover = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

const WAY_ORDER = ['left_low', 'left_low_mid', 'left_mid', 'left_high',
    'right_low', 'right_low_mid', 'right_mid', 'right_high'];
const SUB_ROLES = ['sub_l', 'sub_r', 'sub1', 'sub2'];
const FAMILY_PREFIXES = { 'linkwitz-riley': 'LR', butterworth: 'BW', bessel: 'BS' };

function filterLabel(family, slope) {
    return `${FAMILY_PREFIXES[family] || String(family || '')}${slope}`;
}

function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function orderedWays(ways) {
    const present = new Set(Object.keys(ways || {}).filter((role) => WAY_ORDER.includes(role)));
    return WAY_ORDER.filter((role) => present.has(role));
}

function isLowWayRole(role) {
    return role === 'left_low' || role === 'right_low';
}

// Mirror a speaker way across sides for the L/R link: only the crossover
// filter values are shared, never trim (level/align/polarity stays per-way
// physical tuning, e.g. from Speaker Gain Alignment).
function mirrorRole(role) {
    if (typeof role !== 'string') return null;
    if (role.startsWith('left_')) return `right_${role.slice(5)}`;
    if (role.startsWith('right_')) return `left_${role.slice(6)}`;
    return null;
}

function sideForRole(role) {
    const name = String(role || '');
    return (name === 'sub_l' || name === 'main_l' || name.startsWith('left_')) ? 'left' : 'right';
}

// Way key after the side prefix: left_low_mid -> low_mid.
function wayKey(role) {
    return String(role || '').split('_').slice(1).join('_');
}

function wayLabel(way) {
    return String(way || '').split('_').map((word) =>
        (word === 'low_mid' ? 'Low-Mid' : word.charAt(0).toUpperCase() + word.slice(1))).join(' ');
}

// Linked view: one entry per way, pairing left/right roles. Order follows
// first appearance (WAY_ORDER), so tabs read Low, Low-Mid, Mid, High. A way
// routed on one side only keeps its single-sided entry.
function pairedWays(roles) {
    const pairs = [];
    const index = new Map();
    for (const role of roles || []) {
        const way = wayKey(role);
        if (!way) continue;
        let pair = index.get(way);
        if (!pair) {
            pair = { way, left: null, right: null };
            index.set(way, pair);
            pairs.push(pair);
        }
        if (String(role).startsWith('left_') && !pair.left) pair.left = role;
        else if (String(role).startsWith('right_') && !pair.right) pair.right = role;
    }
    return pairs;
}

function isStereoSubPair(subRoles) {
    const subs = Array.isArray(subRoles) ? subRoles : [];
    return subs.includes('sub_l') && subs.includes('sub_r');
}

// The mode's shared sub crossover. Mono, Dual-Mono and a coupled Stereo pair
// all run exactly these values.
function sharedBassCrossover(bass) {
    const source = bass || {};
    return {
        family: source.family || 'linkwitz-riley',
        slope_db_oct: Number(source.slope_db_oct) || 24,
        frequency_hz: Number(source.frequency_hz) || 80,
    };
}

// Effective sub crossover of one side. Only an unlinked Stereo pair resolves
// to its own stored side filter; a side without an override (and every other
// sub layout) keeps the shared values.
function bassCrossoverForSide(bass, side) {
    const source = bass || {};
    const shared = sharedBassCrossover(bass);
    if (source.sub_link !== false) return shared;
    const override = (source.sub_filters || {})[side];
    if (!override) return shared;
    return { family: override.family, slope_db_oct: override.slope_db_oct,
        frequency_hz: override.frequency_hz };
}

// Shared bass high-pass from the subwoofer tile: with routed subs and Main
// highpass on, the DSP runs every speaker way through the sub crossover
// (type and slope included) as a high-pass. Only the Low way has no stored
// high-pass of its own, so only there is the derived filter displayed.
function bassHighpass(bass, subRoles, role) {
    const subs = Array.isArray(subRoles) ? subRoles.filter(Boolean) : [];
    if (!subs.length) return null;
    if (!bass || bass.main_highpass_enabled !== true) return null;
    const crossover = isStereoSubPair(subs)
        ? bassCrossoverForSide(bass, sideForRole(role))
        : sharedBassCrossover(bass);
    const frequency = Math.round(Number(crossover.frequency_hz));
    if (!Number.isFinite(frequency) || frequency < 40 || frequency > 200) return null;
    return { family: crossover.family, slope_db_oct: crossover.slope_db_oct,
        frequency_hz: frequency };
}

function derivedHighpassForRole(role, bass, subRoles) {
    if (!isLowWayRole(role)) return null;
    return bassHighpass(bass, subRoles, role);
}

function applicableFilters(role, context) {
    const way = String(role || '').split('_').slice(1).join('_');
    if (way === 'low') {
        if (context && derivedHighpassForRole(role, context.bass, context.subRoles)) {
            return ['highpass', 'lowpass'];
        }
        return ['lowpass'];
    }
    if (way === 'high') return ['highpass'];
    return ['highpass', 'lowpass'];
}

function slopesForFamily(family, capabilities) {
    const slopes = capabilities && capabilities.filter_families && capabilities.filter_families[family];
    return Array.isArray(slopes) ? slopes.slice() : [];
}

function starterValues(wayCount) {
    const lr24 = (frequency_hz) => ({ family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz });
    const sides = ['left', 'right'];
    const build = (ways) => {
        const result = {};
        for (const side of sides) {
            for (const [way, highpass, lowpass] of ways) {
                result[`${side}_${way}`] = {};
                if (highpass) result[`${side}_${way}`].highpass = lr24(highpass);
                if (lowpass) result[`${side}_${way}`].lowpass = lr24(lowpass);
            }
        }
        return result;
    };
    // Subwoofers need no starter entries: the shared bass section applies
    // the low-way high-pass whenever subs are present.
    if (wayCount === 2) return build([['low', null, 2000], ['high', 2000, null]]);
    if (wayCount === 3) return build([['low', null, 300], ['mid', 300, 2500], ['high', 2500, null]]);
    if (wayCount === 4) {
        return build([['low', null, 200], ['low_mid', 200, 800], ['mid', 800, 3000],
            ['high', 3000, null]]);
    }
    throw new Error(`Starter values need a 2-way, 3-way or 4-way system, got ${wayCount}`);
}

function missingStarterRoles(processing, starters) {
    const missing = [];
    for (const role of Object.keys(starters || {})) {
        const current = (processing || {})[role] || {};
        const wanted = starters[role] || {};
        const needsHighpass = wanted.highpass && !current.highpass;
        const needsLowpass = wanted.lowpass && !current.lowpass;
        if (needsHighpass || needsLowpass) missing.push(role);
    }
    return missing;
}

function clampLevelDb(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.min(24, Math.max(-80, number));
}

function clampAlignmentMs(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.min(40, Math.max(-40, number));
}

function clampFrequencyHz(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 1000;
    return Math.min(20000, Math.max(20, number));
}

// Starter frequency for re-enabling a cleared filter: picking a type is
// enough to bring the filter back instead of leaving it off.
function starterFrequency(wayCount, role, kind) {
    try {
        const wanted = starterValues(wayCount)?.[role]?.[kind];
        const frequency = Number(wanted?.frequency_hz);
        if (Number.isFinite(frequency)) return clampFrequencyHz(frequency);
    } catch (e) {
        // No starters for this configuration; the caller falls back.
    }
    return null;
}

// Cutoff-normalized magnitude of one crossover shape, in dB. Butterworth is
// |H| = 1/sqrt(1+u^(2N)); Linkwitz-Riley cascades two halves and lands at
// -6 dB on the cutoff; Bessel shares the asymptote and is painted with the
// Butterworth knee. The exact digital sections live in dsp/crossover.py;
// this only paints the tile preview.
function crossoverMagnitudeDb(definition, frequency_hz, kind) {
    const shape = definition || {};
    const cutoff = Math.max(1, Number(shape.frequency_hz) || 80);
    const order = Math.max(1, Math.round((Number(shape.slope_db_oct) || 24) / 6));
    const hz = Math.max(1e-3, Number(frequency_hz) || cutoff);
    const ratio = Math.max(1e-9, kind === 'highpass' ? cutoff / hz : hz / cutoff);
    if (shape.family === 'butterworth' || shape.family === 'bessel') {
        return -10 * Math.log10(1 + Math.pow(ratio, 2 * order));
    }
    return -20 * Math.log10(1 + Math.pow(ratio, order));
}

function wayTabButton(role, text, selected) {
    return `<button type="button" role="tab" aria-selected="${selected}" data-crossover-way="${esc(role)}" class="crossover-tab${selected ? ' is-active' : ''}">${text}</button>`;
}

// Linked pairs share one tab ("L/R · Low") carrying the canonical left
// role; a way routed on one side only keeps its single-sided tab.
function renderWayTabs(tabs, roles, activeRole, onSelect, linked) {
    if (!tabs) return;
    let html;
    if (linked) {
        html = pairedWays(roles).map((pair) => {
            if (pair.left && pair.right) {
                const selected = activeRole === pair.left || activeRole === pair.right;
                return wayTabButton(pair.left, `L/R · ${esc(wayLabel(pair.way))}`, selected);
            }
            const role = pair.left || pair.right;
            const side = String(role).startsWith('left_') ? 'L' : 'R';
            return wayTabButton(role, `${side} · ${esc(wayLabel(pair.way))}`, role === activeRole);
        }).join('');
    } else {
        html = roles.map((role) => {
            const label = role.split('_').slice(1).map((w) => (w === 'low_mid' ? 'Low-Mid' : w.charAt(0).toUpperCase() + w.slice(1))).join(' ');
            const side = role.startsWith('left_') ? 'L' : 'R';
            return wayTabButton(role, `${side} · ${esc(label)}`, role === activeRole);
        }).join('');
    }
    if (tabs.innerHTML !== html) tabs.innerHTML = html;
    if (typeof onSelect === 'function') {
        // The click listener binds once; it delegates to the latest render's
        // callback so toggles (link on/off) never act on a stale closure.
        tabs._crossoverOnSelect = onSelect;
        if (!tabs.dataset.crossoverBound) {
            tabs.dataset.crossoverBound = '1';
            tabs.addEventListener('click', (event) => {
                const button = event.target && event.target.closest
                    ? event.target.closest('[data-crossover-way]') : null;
                if (button && tabs._crossoverOnSelect) tabs._crossoverOnSelect(button.dataset.crossoverWay);
            });
        }
    }
}

    return {
        WAY_ORDER,
        SUB_ROLES,
        FAMILY_PREFIXES,
        filterLabel,
        esc,
        orderedWays,
        wayKey,
        wayLabel,
        pairedWays,
        isLowWayRole,
        mirrorRole,
        sideForRole,
        isStereoSubPair,
        sharedBassCrossover,
        bassCrossoverForSide,
        bassHighpass,
        derivedHighpassForRole,
        applicableFilters,
        slopesForFamily,
        starterValues,
        missingStarterRoles,
        clampLevelDb,
        clampAlignmentMs,
        clampFrequencyHz,
        starterFrequency,
        crossoverMagnitudeDb,
        renderWayTabs,
    };
});
