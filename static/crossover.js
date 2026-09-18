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

function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function orderedWays(ways) {
    const present = new Set(Object.keys(ways || {}).filter((role) => WAY_ORDER.includes(role)));
    return WAY_ORDER.filter((role) => present.has(role));
}

function applicableFilters(role) {
    const way = String(role || '').split('_').slice(1).join('_');
    if (way === 'low') return ['lowpass'];
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

function renderWayTabs(tabs, roles, activeRole, onSelect) {
    if (!tabs) return;
    const html = roles.map((role) => {
        const label = role.split('_').slice(1).map((w) => (w === 'low_mid' ? 'Low-Mid' : w.charAt(0).toUpperCase() + w.slice(1))).join(' ');
        const side = role.startsWith('left_') ? 'L' : 'R';
        return `<button type="button" role="tab" aria-selected="${role === activeRole}" data-crossover-way="${esc(role)}" class="crossover-tab${role === activeRole ? ' is-active' : ''}">${side} · ${esc(label)}</button>`;
    }).join('');
    if (tabs.innerHTML !== html) tabs.innerHTML = html;
    if (typeof onSelect === 'function' && !tabs.dataset.crossoverBound) {
        tabs.dataset.crossoverBound = '1';
        tabs.addEventListener('click', (event) => {
            const button = event.target && event.target.closest
                ? event.target.closest('[data-crossover-way]') : null;
            if (button) onSelect(button.dataset.crossoverWay);
        });
    }
}

    return {
        WAY_ORDER,
        SUB_ROLES,
        esc,
        orderedWays,
        applicableFilters,
        slopesForFamily,
        starterValues,
        missingStarterRoles,
        clampLevelDb,
        clampAlignmentMs,
        clampFrequencyHz,
        renderWayTabs,
    };
});
