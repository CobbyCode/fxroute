// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute crossover response graph painter.
 * Canonical owner of the Crossover/Speaker tile canvas rendering, in the
 * subwoofer-preview visual language, plus the plot geometry the cutoff drag
 * handles need (marker list, hit test, frequency at a pointer). Pure painter:
 * canvas plus response data in, pixels out. Way ordering comes from
 * crossover.js at call time; state, mutations and listeners stay in
 * crossover_ui.js. Browser-loadable UMD, no build step; Node-testable via
 * require() with a stub canvas.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCrossoverView = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

// Plot geometry in display pixels. The painter and the cutoff drag handles
// share it, so a handle can never sit somewhere other than the line it moves.
const PLOT = {
    pad: { left: 56, right: 24, top: 18, bottom: 24 },
    minHz: 20,
    maxHz: 20000,
    minDb: -30,
    maxDb: 6,
};
// A 1px cutoff line is a thin grab target; the hit area is a wide column
// around it, so the line can be caught well off its exact pixel.
const HANDLE_HIT_PX = 16;
// Cutoff lines closer than this (log distance) share one marker: the stored
// filter wins, which is the marker the painter has always drawn.
const MARKER_MERGE_LOG = 0.02;
// Same log grid and point count the response endpoint uses, so a previewed
// curve does not jump when the refetched curve replaces it.
const PREVIEW_POINTS = 180;

function graphLayout(canvas) {
    if (!canvas) return null;
    const clientWidth = Math.round(canvas.clientWidth || 0);
    // Hidden cards (other tab, closed panel) report no width: the painter
    // skips instead of painting a fallback size that would stick.
    if (!clientWidth) return null;
    const width = Math.max(320, clientWidth);
    const height = Math.max(120, Math.round(canvas.clientHeight || 136));
    const { pad } = PLOT;
    return { width, height, pad,
        plotW: Math.max(1, width - pad.left - pad.right),
        plotH: Math.max(1, height - pad.top - pad.bottom) };
}

function xForHz(layout, hz) {
    return layout.pad.left + ((Math.log10(Math.max(PLOT.minHz, hz)) - Math.log10(PLOT.minHz))
        / (Math.log10(PLOT.maxHz) - Math.log10(PLOT.minHz))) * layout.plotW;
}

function hzForX(layout, x) {
    const t = (x - layout.pad.left) / layout.plotW;
    return PLOT.minHz * Math.pow(PLOT.maxHz / PLOT.minHz, Math.max(0, Math.min(1, t)));
}

function clientXInPlot(canvas, clientX) {
    if (typeof canvas.getBoundingClientRect !== 'function') return null;
    const value = Number(clientX);
    if (!Number.isFinite(value)) return null;
    return value - canvas.getBoundingClientRect().left;
}

// The cutoff lines of one way, in paint order. Stored filters are editable
// unless the caller says otherwise (Off direction, busy save, direction the
// way does not run); the sub-owned derived high-pass is never editable, it
// belongs to the Subwoofer tile. `frequency` previews a live drag position so
// the dragged line and its handle follow the pointer.
function crossoverMarkers(ways, activeRole, options = {}) {
    const entry = (ways || {})[activeRole] || {};
    const filters = entry.filters || {};
    const override = options.frequency || null;
    const canEdit = typeof options.canEdit === 'function' ? options.canEdit : null;
    const markers = [];
    for (const kind of ['highpass', 'lowpass']) {
        const filter = filters[kind] || null;
        let hz = Number(filter && filter.frequency_hz);
        if (override && override.kind === kind) {
            const overrideHz = Number(override.frequency_hz);
            if (Number.isFinite(overrideHz)) hz = overrideHz;
        }
        if (!Number.isFinite(hz) || hz < PLOT.minHz || hz > PLOT.maxHz) continue;
        const editable = !!filter && (filter.family || 'linkwitz-riley') !== 'off'
            && (!canEdit || canEdit(kind) !== false);
        markers.push({ kind, frequency_hz: hz, derived: false, editable });
    }
    // Sub-owned Low high-pass: no stored filter, but the running curve
    // includes it, so mark it like a stored cutoff. Skipped when a stored
    // high-pass already marks (near-)the same frequency.
    const derived = entry.derived_highpass || null;
    if (derived && !filters.highpass) {
        const hz = Number(derived.frequency_hz);
        if (Number.isFinite(hz) && hz >= PLOT.minHz && hz <= PLOT.maxHz
            && !markers.some((marked) => Math.abs(Math.log(marked.frequency_hz / hz)) < MARKER_MERGE_LOG)) {
            markers.push({ kind: 'highpass', frequency_hz: hz, derived: true, editable: false });
        }
    }
    return markers;
}

