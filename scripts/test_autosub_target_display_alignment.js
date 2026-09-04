#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

global.window = global;

const autosubTarget = require(path.join(__dirname, '..', 'static', 'autosub_target.js'));
window.FXRouteAutoSubTarget = autosubTarget;
const measurementUi = require(path.join(__dirname, '..', 'static', 'measurement_ui.js'));

const neutralTarget = [[20, 0], [20000, 0]];
const runTarget = [[20, 6], [20000, 6]];
const mainReferencePoints = {
    left: [[120, 4], [1000, 4], [8000, 4]],
    right: [[120, 4], [1000, 4], [8000, 4]],
};
const autosubMeta = {
    target: { key: 'run-target', label: 'Run Target', points: runTarget },
    target_vertical_offset_db: -2,
    main_reference_points: mainReferencePoints,
};

function savedAutoSubEntry(id, label, displayOffsetDb, levels, meta = autosubMeta) {
    return {
        id,
        measurement_kind: 'auto_sub',
        autosub_meta: meta,
        traces: [{
            label,
            display_offset_db: displayOffsetDb,
            points: [[20, levels[0]], [100, levels[1]], [500, levels[2]]],
        }],
    };
}

// Target selection must never move traces. Narrow, single-point and custom
// targets all resolve through the same fixed Neutral reference.
{
    const entry = savedAutoSubEntry('before-l', 'Before L', 10, [-2, 0, 1]);
    const neutralAligned = autosubTarget.alignAutoSubEntries([entry], neutralTarget);
    assert.deepEqual(neutralAligned[0].traces[0].points, [[20, 4], [100, 6], [500, 7]]);

    const narrowAligned = autosubTarget.alignAutoSubEntries([entry], [[500, 0], [1000, 2]]);
    assert.deepEqual(narrowAligned[0].traces[0].points, neutralAligned[0].traces[0].points,
        'narrow target must not move traces');

    const onePointAligned = autosubTarget.alignAutoSubEntries([entry], [[1000, 1]]);
    assert.deepEqual(onePointAligned[0].traces[0].points, neutralAligned[0].traces[0].points,
        'single-point target must not move traces');

    const nullAligned = autosubTarget.alignAutoSubEntries([entry], null);
    assert.deepEqual(nullAligned[0].traces[0].points, neutralAligned[0].traces[0].points,
        'graph path passes no target; traces must stay identical');
}

// Built-in shaped targets keep their shape; the AutoSub set receives one
// constant vertical shift that is identical for every target.
for (const key of ['neutral', 'bk', 'harman']) {
    const targetPoints = measurementUi.measurementConvolverCurves[key].points;
    const targetSnapshot = targetPoints.map(point => [...point]);
    const entry = savedAutoSubEntry(`${key}-before`, 'Before L', 10, [-2, 0, 1]);
    const aligned = autosubTarget.alignAutoSubEntries([entry], targetPoints);
    const shifts = aligned[0].traces[0].points.map((point, index) => (
        point[1] - entry.traces[0].points[index][1]
    ));
    assert.ok(shifts.every(shift => Math.abs(shift - shifts[0]) < 1e-9),
        `${key} must apply one constant shift to the AutoSub trace`);
    assert.deepEqual(targetPoints, targetSnapshot, `${key} target shape must stay unchanged`);
}

