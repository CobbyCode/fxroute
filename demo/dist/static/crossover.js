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

// Shared bass high-pass from the subwoofer tile: with routed subs and Main
// highpass on, the DSP runs every speaker way through an LR24 high-pass at
// the sub crossover. Only the Low way has no stored high-pass of its own,
// so only there is the derived filter displayed.
function bassHighpass(bass, subRoles) {
    const subs = Array.isArray(subRoles) ? subRoles.filter(Boolean) : [];
    if (!subs.length) return null;
    if (!bass || bass.main_highpass_enabled !== true) return null;
    const frequency = Math.round(Number(bass.frequency_hz));
    if (!Number.isFinite(frequency) || frequency < 40 || frequency > 200) return null;
    return { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: frequency };
}

function derivedHighpassForRole(role, bass, subRoles) {
    if (!isLowWayRole(role)) return null;
    return bassHighpass(bass, subRoles);
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
        isLowWayRole,
        mirrorRole,
        bassHighpass,
        derivedHighpassForRole,
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
