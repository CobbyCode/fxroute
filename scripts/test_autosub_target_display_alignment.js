#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// AutoSub Target-Curve vertical alignment tests.
//
// Coordinate systems (verified against real .104 run data):
//   raw          = sweep analysis output before normalization
//   normalized   = raw - normalized_by_db            (per sweep!)
//   displayed    = normalized - anchor_shift - shared_offset
//   calibrated   = raw = normalized + normalized_by_db
//
// Scoring/Gain work in the CALIBRATED coordinate: the target is placed at
// (target + tvo), where tvo = job.main_target_anchor.target_vertical_offset_db.
//
// New runs embed exact metadata:
//   - autosub_meta.target_vertical_offset_db  (run's tvo)
//   - trace.display_offset_db                 (nb + anchor_shift + shared)
// and the exact displayed target position is:
//   target_displayed_db = target + tvo - display_offset_db
//
// Tests:
//  1. Exact path: resolveTargetOffsetDb returns tvo - display_offset_db from
//     embedded run metadata.
//  2. Legacy fallback: bass-median shared reference when no metadata exists.
//  3. The offset is derived only when 'auto_sub' measurements are on screen
//     (normal measurement graphs are untouched).
//  4. Shifting the target preserves its shape exactly (constant dB offset).
//  5. app.js draws the target from the shifted points (source-level contract).
//  6. End-to-end math on a real AutoSub job snapshot pulled from .104.

const assert = require('assert/strict');
const path = require('path');
const fs = require('fs');

global.window = global;

const autosubTarget = require(path.join(__dirname, '..', 'static', 'autosub_target.js'));
window.FXRouteAutoSubTarget = autosubTarget;

// ---------------------------------------------------------------------------
// 1. Bass-median parity with the backend helper (legacy fallback basis)
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
// 2. EXACT path: tvo - display_offset_db from embedded run metadata
// ---------------------------------------------------------------------------
{
    const entries = [{
        id: 'a1', measurement_kind: 'auto_sub',
        autosub_meta: { target_vertical_offset_db: -26.6385 },
        traces: [
            { label: 'Before L', display_offset_db: -75.466, points: [[20, -0.5], [100, 0], [500, 1]] },
            { label: 'Before R', display_offset_db: -73.893, points: [[20, 1.2], [100, 0], [500, 1]] },
        ],
    }];
    const off = autosubTarget.resolveTargetOffsetDb(entries);
    // tvo - display_offset_db = -26.6385 - (-75.466) = 48.8275
    assert.equal(off, -26.6385 - (-75.466));
    // displayed target = target + off  (shiftTargetPoints subtracts off)
    const target = [[20, 0], [100, -3], [1000, -6]];
    const displayed = autosubTarget.shiftTargetPoints(target, off);
    // Check the scored relation reproduced in display coordinates:
    // displayed_trace = calibrated - display_offset_db
    // displayed_target(20Hz) = target(20) - (tvo - display_offset_db)
    //   = 0 - (-26.6385 + 75.466) = -48.8275
    assert.equal(displayed[0][1], 0 - ((-26.6385) - (-75.466)));
}

// ---------------------------------------------------------------------------
// 3. LEGACY fallback: bass-median shared reference when metadata is absent
// ---------------------------------------------------------------------------
{
    const legacy = {
        id: 'a1', measurement_kind: 'auto_sub',
        traces: [{ label: 'Before L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.equal(autosubTarget.resolveTargetOffsetDb([legacy]), -2,
        'legacy runs must fall back to the shared bass median');
}

// ---------------------------------------------------------------------------
// 4. Offset is derived only for auto_sub measurements
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
// 5. Shape preservation (constant offset only)
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
// 6. app.js draw path contract
// ---------------------------------------------------------------------------
{
    const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
    assert.ok(source.includes('window.FXRouteAutoSubTarget.resolveTargetOffsetDb(entries)'),
        'drawMeasurementTargetCurve must resolve the target offset from the graph entries');
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
// 7. Real AutoSub job snapshot from .104 (end-to-end coordinate check)
// ---------------------------------------------------------------------------
{
    const fixturePath = path.join(__dirname, 'fixtures', 'autosub-job-22s-sample.json');
    if (fs.existsSync(fixturePath)) {
        const job = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
        const result = job.result;
        const anchor = result.main_target_anchor || {};
        assert.equal(anchor.status, 'ready', 'fixture must be an anchored run');

        // Legacy run: no display_offset_db on traces, no tvo in meta.
        const measurements = [result.baseline_measurement, result.confirmation_measurement]
            .filter(Boolean);
        for (const m of measurements) m.measurement_kind = 'auto_sub';

        // Without exact metadata the module must use the legacy bass fallback.
        const offsetDb = autosubTarget.resolveTargetOffsetDb(measurements);
        assert.ok(Number.isFinite(offsetDb), 'offset must be derived from the displayed traces');

        const target = result.target_curve;
        const displayed = autosubTarget.shiftTargetPoints(target.points, offsetDb);

        // Shape preservation on the real curve
        for (let i = 0; i < target.points.length; i++) {
            assert.equal(
                displayed[i][1] - target.points[i][1],
                -(offsetDb),
                'constant offset per point on the real target curve',
            );
        }

        // Legacy semantics: displayed target bass median = raw median - offset,
        // i.e. the target sits on the same shared vertical reference as the
        // displayed traces (bass median basis).
        const bassMedian = (pts) => {
            const v = pts.filter(([hz]) => hz >= 20 && hz <= 200).map(([, db]) => db).sort((a, b) => a - b);
            const n = v.length;
            return n ? (n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2) : null;
        };
        assert.equal(bassMedian(displayed), bassMedian(target.points) - offsetDb);

        // Simulate a NEW run: inject the exact metadata the new backend embeds
        // and verify the exact scored-position path.
        const tvo = anchor.target_vertical_offset_db;
        const m0 = measurements[0];
        m0.autosub_meta = { target_vertical_offset_db: tvo };
        m0.traces[0].display_offset_db = -75.466; // nb + shift + shared (L)
        const exactOff = autosubTarget.resolveTargetOffsetDb(measurements);
        assert.equal(exactOff, tvo - (-75.466),
            'new runs must resolve the exact scored offset (tvo - display_offset_db)');
    } else {
        console.log('SKIP fixture-based end-to-end check (no fixture present)');
    }
}

console.log('PASS test_autosub_target_display_alignment.js');
