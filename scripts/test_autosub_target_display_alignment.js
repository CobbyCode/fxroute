#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// AutoSub Target-Curve vertical alignment tests.
//
// Coordinate systems (verified against real .104 run data):
//   raw          = sweep analysis output before normalization
//   normalized   = raw - normalized_by_db            (per sweep!)
//   displayed    = normalized + anchor_shift - shared_offset
//   calibrated   = raw = normalized + normalized_by_db
//
// Scoring/Gain work in the CALIBRATED coordinate: the target is placed at
// (target + tvo), where tvo = job.main_target_anchor.target_vertical_offset_db.
//
// New runs embed exact metadata:
//   - autosub_meta.target_vertical_offset_db  (run's tvo)
//   - trace.display_offset_db                 (nb - anchor_shift + shared)
// and the exact displayed target position is:
//   target_displayed_db = target + tvo - display_offset_db
//
// Tests:
//  1. Exact path: resolveTargetOffsetDb returns display_offset_db - tvo from
//     embedded run metadata.
//  2. Missing exact metadata does not create a compatibility fallback.
//  3. The offset is derived only when 'auto_sub' measurements are on screen
//     (normal measurement graphs are untouched).
//  4. Shifting the target preserves its shape exactly (constant dB offset).
//  5. app.js draws the target from the shifted points (source-level contract).
//  6. End-to-end math on a real AutoSub job snapshot pulled from .104.
//  7. Saved run metadata never overrides the current graph Target Curve.

const assert = require('assert/strict');
const path = require('path');
const fs = require('fs');

global.window = global;

const autosubTarget = require(path.join(__dirname, '..', 'static', 'autosub_target.js'));
window.FXRouteAutoSubTarget = autosubTarget;

// ---------------------------------------------------------------------------
// 1. EXACT path: tvo - display_offset_db from embedded run metadata
// ---------------------------------------------------------------------------
{
    const currentTarget = [[20, 0], [20000, 0]];
    const entries = [{
        id: 'a1', measurement_kind: 'auto_sub',
        autosub_meta: {
            target: { points: [[20, 6], [20000, 6]] },
            target_vertical_offset_db: -32.6385,
            main_reference_points: {
                left: [[120, -26.6385], [1000, -26.6385], [8000, -26.6385]],
                right: [[120, -26.6385], [1000, -26.6385], [8000, -26.6385]],
            },
        },
        traces: [
            { label: 'Before L', display_offset_db: -75.466, points: [[20, -0.5], [100, 0], [500, 1]] },
            { label: 'Before R', display_offset_db: -73.893, points: [[20, 1.2], [100, 0], [500, 1]] },
        ],
    }];
    const off = autosubTarget.resolveTargetOffsetDb(entries, currentTarget);
    // shift value = display_offset_db - tvo = -75.466 - (-26.6385) = -48.8275
    assert.equal(off, (-75.466) - (-26.6385));
    // displayed target = target - (display_offset_db - tvo)
    //   = target + tvo - display_offset_db  (exact scored position)
    const target = [[20, 0], [100, -3], [1000, -6]];
    const displayed = autosubTarget.shiftTargetPoints(target, off);
    // Check the scored relation reproduced in display coordinates:
    // displayed_trace = calibrated - display_offset_db
    // displayed_target(20Hz) = target(20) + tvo - display_offset_db
    //   = 0 + (-26.6385) + 75.466 = 48.8275
    assert.equal(displayed[0][1], 0 + (-26.6385) - (-75.466));

    const runTargetOffset = autosubTarget.resolveTargetOffsetDb(
        entries, entries[0].autosub_meta.target.points,
    );
    assert.equal(runTargetOffset, (-75.466) - (-32.6385));
    assert.notEqual(off, runTargetOffset,
        'switching target shape must recompute its vertical anchor from the saved Main references');
}

