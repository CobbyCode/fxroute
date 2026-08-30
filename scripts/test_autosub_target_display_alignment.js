#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// AutoSub Target-Curve vertical alignment tests.
//
// Verifies that the Target Curve is drawn on the same vertical reference as
// the AutoSub Before/After measurement traces:
//  1. The offset module computes the same 20-200 Hz bass median as the
//     backend's _auto_sub_shared_bass_offset.
//  2. The offset is derived only when 'auto_sub' measurements are on screen
//     (normal measurement graphs are untouched).
//  3. Shifting the target preserves its shape exactly (constant dB offset).
//  4. app.js draws the target from the shifted points when an AutoSub set is
//     displayed (source-level contract, mirrors test_measurement_graph_contract).
//  5. End-to-end math on a real AutoSub job snapshot pulled from .104
//     (2.2-stereo run, anchor status ready): the displayed target bass median
//     must equal the raw target bass median minus the shared display offset.

const assert = require('assert/strict');
const path = require('path');
const fs = require('fs');

global.window = global;

const autosubTarget = require(path.join(__dirname, '..', 'static', 'autosub_target.js'));
window.FXRouteAutoSubTarget = autosubTarget;

// ---------------------------------------------------------------------------
// 1. Bass-median parity with the backend helper
// ---------------------------------------------------------------------------
{
    const points = [[20, -10], [100, -5], [300, -2]];
    // backend: sorted [-10, -5] -> median -7.5 (300 Hz is outside the band)
    assert.equal(autosubTarget.bassMedianDb([points]), -7.5);
    // even count averages the middle pair
    assert.equal(autosubTarget.bassMedianDb([[[20, 0], [80, 2], [150, 4], [300, 9]]]), 2);
    // out-of-band only -> null
    assert.equal(autosubTarget.bassMedianDb([[[500, 1], [600, 2]]]), null);
}

// ---------------------------------------------------------------------------
// 2. Offset is derived only for auto_sub measurements
// ---------------------------------------------------------------------------
{
    const normal = {
        id: 'm1', measurement_kind: '',
        traces: [{ label: 'L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.equal(autosubTarget.getAutoSubDisplayOffsetDb([normal]), null,
        'normal measurement graphs must not shift the target curve');
    assert.equal(autosubTarget.getAutoSubDisplayOffsetDb([]), null);

    const autoSub = {
        id: 'a1', measurement_kind: 'auto_sub',
        traces: [{ label: 'Before L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.equal(autosubTarget.getAutoSubDisplayOffsetDb([autoSub]), -2);
}

// ---------------------------------------------------------------------------
// 3. Shape preservation (constant offset only)
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
// 4. app.js draw path contract
// ---------------------------------------------------------------------------
{
    const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
    assert.ok(source.includes('window.FXRouteAutoSubTarget.getAutoSubDisplayOffsetDb(entries)'),
        'drawMeasurementTargetCurve must derive the AutoSub display offset from the graph entries');
    assert.ok(source.includes('shiftTargetPoints(points, offsetDb)'),
        'drawMeasurementTargetCurve must draw the target from the shifted points');
    assert.ok(source.includes('getMeasurementConvolverCurveDbFromPoints(displayPoints, frequency)'),
        'the plotted level must come from the shifted display points');
    // no fixed dB constants introduced
    assert.ok(!/displayPoints\s*=.*[+\-]=?\s*\d+(\.\d+)?\s*;/.test(source),
        'no fixed dB offsets in the target display path');
}

// ---------------------------------------------------------------------------
// 5. Real AutoSub job snapshot from .104 (end-to-end coordinate check)
// ---------------------------------------------------------------------------
{
    const fixturePath = path.join(__dirname, 'fixtures', 'autosub-job-22s-sample.json');
    if (fs.existsSync(fixturePath)) {
        const job = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
        const result = job.result;
        const anchor = result.main_target_anchor || {};
        assert.equal(anchor.status, 'ready', 'fixture must be an anchored run');

        const measurements = [result.baseline_measurement, result.confirmation_measurement]
            .filter(Boolean);
        for (const m of measurements) m.measurement_kind = 'auto_sub';

        const offsetDb = autosubTarget.getAutoSubDisplayOffsetDb(measurements);
        assert.ok(Number.isFinite(offsetDb), 'offset must be derived from the displayed traces');

        const target = result.target_curve;
        assert.equal(target.label, 'Neutral');
        const displayed = autosubTarget.shiftTargetPoints(target.points, offsetDb);

        // Shape preservation on the real curve
        for (let i = 0; i < target.points.length; i++) {
            assert.equal(
                displayed[i][1] - target.points[i][1],
                -(offsetDb),
                'constant offset per point on the real target curve',
            );
        }

        // Same coordinate system: the displayed target bass median equals the
        // measured traces' bass median basis (raw target level - offset).
        const bassMedian = (pts) => {
            const v = pts.filter(([hz]) => hz >= 20 && hz <= 200).map(([, db]) => db).sort((a, b) => a - b);
            const n = v.length;
            return n ? (n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2) : null;
        };
        const targetDisplayedMedian = bassMedian(displayed);
        const measuredMedian = bassMedian(measurements[0].traces[0].points);
        assert.equal(targetDisplayedMedian, target.points[0][1] - offsetDb);
        assert.ok(Number.isFinite(measuredMedian));
        // The backend normalization subtracted offsetDb from raw sweeps, so the
        // displayed measured median and displayed target share the reference:
        // displayed target = raw target - offsetDb, exactly like displayed traces.
        assert.equal(bassMedian(displayed), bassMedian(target.points) - offsetDb);
    } else {
        console.log('SKIP fixture-based end-to-end check (no fixture present)');
    }
}

console.log('PASS test_autosub_target_display_alignment.js');
