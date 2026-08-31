// SPDX-License-Identifier: AGPL-3.0-only
/**
 * AutoSub trace alignment for the measurement graph.
 *
 * Coordinate systems (verified against real .104 run data):
 *   raw          = sweep analysis output before normalization
 *   normalized   = raw - normalized_by_db            (per sweep!)
 *   displayed    = normalized + anchor_shift - shared_offset
 *   calibrated   = raw = normalized + normalized_by_db
 *
 * Scoring/Gain work in the CALIBRATED coordinate. Graph traces are moved as
 * one rigid set into the normal measurement coordinate, where the selected
 * Target Curve keeps its unshifted shape (Neutral at 0 dB).
 *
 * Saved runs embed the calibrated Main L/R reference points and each trace's
 * calibrated-to-display offset. The graph recomputes the broadband Main/Target
 * anchor for the currently selected curve and applies the inverse of the old
 * target shift to the traces instead.
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
        if (!Array.isArray(points) || !points.length || !(frequencyHz > 0)) return null;
        const firstHz = Number(points[0][0]);
        const firstDb = Number(points[0][1]);
        if (!Number.isFinite(firstHz) || !(firstHz > 0) || !Number.isFinite(firstDb)) return null;
        if (frequencyHz <= firstHz || points.length === 1) return firstDb;
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
        return Number(points[points.length - 1][1]);
    }

    function targetVerticalOffsetDb(meta, targetPoints) {
        const references = meta && meta.main_reference_points;
        if (!references || !Array.isArray(targetPoints) || !targetPoints.length) return null;
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

    function mainReferenceKey(meta) {
        const references = meta && meta.main_reference_points;
        if (!references) return null;
        const normalized = {};
        for (const side of ['left', 'right']) {
            const points = references[side];
            if (!Array.isArray(points) || !points.length) return null;
            normalized[side] = points.map((point) => [Number(point[0]), Number(point[1])]);
        }
        return JSON.stringify(normalized);
    }

    /**
     * Move each saved AutoSub run as one rigid set into the selected Target
     * Curve's normal graph coordinate. A single median display offset per run
     * keeps Before/After and L/R differences unchanged and order-independent.
     */
    function alignAutoSubEntries(entries, targetPoints, referenceEntries = entries) {
        if (!Array.isArray(entries)) return entries;
        const groups = new Map();
        const groupKeyByEntryIndex = new Map();

        (Array.isArray(referenceEntries) ? referenceEntries : entries).forEach((entry) => {
            if (!isAutoSubMeasurement(entry)) return;
            const meta = entry.autosub_meta || {};
            const key = mainReferenceKey(meta);
            const targetOffset = targetVerticalOffsetDb(meta, targetPoints);
            if (key === null || targetOffset === null) return;

            if (!groups.has(key)) {
                groups.set(key, { targetOffset, displayOffsets: [], complete: true });
            }
            const group = groups.get(key);
            if (Math.abs(group.targetOffset - targetOffset) > 1e-9) group.complete = false;
            const traces = Array.isArray(entry.traces) ? entry.traces : [];
            if (!traces.length) group.complete = false;
            traces.forEach((trace) => {
                const displayOffset = finiteNumber(trace.display_offset_db);
                if (displayOffset === null) group.complete = false;
                else group.displayOffsets.push(displayOffset);
            });
        });

        entries.forEach((entry, entryIndex) => {
            if (!isAutoSubMeasurement(entry)) return;
            const meta = entry.autosub_meta || {};
            const key = mainReferenceKey(meta);
            if (key !== null && targetVerticalOffsetDb(meta, targetPoints) !== null) {
                groupKeyByEntryIndex.set(entryIndex, key);
            }
        });

        const shiftByGroupKey = new Map();
        groups.forEach((group, key) => {
            if (!group.complete || !group.displayOffsets.length) return;
            shiftByGroupKey.set(key, median(group.displayOffsets) - group.targetOffset);
        });

        return entries.map((entry, entryIndex) => {
            const shiftDb = shiftByGroupKey.get(groupKeyByEntryIndex.get(entryIndex));
            if (!Number.isFinite(shiftDb)) return entry;
            return {
                ...entry,
                traces: entry.traces.map((trace) => ({
                    ...trace,
                    points: trace.points.map((point) => [Number(point[0]), Number(point[1]) + shiftDb]),
                })),
            };
        });
    }

    return {
        alignAutoSubEntries,
        targetVerticalOffsetDb,
        isAutoSubMeasurement,
    };
});