// Regression: a bass-heavy Main previously moved 2.5 dB between Neutral and
// Harman (1.5 dB for BK) with no new sweep. All three must now agree exactly.
{
    const freqs = [120, 200, 500, 1000, 2000, 5000, 8000];
    const bassHeavy = [5, 5, 0, 0, 0, 0, 0];
    const heavyMeta = {
        target: { key: 'run-target', label: 'Run Target', points: runTarget },
        main_reference_points: {
            left: freqs.map((f, i) => [f, bassHeavy[i]]),
            right: freqs.map((f, i) => [f, bassHeavy[i]]),
        },
    };
    const entry = savedAutoSubEntry('heavy-before', 'Before L', 10, [-2, 0, 1], heavyMeta);
    const byTarget = {};
    for (const key of ['neutral', 'harman', 'bk']) {
        const pts = measurementUi.measurementConvolverCurves[key].points;
        byTarget[key] = autosubTarget.alignAutoSubEntries([entry], pts)[0].traces[0].points;
    }
    assert.deepEqual(byTarget.harman, byTarget.neutral,
        'Harman selection must not move traces (was -2.5 dB)');
    assert.deepEqual(byTarget.bk, byTarget.neutral,
        'BK selection must not move traces (was -1.5 dB)');
}

// Two runs with different Mains previously drifted relative to each other on
// target switch (flat vs bass-heavy: 0.0 dB at Neutral, -2.5 dB at Harman).
// Their relative distance must now be target-independent.
{
    const freqs = [120, 200, 500, 1000, 2000, 5000, 8000];
    const flat = [0, 0, 0, 0, 0, 0, 0];
    const bass = [5, 5, 0, 0, 0, 0, 0];
    const metaFor = (arr) => ({
        main_reference_points: {
            left: freqs.map((f, i) => [f, arr[i]]),
            right: freqs.map((f, i) => [f, arr[i]]),
        },
    });
    const runA = savedAutoSubEntry('run-a', 'Before', 10, [0, 0, 0], metaFor(flat));
    const runB = savedAutoSubEntry('run-b', 'Before', 10, [0, 0, 0], metaFor(bass));
    const relByTarget = {};
    for (const key of ['neutral', 'harman', 'bk']) {
        const pts = measurementUi.measurementConvolverCurves[key].points;
        const [a, b] = autosubTarget.alignAutoSubEntries([runA, runB], pts);
        relByTarget[key] = b.traces[0].points[0][1] - a.traces[0].points[0][1];
    }
    assert.equal(relByTarget.harman, relByTarget.neutral,
        'inter-run distance must not depend on target');
    assert.equal(relByTarget.bk, relByTarget.neutral,
        'inter-run distance must not depend on target');
}

// Saved split traces from one run must move as one rigid set into the fixed
// normal coordinate. Median display offset 10 minus Neutral anchor 4 gives a
// +6 dB trace shift, while the Neutral target itself remains at 0 dB.
{
    const entries = [
        savedAutoSubEntry('before-l', 'Before L', 7, [-2, 0, 1]),
        savedAutoSubEntry('before-r', 'Before R', 9, [-1, 1, 2]),
        savedAutoSubEntry('after-l', 'After L', 11, [0, 3, 4]),
        savedAutoSubEntry('after-r', 'After R', 13, [1, 4, 5]),
    ];
    const aligned = autosubTarget.alignAutoSubEntries(entries, neutralTarget);

    assert.deepEqual(aligned.map(entry => entry.traces[0].points), [
        [[20, 4], [100, 6], [500, 7]],
        [[20, 5], [100, 7], [500, 8]],
        [[20, 6], [100, 9], [500, 10]],
        [[20, 7], [100, 10], [500, 11]],
    ]);
    assert.deepEqual(neutralTarget, [[20, 0], [20000, 0]],
        'the fixed reference stays in the normal graph coordinate');

    for (let pointIndex = 0; pointIndex < 3; pointIndex += 1) {
        const beforeDifference = entries[2].traces[0].points[pointIndex][1]
            - entries[0].traces[0].points[pointIndex][1];
        const afterDifference = aligned[2].traces[0].points[pointIndex][1]
            - aligned[0].traces[0].points[pointIndex][1];
        assert.equal(afterDifference, beforeDifference,
            'Before/After relative differences must remain unchanged');
    }
    assert.deepEqual(entries[0].traces[0].points, [[20, -2], [100, 0], [500, 1]],
        'alignment must not mutate saved measurements');

    const beforeOnly = autosubTarget.alignAutoSubEntries([entries[0]], neutralTarget, entries);
    assert.deepEqual(beforeOnly[0].traces[0].points, [[20, 4], [100, 6], [500, 7]],
        'hiding sibling traces from the run must not change the display coordinate');
}

