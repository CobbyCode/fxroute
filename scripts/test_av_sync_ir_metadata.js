#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
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
    for (const [phaseMode, algorithmic, intentional] of [
        ['minimum', 0, 0], ['minimum_aligned', 0, 48], ['linear', 32, 0], ['hybrid_aligned', 32, 48],
    ]) {
        await editor.createMeasurementConvolverPreset('both', [{ side: 'left' }, { side: 'right' }], 0, 'Timing', {
            sampleRate: 48000, irLength: 64, phaseMode,
            leftTiming: { available: true, arrivalMs: 2 }, rightTiming: { available: true, arrivalMs: 3 } });
        assert.deepEqual(await metadata(files[0]), { version: 1, algorithmic_frames: [algorithmic], intentional_frames: [intentional] });
        assert.deepEqual(await metadata(files[1]), { version: 1, algorithmic_frames: [algorithmic], intentional_frames: [0] });
    }
    console.log('AV Sync generated IR metadata tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
