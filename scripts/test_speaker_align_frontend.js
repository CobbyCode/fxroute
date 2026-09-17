#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Speaker Align frontend contract: payload, visibility, status text,
// shell IDs, app bindings, flow exports and demo routes.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const flowsSource = fs.readFileSync(path.join(repoRoot, 'static', 'measurement_flows.js'), 'utf8');
const demoRoutes = fs.readFileSync(path.join(repoRoot, 'demo', 'routes.js'), 'utf8');

const speakerModule = require('../static/speaker_align.js');

async function main() {
    // Pure payload builder validates the service contract before any fetch.
    const payload = speakerModule.buildSpeakerAlignPayload({
        side: 'left',
        inputId: 'mic-1',
        micChannel: '1',
        referenceChannel: '2',
        referenceId: 'interface:input-2:upstream',
        microphonePositionId: 'seat-1-fixed',
        dryRun: true,
    });
    assert.equal(payload.side, 'left');
    assert.equal(payload.input_id, 'mic-1');
    assert.equal(payload.mic_input_channel, '1');
    assert.equal(payload.reference_input_channel, '2');
    assert.equal(payload.reference_id, 'interface:input-2:upstream');
    assert.equal(payload.microphone_position_id, 'seat-1-fixed');
    assert.equal(payload.dry_run, true);

    assert.throws(() => speakerModule.buildSpeakerAlignPayload({
        side: 'center', inputId: 'mic-1', micChannel: '1',
        referenceChannel: '2', referenceId: 'r', microphonePositionId: 'm',
    }), /side must be left or right/);
    assert.throws(() => speakerModule.buildSpeakerAlignPayload({
        side: 'left', inputId: '  ', micChannel: '1',
        referenceChannel: '2', referenceId: 'r', microphonePositionId: 'm',
    }), /capture input/);
    assert.throws(() => speakerModule.buildSpeakerAlignPayload({
        side: 'left', inputId: 'mic-1', micChannel: '1',
        referenceChannel: '  ', referenceId: 'r', microphonePositionId: 'm',
    }), /reference input channel/);
    assert.throws(() => speakerModule.buildSpeakerAlignPayload({
        side: 'left', inputId: 'mic-1', micChannel: '1',
        referenceChannel: '2', referenceId: '  ', microphonePositionId: 'm',
    }), /upstream reference/);
    assert.throws(() => speakerModule.buildSpeakerAlignPayload({
        side: 'left', inputId: 'mic-1', micChannel: '1',
        referenceChannel: '2', referenceId: 'r', microphonePositionId: ' ',
    }), /microphone position/);

    // Visibility follows the crossover catalog, never legacy mode strings.
    assert.equal(speakerModule.speakerAlignVisible(null), false);
    assert.equal(speakerModule.speakerAlignVisible({}), false);
    assert.equal(speakerModule.speakerAlignVisible({
        active_mode: 'stereo',
        modes: { stereo: { topology: { way_count: 0 } }, crossover: { topology: { way_count: 2 } } },
    }), false);
    assert.equal(speakerModule.speakerAlignVisible({
        active_mode: 'crossover',
        modes: { crossover: { topology: { way_count: 1 } } },
    }), false);
    assert.equal(speakerModule.speakerAlignVisible({
        active_mode: 'crossover',
        modes: { crossover: { topology: { way_count: 2 } } },
    }), true);
    assert.equal(speakerModule.speakerAlignVisible({
        active_mode: 'crossover',
        modes: { crossover: { topology: { way_count: 4 } } },
    }), true);

    // Status text never renders [object Object] and never leaves placeholders.
    const committed = speakerModule.formatSpeakerAlignStatus({
        side: 'left', status: 'committed', message: '',
        result: { committed_revision: 7 },
    });
    assert.match(committed, /committed/i);
    assert.match(committed, /7/);
    assert.doesNotMatch(committed, /\?/);
    assert.doesNotMatch(committed, /\[object Object\]/);
    const failed = speakerModule.formatSpeakerAlignStatus({
        side: 'right', status: 'failed', message: 'boom',
        result: null, error: 'boom',
    });
    assert.match(failed, /boom/);
    const cancelled = speakerModule.formatSpeakerAlignStatus({
        side: 'left', status: 'cancelled', message: '',
        result: null, error: null,
    });
    assert.match(cancelled, /cancel/i);

    // Shell carries the speaker section with stable IDs.
    assert.match(indexSource, /id="measurement-speaker-align-group"/);
    assert.match(indexSource, /id="measurement-speaker-align-start"/);
    assert.match(indexSource, /id="measurement-speaker-align-status"/);
    assert.match(indexSource, /id="measurement-speaker-align-side"/);
    assert.match(indexSource, /id="measurement-speaker-align-dry-run"/);
    assert.match(indexSource, /id="measurement-speaker-align-reference"/);
    assert.match(indexSource, /id="measurement-speaker-align-position"/);
    assert.match(indexSource, /speaker_align\.js\?v=\d+\.\d+\.\d+/);

    // App binds the shell, talks JSON to the speaker endpoints, and tracks one job.
    assert.match(appSource, /measurementSpeakerAlignStartBtn: document\.getElementById\('measurement-speaker-align-start'\)/);
    assert.match(appSource, /measurementSpeakerAlignGroup: document\.getElementById\('measurement-speaker-align-group'\)/);
    assert.match(appSource, /measurementSpeakerAlignStatus: document\.getElementById\('measurement-speaker-align-status'\)/);
    assert.match(appSource, /measurementSpeakerAlignSide: document\.getElementById\('measurement-speaker-align-side'\)/);
    assert.match(appSource, /measurementSpeakerAlignDryRun: document\.getElementById\('measurement-speaker-align-dry-run'\)/);
    assert.match(appSource, /measurementSpeakerAlignReference: document\.getElementById\('measurement-speaker-align-reference'\)/);
    assert.match(appSource, /measurementSpeakerAlignPosition: document\.getElementById\('measurement-speaker-align-position'\)/);
    assert.match(appSource, /startSpeakerAlign: \(payload\) => fetch\('\/api\/speaker-align\/start'/);
    assert.match(appSource, /pollSpeakerAlignJob: \(jobId\) => fetch\(`\/api\/speaker-align\/jobs\/\$\{encodeURIComponent\(jobId\)\}`\)/);
    assert.match(appSource, /cancelSpeakerAlignJob: \(jobId\) => fetch\(`\/api\/speaker-align\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/cancel`/);
    assert.match(appSource, /speakerAlignInFlight/);
    assert.match(appSource, /speaker_align/);
    assert.match(appSource, /SpeakerAlign/);

    // Flow module owns start/cancel/poll/result behind the injected api.
    assert.match(flowsSource, /function syncSpeakerAlignButton\(\)/);
    assert.match(flowsSource, /async function startSpeakerAlign\(\)/);
    assert.match(flowsSource, /async function cancelSpeakerAlign\(\)/);
    assert.match(flowsSource, /async function pollSpeakerAlignJob\(jobId\)/);
    assert.match(flowsSource, /async function handleSpeakerAlignResult\(job\)/);
    assert.match(flowsSource, /api\.startSpeakerAlign/);
    assert.match(flowsSource, /api\.pollSpeakerAlignJob/);
    assert.match(flowsSource, /api\.cancelSpeakerAlignJob/);
    assert.doesNotMatch(flowsSource, /\[object Object\]/);

    // Demo intercepts the same endpoints so the UI is explorable offline.
    assert.match(demoRoutes, /\/api\/speaker-align\/start/);
    assert.match(demoRoutes, /\/api\/speaker-align\/jobs/);

    console.log('speaker align frontend tests: ok');
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
