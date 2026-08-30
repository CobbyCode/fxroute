// SPDX-License-Identifier: AGPL-3.0-only
/**
 * AutoSub-aware Target-Curve vertical alignment for the measurement graph.
 *
 * Coordinate systems (verified against real .104 run data):
 *   raw          = sweep analysis output before normalization
 *   normalized   = raw - normalized_by_db            (per sweep!)
 *   displayed    = normalized - anchor_shift - shared_offset
 *   calibrated   = raw = normalized + normalized_by_db
 *
 * Scoring/Gain work in the CALIBRATED coordinate: the target is placed at
 * (target + tvo), where tvo = job.main_target_anchor.target_vertical_offset_db
 * = median(calibrated_main_db - target_db) over the anchor band.
 *
 * New runs therefore embed exact metadata:
 *   - autosub_meta.target_vertical_offset_db  (the run's tvo)
 *   - trace.display_offset_db                 (nb + anchor_shift + shared)
 * and the exact displayed target position is:
 *   target_displayed_db = target + tvo - display_offset_db
 *
 * For legacy runs without this metadata we fall back to the shared bass
 * reference (median 20-200 Hz of the visible traces) so the target at least
 * sits on the same vertical reference as the traces. No fixed dB constants
 * are used anywhere; Neutral, Harman and custom targets all work because the
 * offset is derived per run.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteAutoSubTarget = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    // Must match the backend's _auto_sub_shared_bass_offset band.
    const BASS_LOW_HZ = 20.0;
    const BASS_HIGH_HZ = 200.0;

    function isAutoSubMeasurement(measurement) {
        return String((measurement && measurement.measurement_kind) || '') === 'auto_sub';
    }

    function finiteNumber(value) {
        return typeof value === 'number' && Number.isFinite(value) ? value : null;
    }

    /**
     * Median dB over the 20-200 Hz band of the given point lists.
     * Returns null when no points fall inside the band.
     */
    function bassMedianDb(pointLists) {
        const dbs = [];
        for (const points of pointLists) {
            if (!Array.isArray(points)) continue;
            for (const point of points) {
                if (!Array.isArray(point) || point.length < 2) continue;
                const hz = Number(point[0]);
                const db = Number(point[1]);
                if (Number.isFinite(hz) && Number.isFinite(db) && hz >= BASS_LOW_HZ && hz <= BASS_HIGH_HZ) {
                    dbs.push(db);
                }
            }
        }
        if (!dbs.length) return null;
        dbs.sort((a, b) => a - b);
        const mid = dbs.length >> 1;
        return dbs.length % 2 ? dbs[mid] : (dbs[mid - 1] + dbs[mid]) / 2;
    }

    /**
     * Legacy fallback: the shared vertical reference (display offset) used by
     * the AutoSub traces currently visible in the graph, or null when the
     * graph is not showing an AutoSub Before/After set.
     *
     * Entries are already-normalized display entries (graph entries), i.e.
     * the backend's offset has been applied to their points.
     */
    function getAutoSubDisplayOffsetDb(entries) {
        if (!Array.isArray(entries)) return null;
        const autoSubEntries = entries.filter(isAutoSubMeasurement);
        if (!autoSubEntries.length) return null;
        const pointLists = [];
        for (const entry of autoSubEntries) {
            for (const trace of entry.traces || []) {
                if (Array.isArray(trace.points) && trace.points.length) {
                    pointLists.push(trace.points);
                }
            }
        }
        if (!pointLists.length) return null;
        return bassMedianDb(pointLists);
    }

    /**
     * Exact scored target offset for the AutoSub set currently on screen.
     *
     * Returns the offset to SUBTRACT from (target + tvo) so the target lands
     * in the display coordinate of the traces, or null when the exact
     * transform is not available (no AutoSub entries, no metadata, or a
     * normal measurement graph is shown).
     *
     * The backend embeds:
     *   autosub_meta.target_vertical_offset_db  (run's tvo, calibrated coords)
     *   trace.display_offset_db                 (nb + anchor_shift + shared)
     * and the exact displayed target position is:
     *   target_displayed = target + tvo - display_offset_db
     */
    function resolveTargetOffsetDb(entries) {
        if (!Array.isArray(entries)) return null;
        const autoSubEntries = entries.filter(isAutoSubMeasurement);
        if (!autoSubEntries.length) return null;

        // Prefer the exact metadata chain (new runs).
        for (const entry of autoSubEntries) {
            const meta = entry.autosub_meta || {};
            const tvo = finiteNumber(meta.target_vertical_offset_db);
            if (tvo === null) continue;
            for (const trace of entry.traces || []) {
                const displayOffset = finiteNumber(trace.display_offset_db);
                if (displayOffset !== null) {
                    return tvo - displayOffset;
                }
            }
        }

        // Legacy fallback: shared bass reference (no exact metadata available).
        return getAutoSubDisplayOffsetDb(entries);
    }

    /**
     * Shift target-curve [freq, db] points by the given constant offset.
     * The curve shape is unchanged (single constant per point).
     */
    function shiftTargetPoints(points, offsetDb) {
        if (offsetDb === null || offsetDb === undefined || !Array.isArray(points)) return points;
        return points.map((point) => {
            if (!Array.isArray(point) || point.length < 2) return point;
            return [Number(point[0]), Number(point[1]) - offsetDb];
        });
    }

    return {
        BASS_LOW_HZ,
        BASS_HIGH_HZ,
        bassMedianDb,
        getAutoSubDisplayOffsetDb,
        resolveTargetOffsetDb,
        isAutoSubMeasurement,
        shiftTargetPoints,
    };
});
