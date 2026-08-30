// SPDX-License-Identifier: AGPL-3.0-only
/**
 * AutoSub-aware Target-Curve vertical alignment for the measurement graph.
 *
 * AutoSub Before/After display traces are normalized by the backend to a
 * shared vertical reference: the median dB of the combined 20-200 Hz bass
 * region of the baseline sweep (see _auto_sub_shared_bass_offset in
 * measurement/autosub/scoring.py). The Target Curve must be drawn against
 * the same reference or it sits at an arbitrary height relative to the
 * measured traces.
 *
 * This module computes the same offset from the graph entries currently on
 * screen and shifts the target curve by it. No fixed dB constants are used;
 * the offset is derived from whatever traces are visible, so Neutral,
 * Harman, and custom house targets all land correctly, and the target's
 * shape is untouched (a single constant offset).
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
     * The shared vertical reference (display offset) used by the AutoSub
     * traces currently visible in the graph, or null when the graph is not
     * showing an AutoSub Before/After set.
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
     * Shift target-curve [freq, db] points into the AutoSub display
     * reference. offsetDb is the median bass level of the visible AutoSub
     * traces; subtracting it reproduces exactly the transformation the
     * backend applied to the measured traces. The curve shape is unchanged.
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
        isAutoSubMeasurement,
        shiftTargetPoints,
    };
});
