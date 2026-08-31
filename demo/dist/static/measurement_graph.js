// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute measurement graph rendering.
 *
 * Owns drawMeasurementGraph, the trace-entry builders and the
 * requestAnimationFrame render coalescing for the measurement assistant.
 * Dependencies (state readers, overlay painters, canvas/panel getters) are
 * injected by app.js via init(); the canvas element is passed into draw(),
 * and the module never touches app.js globals directly.
 */
(function () {
    'use strict';

    const ui = window.FXRouteMeasurementUI || {};
    const dsp = window.FXRouteMeasurementDsp || {};
    const autoSubTarget = window.FXRouteAutoSubTarget || {};

    let deps = {
        getCanvas: () => null,
        getPanel: () => null,
        getMeasurementGraphView: () => 'freq',
        getDisplaySmoothing: () => '1/6-oct',
        getCurrentMeasurementEntries: () => [],
        getVisibleMeasurementEntries: () => [],
        getVisibleMeasurementColorById: () => ({}),
        getMeasurementDisplayTraces: () => [],
        getMeasurementTargetCurvePreview: () => null,
        getAutoSubDisplayReferenceEntries: () => [],
        buildMeasurementIrGraphEntry: () => null,
        drawMeasurementIrGraph: () => {},
        drawMeasurementTargetCurve: () => {},
        drawMeasurementConvolverRangeOverlay: () => {},
        drawMeasurementPeqOverlay: () => {},
        drawCustomHouseCurveHandles: () => {},
    };
    let renderScheduled = false;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function scheduleMeasurementGraphRender() {
        if (renderScheduled) return;
        renderScheduled = true;
        window.requestAnimationFrame(() => {
            renderScheduled = false;
            drawMeasurementGraph(deps.getCanvas());
        });
    }


    function scheduleMeasurementGraphRenderForResize() {
        if (!deps.getPanel() || deps.getPanel().classList.contains('hidden')) return;
        scheduleMeasurementGraphRender();
    }


    function buildMeasurementGraphEntry(measurement = {}, { current = false, graphColor = '' } = {}) {
        const traces = deps.getMeasurementDisplayTraces(measurement);
        if (!traces.length) return null;
        const smoothing = deps.getDisplaySmoothing() || '1/6-oct';
        return {
            ...measurement,
            traces: traces.map((trace) => ({
                ...trace,
                points: dsp.smoothMeasurementTracePoints(trace.points || [], smoothing),
            })),
            current,
            graphColor: graphColor || (current ? ui.measurementCurrentColor : ''),
        };
    }


    function getGraphMeasurementEntries() {
        const entries = [];
        const builder = deps.getMeasurementGraphView() === 'ir' ? deps.buildMeasurementIrGraphEntry : buildMeasurementGraphEntry;
        deps.getCurrentMeasurementEntries().forEach((measurement, index) => {
            const currentEntry = builder(measurement, {
                current: true,
                graphColor: index === 0 ? ui.measurementCurrentColor : ui.measurementComparePalette[index - 1],
            });
            if (currentEntry) entries.push(currentEntry);
        });
        const visibleColorById = deps.getVisibleMeasurementColorById();
        deps.getVisibleMeasurementEntries().forEach((measurement) => {
            const entry = builder(measurement, { current: false, graphColor: visibleColorById[measurement.id] });
            if (entry) entries.push(entry);
        });
        if (deps.getMeasurementGraphView() === 'ir' || typeof autoSubTarget.alignAutoSubEntries !== 'function') {
            return entries;
        }
        const targetPoints = deps.getMeasurementTargetCurvePreview()?.points || [];
        const referenceEntries = deps.getAutoSubDisplayReferenceEntries();
        return autoSubTarget.alignAutoSubEntries(entries, targetPoints, referenceEntries);
    }


    function drawMeasurementGraph(canvas) {
        canvas = canvas || deps.getCanvas();
        const panel = deps.getPanel();
        if (!canvas || panel?.classList.contains('hidden')) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const displaySize = ui.getMeasurementGraphDisplaySize(canvas);
        if (!displaySize.width || !displaySize.height) return;
        const displayWidth = displaySize.width;
        const displayHeight = displaySize.height;
        const dpr = window.devicePixelRatio || 1;
        const targetWidth = Math.round(displayWidth * dpr);
        const targetHeight = Math.round(displayHeight * dpr);
        if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
            canvas.width = targetWidth;
            canvas.height = targetHeight;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, displayWidth, displayHeight);

        ctx.fillStyle = '#161619';
        ctx.fillRect(0, 0, displayWidth, displayHeight);

        const graphEntries = getGraphMeasurementEntries();
        const range = dsp.getMeasurementGraphRange(graphEntries);
        const bounds = ui.getMeasurementGraphBounds(displayWidth, displayHeight);
        if (deps.getMeasurementGraphView() === 'ir') {
            deps.drawMeasurementIrGraph(ctx, bounds, graphEntries);
            ctx.strokeStyle = 'rgba(255,255,255,0.16)';
            ctx.lineWidth = 1;
            ctx.strokeRect(bounds.left, bounds.top, bounds.width, bounds.height);
            return;
        }

        ctx.strokeStyle = 'rgba(255,255,255,0.06)';
        ctx.lineWidth = 1;
        const dbStep = 6;
        for (let db = range.minDb; db <= range.maxDb; db += dbStep) {
            const y = dsp.measurementDbToY(db, bounds, range);
            ctx.beginPath();
            ctx.moveTo(bounds.left, y);
            ctx.lineTo(bounds.left + bounds.width, y);
            ctx.stroke();
            ctx.fillStyle = db === 0 ? '#6ee7b7' : 'rgba(236,236,240,0.65)';
            ctx.font = '11px "Geist Mono", "JetBrains Mono", monospace, sans-serif';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.fillText(`${db} dB`, bounds.left - 8, y);
        }

        [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach(frequency => {
            const x = dsp.measurementFrequencyToX(frequency, bounds);
            ctx.strokeStyle = frequency === 1000 ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.06)';
            ctx.beginPath();
            ctx.moveTo(x, bounds.top);
            ctx.lineTo(x, bounds.top + bounds.height);
            ctx.stroke();
            ctx.fillStyle = 'rgba(236,236,240,0.65)';
            ctx.font = '11px "Geist Mono", "JetBrains Mono", monospace, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(frequency >= 1000 ? `${frequency / 1000}k` : `${frequency}`, x, bounds.top + bounds.height + 8);
        });

        deps.drawMeasurementTargetCurve(ctx, bounds, range);

        graphEntries.forEach(entry => {
            (entry.traces || []).forEach(trace => {
                if (!trace.points.length) return;
                const isReviewTrace = trace.role === 'raw-review';
                const traceColor = entry.graphColor || '#6ee7b7';

                ctx.strokeStyle = traceColor;
                ctx.lineWidth = entry.current ? 2.6 : (isReviewTrace ? 1.6 : 2.0);
                ctx.setLineDash(entry.current ? [] : (isReviewTrace ? [5, 4] : [8, 5]));
                ctx.beginPath();
                trace.points.forEach(([frequency, level], pointIndex) => {
                    const x = dsp.measurementFrequencyToX(frequency, bounds);
                    const y = Math.max(bounds.top, Math.min(bounds.top + bounds.height, dsp.measurementDbToY(level, bounds, range)));
                    if (pointIndex === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                });
                ctx.stroke();
                ctx.setLineDash([]);
            });
        });

        deps.drawMeasurementConvolverRangeOverlay(ctx, bounds);
        deps.drawMeasurementPeqOverlay(ctx, bounds, range);
        deps.drawCustomHouseCurveHandles(ctx, bounds, range);

        ctx.strokeStyle = 'rgba(255,255,255,0.16)';
        ctx.lineWidth = 1;
        ctx.strokeRect(bounds.left, bounds.top, bounds.width, bounds.height);
    }


    window.FXRouteMeasurementGraph = {
        init,
        scheduleMeasurementGraphRender,
        scheduleMeasurementGraphRenderForResize,
        drawMeasurementGraph,
        buildMeasurementGraphEntry,
        getGraphMeasurementEntries,
    };
})();