// ---------------------------------------------------------------------------
// 2. No fallback for obsolete AutoSub metadata shapes
// ---------------------------------------------------------------------------
{
    const incomplete = {
        id: 'a1', measurement_kind: 'auto_sub',
        traces: [{ label: 'Before L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.equal(autosubTarget.resolveTargetOffsetDb([incomplete]), null,
        'obsolete runs without exact display metadata must not create a parallel target path');
}

// ---------------------------------------------------------------------------
// 3. Saved AutoSub metadata does not expose a graph Target Curve override
// ---------------------------------------------------------------------------
{
    const runTarget = {
        key: 'house:run-target',
        label: 'Run Target',
        provenance: 'uploaded',
        points: [[20, 6], [100, 2], [20000, -4]],
    };
    const saved = {
        id: 'saved-a1', measurement_kind: 'auto_sub',
        autosub_meta: { target: runTarget },
        traces: [{ points: [[20, 0], [100, 1], [20000, -2]] }],
    };
    assert.equal(autosubTarget.resolveTargetCurve, undefined,
        'stored AutoSub target remains summary metadata, not graph selection state');
    assert.equal(saved.autosub_meta.target.label, 'Run Target');
}

// ---------------------------------------------------------------------------
// 5. Offset is derived only for auto_sub measurements
// ---------------------------------------------------------------------------
{
    const normal = {
        id: 'm1', measurement_kind: '',
        traces: [{ label: 'L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.equal(autosubTarget.resolveTargetOffsetDb([normal]), null,
        'normal measurement graphs must not shift the target curve');
    assert.equal(autosubTarget.resolveTargetOffsetDb([]), null);
}

// ---------------------------------------------------------------------------
// 6. Shape preservation (constant offset only)
// ---------------------------------------------------------------------------
{
    const raw = [[20, 5], [80, 3], [1000, 0], [20000, -5]];
    const shifted = autosubTarget.shiftTargetPoints(raw, -2.5);
    assert.deepEqual(shifted, [[20, 7.5], [80, 5.5], [1000, 2.5], [20000, -2.5]]);
    for (let i = 0; i < raw.length; i++) {
        assert.equal(shifted[i][1] - raw[i][1], 2.5, 'constant offset per point');
    }
    // null offset -> untouched
    assert.deepEqual(autosubTarget.shiftTargetPoints(raw, null), raw);
}

// ---------------------------------------------------------------------------
// 7. app.js draw path contract
// ---------------------------------------------------------------------------
{
    const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
    assert.ok(source.includes('window.FXRouteAutoSubTarget.resolveTargetOffsetDb(entries, points)'),
        'drawMeasurementTargetCurve must resolve the target offset from the graph entries');
    assert.ok(source.includes('const curve = getMeasurementTargetCurvePreview();'),
        'drawMeasurementTargetCurve must use the current UI Target Curve');
    assert.ok(!source.includes('window.FXRouteAutoSubTarget.resolveTargetCurve(entries)'),
        'saved AutoSub metadata must not override the current UI Target Curve');
    assert.ok(source.includes('shiftTargetPoints(points, offsetDb)'),
        'drawMeasurementTargetCurve must draw the target from the shifted points');
    assert.ok(source.includes('getMeasurementConvolverCurveDbFromPoints(displayPoints, frequency)'),
        'the plotted level must come from the shifted display points');
    // no fixed dB constants introduced
    assert.ok(!/displayPoints\s*=.*[+\-]=?\s*\d+(\.\d+)?\s*;/.test(source),
        'no fixed dB offsets in the target display path');
    // index.html must load the module
    const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
    assert.ok(/autosub_target\.js\?v=\d+\.\d+\.\d+/.test(html),
        'index.html must load autosub_target.js with a versioned query string');
}

// ---------------------------------------------------------------------------
// 8. Real AutoSub job snapshot from .104 (end-to-end coordinate check)
// ---------------------------------------------------------------------------
{
    const fixturePath = path.join(__dirname, 'fixtures', 'autosub-job-22s-sample.json');
    if (fs.existsSync(fixturePath)) {
        const job = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
        const result = job.result;
        const anchor = result.main_target_anchor || {};
        assert.equal(anchor.status, 'ready', 'fixture must be an anchored run');

        // Old fixture: no display_offset_db on traces and no tvo in meta.
        const measurements = [result.baseline_measurement, result.confirmation_measurement]
            .filter(Boolean);
        for (const m of measurements) m.measurement_kind = 'auto_sub';

        // Development snapshots without the current exact metadata are not
        // supported by a separate graph path.
        const offsetDb = autosubTarget.resolveTargetOffsetDb(measurements);
        assert.equal(offsetDb, null);

        // Simulate a current run: inject the exact metadata the backend embeds
        // and verify the exact scored-position path.
        const tvo = anchor.target_vertical_offset_db;
        const m0 = measurements[0];
        m0.autosub_meta = {
            target_vertical_offset_db: tvo,
            main_reference_points: Object.fromEntries(
                ['left', 'right'].map(side => [side, anchor.sides[side].aligned_points.map(point => point.slice(0, 2))]),
            ),
        };
        m0.traces[0].display_offset_db = -75.466; // nb + shift + shared (L)
        const exactOff = autosubTarget.resolveTargetOffsetDb(measurements, result.target_curve.points);
        assert.equal(exactOff, (-75.466) - tvo,
            'new runs must resolve the exact scored offset (display_offset_db - tvo)');
    } else {
        console.log('SKIP fixture-based end-to-end check (no fixture present)');
    }
}

console.log('PASS test_autosub_target_display_alignment.js');