// The stored run target remains metadata only and never moves traces. The
// currently selected target owns shape alone.
{
    const entry = savedAutoSubEntry('before-l', 'Before L', 10, [-2, 0, 1]);
    const currentCustomTarget = [[20, 1], [20000, 1]];
    const neutralAligned = autosubTarget.alignAutoSubEntries([entry], neutralTarget);
    const customAligned = autosubTarget.alignAutoSubEntries([entry], currentCustomTarget);

    assert.deepEqual(neutralAligned[0].traces[0].points, [[20, 4], [100, 6], [500, 7]]);
    assert.deepEqual(customAligned[0].traces[0].points, neutralAligned[0].traces[0].points,
        'changing the current target must not move traces; only the target line changes');
    assert.notDeepEqual(currentCustomTarget, runTarget,
        'the fixture must use a current target different from the stored run target');
    assert.equal(autosubTarget.resolveTargetCurve, undefined,
        'stored AutoSub target remains information only');
}

// Normal measurements are already in the canonical graph coordinate and are
// untouched, including in a mixed normal/AutoSub graph.
{
    const normal = {
        id: 'normal',
        measurement_kind: '',
        traces: [{ label: 'Normal', points: [[20, -3], [100, 0], [500, 2]] }],
    };
    const autosub = savedAutoSubEntry('before-l', 'Before L', 10, [-2, 0, 1]);
    const aligned = autosubTarget.alignAutoSubEntries([normal, autosub], neutralTarget);
    assert.deepEqual(aligned[0], normal);
    assert.deepEqual(aligned[1].traces[0].points, [[20, 4], [100, 6], [500, 7]]);
}

// Incomplete metadata has no alternate or compatibility display path.
{
    const incomplete = {
        id: 'old-autosub',
        measurement_kind: 'auto_sub',
        traces: [{ label: 'Before L', points: [[20, -3], [100, -1], [500, 0]] }],
    };
    assert.deepEqual(autosubTarget.alignAutoSubEntries([incomplete], neutralTarget), [incomplete]);
    assert.deepEqual(autosubTarget.alignAutoSubEntries([], neutralTarget), []);
}

// Source-level integration contract: graph entries are aligned without the
// selected target; target drawing stays raw and owns shape alone.
{
    const graphSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_graph.js'), 'utf8');
    const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
    assert.ok(graphSource.includes('alignAutoSubEntries(entries, null, referenceEntries)'),
        'measurement graph must align AutoSub traces without the selected target');
    assert.ok(!graphSource.includes('getMeasurementTargetCurvePreview()?.points'),
        'trace alignment must not read the current UI target');
    assert.ok(appSource.includes('getAutoSubDisplayReferenceEntries,'),
        'graph alignment must receive hidden sibling traces from saved AutoSub runs');
    assert.ok(!appSource.includes('resolveTargetOffsetDb(entries, points)'),
        'target drawing must not move into an AutoSub-only coordinate');
    assert.ok(!appSource.includes('shiftTargetPoints(points, offsetDb)'),
        'target points must remain in the normal graph coordinate');
    assert.ok(appSource.includes('const curve = getMeasurementTargetCurvePreview();'),
        'the graph must continue to draw the current UI target');

    const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
    assert.ok(/autosub_target\.js\?v=\d+\.\d+\.\d+/.test(html));
    assert.ok(/measurement_graph\.js\?v=\d+\.\d+\.\d+/.test(html));
    assert.ok(/app\.js\?v=\d+\.\d+\.\d+/.test(html));
}

console.log('PASS test_autosub_target_display_alignment.js');
