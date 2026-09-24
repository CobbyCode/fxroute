// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute crossover response graph painter.
 * Canonical owner of the Crossover/Speaker tile canvas rendering, in the
 * subwoofer-preview visual language. Pure painter: canvas plus response data
 * in, pixels out. Way ordering comes from crossover.js at call time; state,
 * mutations and listeners stay in app.js. Browser-loadable UMD, no build
 * step; Node-testable via require() with a stub canvas.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCrossoverView = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

function drawCrossoverResponse(canvas, ways, activeRole) {
    // Same visual language as the subwoofer preview: dark plot, dB/Hz
    // axes, dimmed ways, highlighted active way, cutoff markers.
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const crossover = (root && root.FXRouteCrossover) || null;
    const ordered = crossover && typeof crossover.orderedWays === 'function' ? crossover.orderedWays(ways || {}) : Object.keys(ways || {});
    const clientWidth = Math.round(canvas.clientWidth || 0);
    // Hidden cards (other tab, closed panel) report no width: skip instead
    // of painting a fallback size that would stick after tab switches.
    if (!clientWidth) return;
    const displayWidth = Math.max(320, clientWidth);
    const displayHeight = Math.max(120, Math.round(canvas.clientHeight || 136));
    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    const targetWidth = Math.round(displayWidth * dpr);
    const targetHeight = Math.round(displayHeight * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = displayWidth;
    const height = displayHeight;
    const pad = { left: 56, right: 24, top: 18, bottom: 24 };
    const plotW = Math.max(1, width - pad.left - pad.right);
    const plotH = Math.max(1, height - pad.top - pad.bottom);
    const minHz = 20;
    const maxHz = 20000;
    const minDb = -30;
    const maxDb = 6;
    const xForHz = (hz) => pad.left + ((Math.log10(Math.max(minHz, hz)) - Math.log10(minHz)) / (Math.log10(maxHz) - Math.log10(minHz))) * plotW;
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
        const x = xForHz(hz);
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
        ctx.fillText(formatTickHz(hz), xForHz(hz), height - 8);
    });
    ctx.textAlign = 'start';
    const drawWay = (role, color, lineWidth) => {
        const entry = (ways || {})[role];
        const points = entry && Array.isArray(entry.points) ? entry.points : null;
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
            const x = xForHz(hz);
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
    drawWay(activeRole, '#60a5fa', 3);
    const activeFilters = ((ways || {})[activeRole] || {}).filters || {};
    const activeDerived = ((ways || {})[activeRole] || {}).derived_highpass || null;
    const markerFrequencies = [];
    ctx.font = '600 10px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    for (const kind of ['highpass', 'lowpass']) {
        const hz = Number(activeFilters[kind] && activeFilters[kind].frequency_hz);
        if (!Number.isFinite(hz) || hz < minHz || hz > maxHz) continue;
        markerFrequencies.push(hz);
        const x = xForHz(hz);
        ctx.strokeStyle = 'rgba(255,255,255,0.35)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        const label = formatHz(hz);
        ctx.textAlign = 'center';
        const labelWidth = ctx.measureText(label).width + 12;
        const labelX = Math.max(pad.left + labelWidth / 2 + 2, Math.min(pad.left + plotW - labelWidth / 2 - 2, x));
        const labelY = pad.top + plotH * 0.72;
        ctx.fillStyle = 'rgba(12,18,28,0.82)';
        ctx.fillRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
        ctx.strokeStyle = 'rgba(255,255,255,0.16)';
        ctx.strokeRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
        ctx.fillStyle = '#e5e7eb';
        ctx.fillText(label, labelX, labelY);
    }
    // Sub-owned Low high-pass: no stored filter, but the running curve
    // includes it, so mark it like a stored cutoff. Skipped when a stored
    // high-pass already marks (near-)the same frequency.
    if (activeDerived && !activeFilters.highpass) {
        const hz = Number(activeDerived.frequency_hz);
        if (Number.isFinite(hz) && hz >= minHz && hz <= maxHz
            && !markerFrequencies.some((marked) => Math.abs(Math.log(marked / hz)) < 0.02)) {
            const x = xForHz(hz);
            ctx.strokeStyle = 'rgba(255,255,255,0.35)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, pad.top);
            ctx.lineTo(x, pad.top + plotH);
            ctx.stroke();
            const label = formatHz(hz);
            ctx.textAlign = 'center';
            const labelWidth = ctx.measureText(label).width + 12;
            const labelX = Math.max(pad.left + labelWidth / 2 + 2, Math.min(pad.left + plotW - labelWidth / 2 - 2, x));
            const labelY = pad.top + plotH * 0.72;
            ctx.fillStyle = 'rgba(12,18,28,0.82)';
            ctx.fillRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
            ctx.strokeStyle = 'rgba(255,255,255,0.16)';
            ctx.strokeRect(labelX - labelWidth / 2, labelY - 9, labelWidth, 18);
            ctx.fillStyle = '#e5e7eb';
            ctx.fillText(label, labelX, labelY);
        }
    }
    ctx.textAlign = 'start';
}

    return {
        drawCrossoverResponse,
    };
});
