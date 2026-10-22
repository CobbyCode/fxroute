// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Calibration overview of the A/B card: Level Matching trims, subwoofer
 * gains, the effective AV Sync delay and the SPL calibration.
 *
 * Trims, gains and SPL are read from state the app already holds (output-state
 * catalog, global extras). Only the effective delay is a live engine readback;
 * it is polled while the DSP tab is visible. Browser-loadable UMD, no build
 * step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCalibrationOverview = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const POLL_MS = 3000;
    const RETRY_MS = 800;
    // Event triggers arrive in bursts (catalog and effects render together).
    const SETTLE_MS = 200;
    // Level the loudness contour assumes until a calibration is stored
    // (measurement/spl_calibration.py target).
    const SPL_DEFAULT_DB = 83;
    const SPEAKER_ORDER = ['fl', 'fr', 'c', 'sl', 'sr', 'rl', 'rr'];
    const SPEAKER_KEYS = { main_l: 'fl', main_r: 'fr', center: 'c', side_l: 'sl', side_r: 'sr', rear_l: 'rl', rear_r: 'rr' };
    // A stereo system names its pair like the footer meter does.
    const STEREO_LABELS = { fl: 'L', fr: 'R' };

    let deps = {
        getElements: () => ({}),
        getState: () => ({}),
        fetch: (...args) => root.fetch(...args),
        isVisible: () => true,
        setTimeout: (...args) => root.setTimeout(...args),
        clearTimeout: (id) => root.clearTimeout(id),
    };
    let avSync = null;
    let sequence = 0;
    let timer = null;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // All ways of a crossover side are one speaker.
    function speakerKey(role) {
        if (role.startsWith('left_')) return 'fl';
        if (role.startsWith('right_')) return 'fr';
        if (role.startsWith('center_')) return 'c';
        return SPEAKER_KEYS[role] || null;
    }

    function signedDb(value) {
        const rounded = Math.round(value * 10) / 10;
        if (rounded === 0) return '0.0';
        return `${rounded > 0 ? '+' : '−'}${Math.abs(rounded).toFixed(1)}`;
    }

    function summarize(catalog, extras, sync) {
        const mode = catalog?.active_mode;
        const config = catalog?.modes?.[mode] || {};
        const processing = config.processing || {};
        const trims = new Map();
        for (const role of config.topology?.roles || []) {
            const key = speakerKey(String(role));
            const slots = processing[role]?.level_cal_db;
            if (!key || !slots) continue;
            const trim = Object.values(slots).reduce((sum, value) => sum + (Number(value) || 0), 0);
            trims.set(key, [...(trims.get(key) || []), trim]);
        }
        const levels = SPEAKER_ORDER.filter(key => trims.has(key)).map(key => ({
            label: (mode !== 'surround' && STEREO_LABELS[key]) || key.toUpperCase(),
            minDb: Math.min(...trims.get(key)),
            maxDb: Math.max(...trims.get(key)),
        }));
        const subRoles = config.topology?.sub_roles || [];
        const subs = subRoles.map((role, index) => ({
            label: subRoles.length > 1 ? `S${index + 1}` : 'SUB',
            db: Number(processing[role]?.level_db) || 0,
        }));
        const latency = sync?.latency;
        const delayMs = latency?.ready && Number.isFinite(latency.actual_ms) ? latency.actual_ms : null;
        // The extras carry the calibration of the output that renders; an output without one reads none.
        const params = extras?.loudness?.params || {};
        const calibration = params.calibration || {};
        const calibrated = calibration.measuredSplDb != null && Number.isFinite(Number(calibration.measuredSplDb));
        const offset = calibration.requiredAdjustmentDb;
        // Outputs that have a calibration of their own, shown when this one has none.
        const otherOutputs = calibrated ? [] : Object.values(params.calibrationProfiles || {})
            .filter(record => record && record.measuredSplDb != null && Number.isFinite(Number(record.measuredSplDb)))
            .map(record => String(record.outputProfileLabel || record.outputProfileId || '').trim())
            .filter(Boolean);
        return {
            levels, subs, delayMs,
            spl: {
                db: calibrated ? Number(calibration.measuredSplDb) : SPL_DEFAULT_DB,
                isDefault: !calibrated,
                offsetDb: calibrated && offset != null && Number.isFinite(Number(offset)) ? Number(offset) : null,
                ...(otherOutputs.length ? { otherOutputs } : {}),
            },
        };
    }

    // Speakers without a correction step back so the active ones stand out.
    function chip(label, text) {
        return `<span class="effects-cal-chip${text === '0.0' ? ' is-zero' : ''}"><span class="effects-cal-key">${label} </span>${text}</span>`;
    }

    // The unit stays on the line of the last chip when a wide Surround wraps.
    function gains(chips) {
        return `${chips.slice(0, -1).map(item => `${item} `).join('')}<span class="effects-cal-tail">${chips.at(-1)} <span class="effects-cal-unit">dB</span></span>`;
    }

    function group(label, tooltip, body) {
        return `<span class="effects-cal-group" data-tooltip="${tooltip}"><span class="effects-cal-label">${label}</span><span class="effects-cal-values">${body}</span></span>`;
    }

    // Output names come from hardware and reach an attribute.
    function escapeAttribute(value) {
        return String(value).replace(/[&<>"']/g, character => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
    }

    function overviewHtml(summary) {
        const rows = [];
        if (summary.levels.length) {
            rows.push([group('Level', 'Level Matching trims', gains(summary.levels.map((speaker) => {
                // Crossover ways normally share one trim; unequal ways show their range.
                const low = signedDb(speaker.minDb), high = signedDb(speaker.maxDb);
                return chip(speaker.label, low === high ? low : `${low}…${high}`);
            })))]);
        }
        if (summary.subs.length) {
            rows.push([group('Sub', 'Subwoofer gain', gains(summary.subs.map(sub => chip(sub.label, signedDb(sub.db)))))]);
        }
        const spl = summary.spl;
        const splDetail = spl.isDefault ? ' <span class="effects-cal-tag">default</span>'
            : (spl.offsetDb == null ? '' : ` <span class="effects-cal-note">${signedDb(spl.offsetDb)} dB</span>`);
        const splTooltip = spl.isDefault
            ? (spl.otherOutputs
                ? `SPL Calibration: not calibrated for this output, default reference (calibrated: ${escapeAttribute(spl.otherOutputs.join(', '))})`
                : 'SPL Calibration: not calibrated, default reference')
            : `SPL Calibration: measured level${spl.offsetDb == null ? '' : ' and Loudness offset'}`;
        rows.push([
            group('Delay', 'Effective delay (AV Sync)', summary.delayMs == null
                ? '<span class="effects-cal-pending">—</span>'
                : `<span class="effects-cal-badge">${summary.delayMs.toFixed(1)} ms</span>`),
            group('SPL', splTooltip, `<span class="effects-cal-value">${spl.db.toFixed(1)} dB</span>${splDetail}`),
        ]);
        return rows.map(row => `<div class="effects-cal-row">${row.join('')}</div>`).join('');
    }

    function render() {
        const host = deps.getElements().effectsInfo;
        const state = deps.getState();
        if (!host || !state.dsp?.available) return;
        const html = overviewHtml(summarize(state.outputSystem?.catalog, state.dsp.global_extras, avSync));
        // Polls must not rebuild unchanged nodes: an open tooltip would lose its host.
        if (host.innerHTML !== html) host.innerHTML = html;
    }

    function schedule(delay) {
        deps.clearTimeout(timer);
        timer = deps.setTimeout(() => { void poll(); }, delay);
    }

    async function poll() {
        // A hidden tile stops polling; the next refresh() restarts it.
        if (!deps.isVisible()) return;
        const request = ++sequence;
        let next = null;
        try {
            const response = await deps.fetch('/api/audio/av-sync');
            const data = await response.json();
            if (response.ok) next = data;
        } catch (error) {
            // Unknown delay renders as a dash.
        }
        if (request !== sequence) return;
        avSync = next;
        render();
        schedule(avSync?.latency?.ready ? POLL_MS : RETRY_MS);
    }

    // Settings changed or the tile became visible: render now, re-read the delay shortly after.
    function refresh() {
        render();
        // Drops a poll in flight so it cannot postpone the re-read.
        sequence += 1;
        schedule(SETTLE_MS);
    }

    return { init, summarize, overviewHtml, render, refresh, poll };
});
