#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const dsp = require('../static/measurement_dsp.js');
const ui = require('../static/measurement_ui.js');
const editor = require('../static/measurement_convolver_editor.js');

async function metadata(blob) {
    const bytes = Buffer.from(await blob.arrayBuffer());
    for (let offset = 12; offset + 8 <= bytes.length;) {
        const size = bytes.readUInt32LE(offset + 4);
        if (bytes.toString('ascii', offset, offset + 4) === 'fxlt') {
            return JSON.parse(bytes.toString('utf8', offset + 8, offset + 8 + size));
        }
        offset += 8 + size + (size % 2);
    }
    return null;
}

async function main() {
    let files;
    editor.init({ requireConcreteFilterBank: () => true, measurementImportBlockedReason: () => '',
        getFormDataType: () => FormData, collectEffectsExtras: () => ({}),
        appendBankBindingFields() {}, fetchOutputSystemCatalog: async () => {},
        getTimingSafetyLimitMs: () => 6,
        fetch: async (url, options) => {
            files = [options.body.get('left_file'), options.body.get('right_file')];
            return { ok: true, json: async () => ({}) };
        } });
    for (const [phaseMode, taps, algorithmic, intentional] of [
        ['minimum', 64, 0, 0], ['minimum_aligned', 64, 0, 48], ['linear', 64, 32, 0], ['hybrid_aligned', 64, 32, 48],
        ['minimum', 65536, 0, 0], ['minimum_aligned', 65536, 0, 48],
        ['linear', 65536, 32768, 0], ['hybrid_aligned', 65536, 32768, 48],
    ]) {
        await editor.createMeasurementConvolverPreset('both', [{ side: 'left' }, { side: 'right' }], 0, 'Timing', {
            sampleRate: 48000, irLength: taps, phaseMode,
            leftTiming: { available: true, arrivalMs: 2 }, rightTiming: { available: true, arrivalMs: 3 } });
        assert.deepEqual(await metadata(files[0]), { version: 1, algorithmic_frames: [algorithmic], intentional_frames: [intentional] });
        assert.deepEqual(await metadata(files[1]), { version: 1, algorithmic_frames: [algorithmic], intentional_frames: [0] });
        for (const [file, frames] of [[files[0], taps + intentional], [files[1], taps]]) {
            const wav = Buffer.from(await file.arrayBuffer());
            assert.equal(wav.readUInt32LE(40) / 4, frames, 'alignment must retain every FIR tap');
            assert.ok(Number.isFinite(wav.readFloatLE(44 + (frames - 1) * 4)));
        }
    }
    const left = new Float32Array(65536);
    const right = new Float32Array(65536);
    left[65535] = 0.25;
    right[65535] = -0.125;
    const aligned = ui.alignStereoImpulsesForMinimumAligned(left, right, 48000,
        { available: true, arrivalMs: 2 }, { available: true, arrivalMs: 3 }, 6);
    assert.equal(aligned[0].length, 65584);
    assert.equal(aligned[0][65583], 0.25, 'alignment shifts the final nonzero tap without truncating it');
    assert.equal(aligned[0][65535], 0, 'the last tap must move with the entire IR');
    assert.equal(aligned[1].length, 65536);
    assert.equal(aligned[1][65535], -0.125);
    for (const [samples, expectedTail] of [[aligned[0], 0.25], [aligned[1], -0.125]]) {
        const wav = Buffer.from(await dsp.writeMeasurementConvolverWav([samples], 48000).arrayBuffer());
        assert.equal(wav.readUInt32LE(40) / 4, samples.length);
        assert.equal(wav.readFloatLE(44 + (samples.length - 1) * 4), expectedTail);
    }
    console.log('AV Sync generated IR metadata tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
