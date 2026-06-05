#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');

const repoRoot = path.resolve(__dirname, '..');
const dsp = require(path.join(repoRoot, 'static', 'measurement_dsp.js'));

const SAMPLE_RATE = 48000;
const TAP_LENGTHS = [8192, 16384, 32768];
const PEAK_INDEX_TOLERANCE = 1;
const MAX_START_TO_PEAK_RATIO = 0.05;
const MAX_TAIL_RMS_DB = -65;
const MIN_DISTINCT_SAMPLE_DELTA = 1e-5;
const MAX_TRANSITION_MAGNITUDE_ERROR_DB = 0.25;

const analysis = {
    corrections: [
        { frequency: 20, correctionDb: -2 },
        { frequency: 35, correctionDb: 5.5 },
        { frequency: 60, correctionDb: -6 },
        { frequency: 90, correctionDb: 4 },
        { frequency: 150, correctionDb: -3 },
        { frequency: 250, correctionDb: 2.5 },
        { frequency: 500, correctionDb: -5 },
        { frequency: 900, correctionDb: 3.5 },
        { frequency: 1800, correctionDb: -2 },
        { frequency: 3500, correctionDb: 4.5 },
        { frequency: 7000, correctionDb: -4 },
        { frequency: 12000, correctionDb: 2 },
        { frequency: 20000, correctionDb: -1 },
    ],
};

function findPeak(samples) {
    let index = 0;
    let value = 0;
    for (let cursor = 0; cursor < samples.length; cursor += 1) {
        const absValue = Math.abs(samples[cursor]);
        if (absValue > value) {
            index = cursor;
            value = absValue;
        }
    }
    return { index, value };
}

function rmsDb(samples, start, end) {
    let sum = 0;
    let count = 0;
    for (let cursor = start; cursor < end; cursor += 1) {
        sum += samples[cursor] * samples[cursor];
        count += 1;
    }
    return 20 * Math.log10(Math.sqrt(sum / Math.max(1, count)) + 1e-15);
}

function maxAbsDiff(left, right) {
    assert.equal(left.length, right.length);
    let max = 0;
    for (let index = 0; index < left.length; index += 1) {
        max = Math.max(max, Math.abs(left[index] - right[index]));
    }
    return max;
}

function checkHybridAlignedImpulse(length) {
    const hybrid = dsp.buildMeasurementConvolverImpulse(analysis, SAMPLE_RATE, length, 0, 'hybrid_aligned');
    const minimum = dsp.buildMeasurementConvolverImpulse(analysis, SAMPLE_RATE, length, 0, 'minimum');
    const peak = findPeak(hybrid);
    const expectedPeakIndex = length / 2;
    const tailStart = length - Math.floor(length / 16);
    const tailDb = rmsDb(hybrid, tailStart, length);
    const minimumDiff = maxAbsDiff(hybrid, minimum);

    assert.ok(
        Math.abs(peak.index - expectedPeakIndex) <= PEAK_INDEX_TOLERANCE,
        `hybrid_aligned ${length} peak index ${peak.index}, expected around ${expectedPeakIndex}`,
    );
    assert.ok(
        Math.abs(hybrid[0]) < peak.value * MAX_START_TO_PEAK_RATIO,
        `hybrid_aligned ${length} starts too hot: start=${Math.abs(hybrid[0])}, peak=${peak.value}`,
    );
    assert.ok(
        tailDb < MAX_TAIL_RMS_DB,
        `hybrid_aligned ${length} tail RMS is ${tailDb.toFixed(2)} dB, expected below ${MAX_TAIL_RMS_DB} dB`,
    );
    assert.ok(
        minimumDiff > MIN_DISTINCT_SAMPLE_DELTA,
        `hybrid_aligned ${length} is unexpectedly close to minimum phase: max diff ${minimumDiff}`,
    );

    return { length, peakIndex: peak.index, peakValue: peak.value, tailDb, minimumDiff };
}

function checkHybridDistinctFromLinear() {
    const length = TAP_LENGTHS[0];
    const hybrid = dsp.buildMeasurementConvolverImpulse(analysis, SAMPLE_RATE, length, 0, 'hybrid_aligned');
    const linear = dsp.buildMeasurementConvolverImpulse(analysis, SAMPLE_RATE, length, 0, 'linear');
    const linearDiff = maxAbsDiff(hybrid, linear);
    assert.ok(
        linearDiff > MIN_DISTINCT_SAMPLE_DELTA,
        `hybrid_aligned ${length} is unexpectedly close to linear phase: max diff ${linearDiff}`,
    );
    return { length, linearDiff };
}

function checkMinimumAlignedRouting() {
    const appPath = path.join(repoRoot, 'static', 'app.js');
    const appSource = fs.readFileSync(appPath, 'utf8');
    const alignedModesPattern = /const\s+measurementConvolverAlignedPhaseModes\s*=\s*\[\s*'minimum_aligned'\s*,\s*'hybrid_aligned'\s*\]/;
    const minimumAlignedMapping = "phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode";
    const mappingCount = appSource.split(minimumAlignedMapping).length - 1;

    assert.match(
        appSource,
        alignedModesPattern,
        'minimum_aligned must remain in the aligned phase-mode set',
    );
    assert.ok(
        mappingCount >= 2,
        'minimum_aligned must be remapped to minimum before single- and dual-channel FIR generation',
    );

    const impulseBuilder = dsp.buildMeasurementConvolverImpulse.toString();
    assert.ok(
        !impulseBuilder.includes('minimum_aligned'),
        'DSP impulse builder should not grow a separate minimum_aligned branch; app routing handles alignment',
    );

    return { mappingCount };
}

