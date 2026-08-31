// SPDX-License-Identifier: AGPL-3.0-only
/**
 * AutoSub-aware Target-Curve vertical alignment for the measurement graph.
 *
 * Coordinate systems (verified against real .104 run data):
 *   raw          = sweep analysis output before normalization
 *   normalized   = raw - normalized_by_db            (per sweep!)
 *   displayed    = normalized + anchor_shift - shared_offset
 *   calibrated   = raw = normalized + normalized_by_db
 *
 * Scoring/Gain work in the CALIBRATED coordinate: the target is placed at
 * (target + tvo), where tvo = job.main_target_anchor.target_vertical_offset_db
 * = median(calibrated_main_db - target_db) over the anchor band.
 *
 * New runs therefore embed exact metadata:
 *   - autosub_meta.target_vertical_offset_db  (the run's tvo)
 *   - autosub_meta.main_reference_points      (calibrated Main L/R)
 *   - trace.display_offset_db                 (nb - anchor_shift + shared)
 * The graph recomputes tvo from those Main points for the currently selected
 * curve, so changing the UI target changes both shape and robust level anchor.
 *
 * The current graph Target Curve remains UI-owned. autosub_meta.target records
 * which curve the run used for its result summary, but never selects a curve.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteAutoSubTarget = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    function isAutoSubMeasurement(measurement) {
        return String((measurement && measurement.measurement_kind) || '') === 'auto_sub';
    }

    function finiteNumber(value) {
        return typeof value === 'number' && Number.isFinite(value) ? value : null;
    }

    function median(values) {
        if (!values.length) return null;
        const sorted = [...values].sort((a, b) => a - b);
        const middle = sorted.length >> 1;
        return sorted.length % 2
            ? sorted[middle]
            : (sorted[middle - 1] + sorted[middle]) / 2;
    }

    function targetDbAtFrequency(points, frequencyHz) {
        if (!Array.isArray(points) || points.length < 2 || !(frequencyHz > 0)) return null;
        const firstHz = Number(points[0][0]);
        const lastHz = Number(points[points.length - 1][0]);
        if (!Number.isFinite(firstHz) || !Number.isFinite(lastHz)
            || frequencyHz < firstHz || frequencyHz > lastHz) return null;
        for (let index = 1; index < points.length; index += 1) {
            const lowHz = Number(points[index - 1][0]);
            const lowDb = Number(points[index - 1][1]);
            const highHz = Number(points[index][0]);
            const highDb = Number(points[index][1]);
            if (![lowHz, lowDb, highHz, highDb].every(Number.isFinite)
                || !(lowHz > 0) || !(highHz > lowHz)) return null;
            if (frequencyHz > highHz) continue;
            const ratio = Math.log(frequencyHz / lowHz) / Math.log(highHz / lowHz);
            return lowDb + (highDb - lowDb) * ratio;
        }
        return null;
    }

    function targetVerticalOffsetDb(meta, targetPoints) {
        const references = meta && meta.main_reference_points;
        if (!references || !Array.isArray(targetPoints) || targetPoints.length < 2) return null;
        const offsets = [];
        for (const side of ['left', 'right']) {
            const points = references[side];
            if (!Array.isArray(points) || !points.length) return null;
            for (const point of points) {
                if (!Array.isArray(point) || point.length < 2) return null;
                const frequencyHz = Number(point[0]);
                const mainDb = Number(point[1]);
                const targetDb = targetDbAtFrequency(targetPoints, frequencyHz);
                if (!Number.isFinite(mainDb) || targetDb === null) return null;
                offsets.push(mainDb - targetDb);
            }
        }
        return median(offsets);
    }

    /**
     * Exact scored target offset for the AutoSub set currently on screen.
     *
     * Returns the value to pass to shiftTargetPoints() so the target lands in
     * the display coordinate of the traces, or null when the exact transform
     * is not available (no AutoSub entries, incomplete current metadata, or a
     * normal measurement graph is shown).
     *
     * The backend embeds calibrated Main reference points and each trace's
     * display offset. Recomputing tvo as median(Main - current Target) keeps
     * target switching shape- and level-aware. Since displayed = calibrated -
     * display_offset_db, the value subtracted from the target is
     * (display_offset_db - tvo).
     */
    function resolveTargetOffsetDb(entries, targetPoints) {
        if (!Array.isArray(entries)) return null;
        const autoSubEntries = entries.filter(isAutoSubMeasurement);
        if (!autoSubEntries.length) return null;

        for (const entry of autoSubEntries) {
            const meta = entry.autosub_meta || {};
            const tvo = targetVerticalOffsetDb(meta, targetPoints);
            if (tvo === null) continue;
            for (const trace of entry.traces || []) {
                const displayOffset = finiteNumber(trace.display_offset_db);
                if (displayOffset !== null) {
                    return displayOffset - tvo;
                }
            }
        }

        return null;
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
        resolveTargetOffsetDb,
        targetVerticalOffsetDb,
        isAutoSubMeasurement,
        shiftTargetPoints,
    };
});