// Frequency the graph shows while a cutoff line is being dragged: the stored
// filters with the dragged one at its live position. The server curve stays
// the authority; this only keeps the preview from lagging behind the pointer
// between two refetches.
function previewWayPoints(entry, preview) {
    const crossover = (root && root.FXRouteCrossover) || null;
    if (!crossover || typeof crossover.crossoverMagnitudeDb !== 'function') return null;
    const kind = preview && preview.kind;
    const frequency_hz = Number(preview && preview.frequency_hz);
    if (!kind || !Number.isFinite(frequency_hz)) return null;
    const filters = (entry && entry.filters) || {};
    const dragged = filters[kind] || null;
    if (!dragged) return null;
    const shapes = [];
    for (const direction of ['highpass', 'lowpass']) {
        const stored = filters[direction];
        if (!stored) continue;
        shapes.push({ direction, definition: direction === kind
            ? { family: stored.family, slope_db_oct: stored.slope_db_oct, frequency_hz }
            : stored });
    }
    const derived = !filters.highpass ? (entry.derived_highpass || null) : null;
    if (derived) shapes.push({ direction: 'highpass', definition: derived });
    if (!shapes.length) return null;
    const points = [];
    for (let index = 0; index < PREVIEW_POINTS; index += 1) {
        const hz = PLOT.minHz * Math.pow(PLOT.maxHz / PLOT.minHz, index / (PREVIEW_POINTS - 1));
        let db = 0;
        for (const shape of shapes) db += crossover.crossoverMagnitudeDb(shape.definition, hz, shape.direction);
        points.push([Math.round(hz * 1000) / 1000, Math.round(db * 1000) / 1000]);
    }
    return points;
}

// Frequency under a pointer x, rounded to whole Hz and clamped to the plot:
// dragging past either plot edge parks the cutoff at 20 Hz or 20 kHz.
function crossoverGraphFrequencyAt(canvas, clientX) {
    const layout = graphLayout(canvas);
    if (!layout) return null;
    const x = clientXInPlot(canvas, clientX);
    if (x === null) return null;
    return Math.round(hzForX(layout, x));
}

// The editable cutoff line under a pointer x, or null. Editable lines win
// over a nearer read-only one (the sub-owned high-pass), so a press always
// grabs a line the user is allowed to move.
function crossoverGraphHandleAt(ways, activeRole, clientX, options = {}) {
    const canvas = options.canvas || null;
    const layout = graphLayout(canvas);
    if (!layout) return null;
    const x = clientXInPlot(canvas, clientX);
    if (x === null) return null;
    const markers = crossoverMarkers(ways, activeRole,
        { canEdit: options.canEdit, frequency: options.frequency });
    let best = null;
    let bestDistance = Infinity;
    for (const marker of markers) {
        if (!marker.editable) continue;
        const distance = Math.abs(xForHz(layout, marker.frequency_hz) - x);
        if (distance > HANDLE_HIT_PX || distance >= bestDistance) continue;
        best = marker;
        bestDistance = distance;
    }
    return best;
}

