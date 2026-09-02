#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// AutoSub saved-measurement summary line tests.
//
// Verifies that:
// 1. AutoSub saved measurements (measurement_kind === 'auto_sub') show a
//    "Target: <label> · Sub 1 +X.X dB · Sub 2 −X.X dB" style line instead of
//    the generic acoustic timing line.
// 2. The target label always comes from the measurement itself (autosub_meta),
//    never from the currently selected UI target curve.
// 3. Old measurements without autosub_meta simply omit the Target part.
// 4. Normal (non-AutoSub) measurements keep the acoustic timing line.

const assert = require('assert/strict');
const MeasurementUI = require('../static/measurement_ui.js');

const TARGET_HARMAN = { key: 'harman', label: 'Harman-style' };
const TARGET_CUSTOM = { key: 'house:my-curve-123', label: 'My Studio Curve' };

function makeMeasurement(overrides = {}) {
    return Object.assign({
        id: 'm1',
        name: 'AutoSub After',
        channel: 'left',
        measurement_kind: 'auto_sub',
        traces: [{ kind: 'measured', label: 'After L', role: 'left', points: [[20, 0], [100, 1], [20000, -2]] }],
        analysis: {},
    }, overrides);
}

function makeAutoSubMeta() {
    return {
        target: { label: 'Harman-style' },
        final_gains_db: { sub1: 1.14, sub2: -0.62 },
    };
}

// ---------------------------------------------------------------------------
// 1. AutoSub measurement with meta -> Target + Sub gains line
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({ autosub_meta: makeAutoSubMeta() });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.equal(info.status, 'autosub');
    assert.match(info.line, /^Target: Harman-style · Sub 1 \+1\.1 dB(?: · [^·]+ ms(?: · [NI])?)? · Sub 2 −0\.6 dB(?: · [^·]+ ms(?: · [NI])?)?$/);
    assert.match(info.detail, /AutoSub result metadata/);
    assert.ok(!/timing/i.test(info.line), 'no timing wording for autosub');
}

// ---------------------------------------------------------------------------
// 2. Custom/house curve label is used verbatim (no derivation from UI state)
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({
        autosub_meta: { target: TARGET_CUSTOM, final_gains_db: { sub1: -0.94, sub2: -4.02 } },
    });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.match(info.line, /^Target: My Studio Curve · Sub 1 −0\.9 dB · Sub 2 −4\.0 dB$/);
    assert.ok(!info.line.includes('ms'), 'no ms for old meta without delays');
}

// ---------------------------------------------------------------------------
// 3. Old AutoSub measurement without meta -> falls back to plain timing line
// ---------------------------------------------------------------------------
{
    const info = MeasurementUI.getMeasurementTimingInfo(makeMeasurement({ autosub_meta: null }));
    assert.equal(info.status, 'acoustic-only');
    assert.match(info.line, /Acoustic-only timing/);
}

// ---------------------------------------------------------------------------
// 4. Meta with target but no gains -> target only, no dangling separator
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({
        autosub_meta: { target: TARGET_HARMAN, final_gains_db: null },
    });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.match(info.line, /^Target: Harman-style$/);
    assert.ok(!info.line.includes('Sub 1'), 'no Sub 1 text when no gains');
}

// ---------------------------------------------------------------------------
// 5. Meta with gains but no target -> gains only
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({
        autosub_meta: { target: null, final_gains_db: { sub1: 0.5 } },
    });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.match(info.line, /^Sub 1 \+0\.5 dB$/);
    assert.ok(!info.line.includes('Target:'));
}

// ---------------------------------------------------------------------------
// 6. Normal (non-AutoSub) measurement keeps the acoustic timing line
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({ measurement_kind: '', autosub_meta: makeAutoSubMeta() });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.equal(info.status, 'acoustic-only');
    assert.match(info.line, /Acoustic-only timing/);
}

// ---------------------------------------------------------------------------
// 7. Normal measurement WITH stray autosub_meta must still ignore the meta
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({
        measurement_kind: 'sweep-response-v3',
        autosub_meta: makeAutoSubMeta(),
    });
    const info = MeasurementUI.getMeasurementTimingInfo(measurement);
    assert.equal(info.status, 'acoustic-only');
    assert.ok(!info.line.includes('Target:'), 'meta must be ignored for non-autosub kinds');
}

// ---------------------------------------------------------------------------
// 8. normalizeMeasurementEntry passes autosub_meta through to the UI state
// ---------------------------------------------------------------------------
{
    const measurement = makeMeasurement({ autosub_meta: makeAutoSubMeta() });
    const normalized = MeasurementUI.normalizeMeasurementEntry(measurement, 0);
    assert.deepEqual(normalized.autosub_meta, makeAutoSubMeta());
}

// ---------------------------------------------------------------------------
// 9. Rounding: 1.14 -> +1.1, -0.62 -> −0.6 (U+2212), zero -> 0.0 without sign
// ---------------------------------------------------------------------------
{
    const info = MeasurementUI.getMeasurementTimingInfo(makeMeasurement({
        autosub_meta: { target: TARGET_HARMAN, final_gains_db: { sub1: 1.14, sub2: -0.62, sub: 0.02 } },
    }));
    assert.match(info.line, /Sub 1 \+1\.1 dB · Sub 2 −0\.6 dB/);
    const zeroInfo = MeasurementUI.getMeasurementTimingInfo(makeMeasurement({
        autosub_meta: { target: TARGET_HARMAN, final_gains_db: { sub1: 0.0 } },
    }));
    assert.match(zeroInfo.line, /Sub 1 0\.0 dB/);
}

// ---------------------------------------------------------------------------
// 10. 2.2-stereo mode with only one stored sub gain renders that sub only
// ---------------------------------------------------------------------------
{
    const info = MeasurementUI.getMeasurementTimingInfo(makeMeasurement({
        autosub_meta: { target: TARGET_HARMAN, final_gains_db: { sub2: -4.0 } },
    }));
    assert.match(info.line, /Sub 2 −4\.0 dB/);
    assert.ok(!info.line.includes('Sub 1'), 'missing sub gain must be omitted');
}

// ---------------------------------------------------------------------------
// 11. 2.1 mode renders the stored single final sub gain
// ---------------------------------------------------------------------------
{
    const info = MeasurementUI.getMeasurementTimingInfo(makeMeasurement({
        autosub_meta: { target: TARGET_HARMAN, final_gains_db: { sub: -2.35 } },
    }));
    assert.match(info.line, /^Target: Harman-style · Sub −2\.3 dB$/);
    assert.ok(!info.line.includes('Sub 1'), '2.1 gain must use the compact single-sub label');
}

console.log('ok autosub saved-measurement timing line (target + final sub gains, all modes)');