function checkHybridTransitionMagnitudeResponse() {
    const length = 32768;
    const hybrid = dsp.buildMeasurementConvolverImpulse(analysis, SAMPLE_RATE, length, 0, 'hybrid_aligned');
    const actualReal = new Float64Array(hybrid);
    const actualImag = new Float64Array(length);
    const intendedMagnitudes = dsp.buildMeasurementConvolverMagnitudeBins(analysis, SAMPLE_RATE, length, 0);
    const transition = dsp.getMeasurementConvolverHybridTransition();
    let maxErrorDb = 0;
    let maxErrorFrequency = 0;
    let sumSquaredError = 0;
    let binCount = 0;

    dsp.fftMeasurementConvolverComplex(actualReal, actualImag, false);
    for (let bin = 1; bin <= length / 2; bin += 1) {
        const frequency = (bin * SAMPLE_RATE) / length;
        if (frequency < transition.hybridMinHz || frequency > transition.hybridLinearHz) continue;
        const actualMagnitude = Math.hypot(actualReal[bin], actualImag[bin]);
        const intendedMagnitude = intendedMagnitudes[bin];
        const errorDb = 20 * Math.log10(Math.max(1e-12, actualMagnitude) / Math.max(1e-12, intendedMagnitude));
        if (Math.abs(errorDb) > Math.abs(maxErrorDb)) {
            maxErrorDb = errorDb;
            maxErrorFrequency = frequency;
        }
        sumSquaredError += errorDb * errorDb;
        binCount += 1;
    }

    const rmsErrorDb = Math.sqrt(sumSquaredError / Math.max(1, binCount));
    assert.ok(binCount > 0, 'hybrid transition magnitude check found no transition bins');
    assert.ok(
        Math.abs(maxErrorDb) < MAX_TRANSITION_MAGNITUDE_ERROR_DB,
        `hybrid_aligned transition magnitude max error ${maxErrorDb.toFixed(3)} dB at ${maxErrorFrequency.toFixed(1)} Hz, expected below ${MAX_TRANSITION_MAGNITUDE_ERROR_DB} dB`,
    );

    return { length, binCount, maxErrorDb, maxErrorFrequency, rmsErrorDb };
}

async function checkFloat32WavExport() {
    const samples = new Float32Array([0, 1.25, -1.5, 0.125]);
    const diagnostics = [];
    const originalDebug = console.debug;
    console.debug = (label, payload) => diagnostics.push({ label, payload });
    let blob;
    try {
        blob = dsp.writeMeasurementConvolverWav([samples], SAMPLE_RATE);
    } finally {
        console.debug = originalDebug;
    }
    const buffer = await blob.arrayBuffer();
    const view = new DataView(buffer);
    const readString = (offset, length) => String.fromCharCode(...new Uint8Array(buffer, offset, length));
    const readSamples = [];
    for (let offset = 44; offset < buffer.byteLength; offset += 4) {
        readSamples.push(view.getFloat32(offset, true));
    }

    assert.equal(readString(0, 4), 'RIFF');
    assert.equal(readString(8, 4), 'WAVE');
    assert.equal(view.getUint16(20, true), 3, 'FIR WAV export must use IEEE float format');
    assert.equal(view.getUint16(34, true), 32, 'FIR WAV export must be 32-bit');
    assert.equal(view.getUint32(40, true), samples.length * 4, 'FIR WAV data size must match float32 samples');
    assert.deepEqual(readSamples, Array.from(samples), 'Float32 FIR WAV export must not clamp samples above 1.0');
    assert.equal(diagnostics.length, 1, 'FIR WAV export should emit one diagnostic log');
    assert.equal(diagnostics[0].label, '[measurement-convolver-wav-export]');
    assert.equal(diagnostics[0].payload.format, 'float32');
    assert.equal(diagnostics[0].payload.clippedSampleCount, 0);
    assert.equal(diagnostics[0].payload.peakBeforeExport, 1.5);
    assert.equal(diagnostics[0].payload.peakAfterExportReadback, 1.5);

    return { formatTag: view.getUint16(20, true), bitsPerSample: view.getUint16(34, true), peak: diagnostics[0].payload.peakAfterExportReadback };
}

async function main() {
    const hybridResults = TAP_LENGTHS.map(checkHybridAlignedImpulse);
    const linearResult = checkHybridDistinctFromLinear();
    const routingResult = checkMinimumAlignedRouting();
    const magnitudeResult = checkHybridTransitionMagnitudeResponse();
    const wavResult = await checkFloat32WavExport();

    for (const result of hybridResults) {
        console.log(
            `ok hybrid_aligned ${result.length}: peak=${result.peakIndex}, tail=${result.tailDb.toFixed(2)} dB, minDiff=${result.minimumDiff.toExponential(3)}`,
        );
    }
    console.log(`ok hybrid_aligned ${linearResult.length}: linearDiff=${linearResult.linearDiff.toExponential(3)}`);
    console.log(`ok minimum_aligned routing: mappingCount=${routingResult.mappingCount}`);
    console.log(
        `ok hybrid_aligned ${magnitudeResult.length}: transitionMagnitudeMax=${magnitudeResult.maxErrorDb.toFixed(3)} dB at ${magnitudeResult.maxErrorFrequency.toFixed(1)} Hz, rms=${magnitudeResult.rmsErrorDb.toFixed(3)} dB over ${magnitudeResult.binCount} bins`,
    );
    console.log(
        `ok FIR WAV export: formatTag=${wavResult.formatTag}, bits=${wavResult.bitsPerSample}, peak=${wavResult.peak}`,
    );
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