function drawCrossoverResponse(canvas, ways, activeRole, preview) {
    // Same visual language as the subwoofer preview: dark plot, dB/Hz
    // axes, dimmed ways, highlighted active way, cutoff markers.
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const crossover = (root && root.FXRouteCrossover) || null;
    const ordered = crossover && typeof crossover.orderedWays === 'function' ? crossover.orderedWays(ways || {}) : Object.keys(ways || {});
    const layout = graphLayout(canvas);
    if (!layout) return;
    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    const targetWidth = Math.round(layout.width * dpr);
    const targetHeight = Math.round(layout.height * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = layout.width;
    const height = layout.height;
    const { pad, plotW, plotH } = layout;
    const minDb = PLOT.minDb;
    const maxDb = PLOT.maxDb;
    const xForHzAt = (hz) => xForHz(layout, hz);
    const yForDb = (db) => pad.top + ((maxDb - Math.max(minDb, Math.min(maxDb, db))) / (maxDb - minDb)) * plotH;
    const formatHz = (hz) => hz >= 1000 ? `${parseFloat((hz / 1000).toFixed(1))} kHz` : `${Math.round(hz)} Hz`;
    const formatTickHz = (hz) => hz >= 1000 ? `${parseFloat((hz / 1000).toFixed(1))} kHz` : `${hz} Hz`;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#08111f';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = 'rgba(255,255,255,0.035)';
    ctx.fillRect(pad.left, pad.top, plotW, plotH);
    ctx.strokeStyle = 'rgba(255,255,255,0.10)';
    ctx.lineWidth = 1;
    const frequencyLabels = [20, 100, 500, 2000, 10000];
    const dbLabels = [0, -6, -12, -18, -24];
    frequencyLabels.forEach((hz) => {
        const x = xForHzAt(hz);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
    });
    dbLabels.forEach((db) => {
        const y = yForDb(db);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
    });
    ctx.fillStyle = 'rgba(229,231,235,0.54)';
    ctx.font = '500 10px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    dbLabels.forEach((db) => {
        ctx.fillText(`${db} dB`, pad.left - 8, yForDb(db));
    });
    ctx.textAlign = 'center';
    ctx.textBaseline = 'alphabetic';
    frequencyLabels.forEach((hz) => {
        ctx.fillText(formatTickHz(hz), xForHzAt(hz), height - 8);
    });
    ctx.textAlign = 'start';
    const drawWay = (role, color, lineWidth, overridePoints) => {
        const entry = (ways || {})[role];
        const points = (overridePoints && overridePoints.length >= 2) ? overridePoints
            : entry && Array.isArray(entry.points) ? entry.points : null;
        if (!points || points.length < 2) return;
        // A way with a cleared (Off) direction keeps its real curve, drawn
        // dashed so the open band is visible in the graph as well.
        ctx.setLineDash(entry.complete === false ? [6, 4] : []);
        ctx.beginPath();
        let started = false;
        for (const point of points) {
            const hz = Number(point && point[0]);
            const db = Number(point && point[1]);
            if (!Number.isFinite(hz) || !Number.isFinite(db)) continue;
            const x = xForHzAt(hz);
            const y = yForDb(db);
            if (!started) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
            started = true;
        }
        if (!started) {
            ctx.setLineDash([]);
            return;
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = lineWidth;
        ctx.stroke();
        ctx.setLineDash([]);
    };
    ordered.filter((role) => role !== activeRole)
        .forEach((role) => drawWay(role, 'rgba(107,114,128,0.85)', 1.5));
    // While a cutoff line is dragged the active way is repainted from the
    // stored filters at the pointer position, so the curve, the line and the
    // label all follow the drag until the refetched curve takes over.
    const activeEntry = (ways || {})[activeRole] || {};
    const previewPoints = preview ? previewWayPoints(activeEntry, preview) : null;
    drawWay(activeRole, '#60a5fa', 3, previewPoints);
    ctx.font = '600 10px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    for (const marker of crossoverMarkers(ways, activeRole, { frequency: preview })) {
        const hz = marker.frequency_hz;
        const x = xForHzAt(hz);
        // A read-only line (the sub-owned high-pass) stays a faint reference
        // so it is visibly not a handle the user can move.
        ctx.strokeStyle = marker.editable ? 'rgba(255,255,255,0.5)' : 'rgba(255,255,255,0.28)';
        ctx.lineWidth = 1;
        ctx.setLineDash(marker.editable ? [] : [4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        ctx.setLineDash([]);
        const label = formatHz(hz);
        ctx.textAlign = 'center';
        const labelWidth = ctx.measureText(label).width + 12;
        const labelX = Math.max(pad.left + labelWidth / 2 + 2, Math.min(pad.left + plotW - labelWidth / 2 - 2, x));
        const labelY = pad.top + plotH * 0.72;
        ctx.fillStyle = 'rgba(12,18,28,0.82)';
        ctx.fillRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
        ctx.strokeStyle = 'rgba(255,255,255,0.16)';
        ctx.strokeRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
        ctx.fillStyle = marker.editable ? '#e5e7eb' : 'rgba(229,231,235,0.62)';
        ctx.fillText(label, labelX, labelY);
    }
    ctx.textAlign = 'start';
}

    return {
        drawCrossoverResponse,
        crossoverMarkers,
        crossoverGraphFrequencyAt,
        crossoverGraphHandleAt,
    };
});
