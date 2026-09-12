#!/usr/bin/env node
// Structured transition failures must stay readable on every playback path.
//
// /api/play and the provider toggles answer a failed transition with
// {"detail": {"ok": false, "stage": "target-rate", "message": "..."}} (see
// PlaybackTransitionFailure.as_status in playback/transition/models.py).
// Stringifying that object produced the bare "[object Object]" toast during
// the .104 playback outage; these helpers must render stage + message instead.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function extractFunction(source, name, file) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name} in ${file}`);
    // Skip the parameter list (it may carry a `{}` default) before the body.
    let parens = 0;
    let brace = -1;
    for (let index = match.index + match[0].length - 1; index < source.length; index += 1) {
        if (source[index] === '(') parens += 1;
        else if (source[index] === ')' && --parens === 0) {
            brace = source.indexOf('{', index);
            break;
        }
    }
    assert.ok(brace !== -1, `missing body for ${name} in ${file}`);
    let depth = 0;
    for (let index = brace; index < source.length; index += 1) {
        if (source[index] === '{') depth += 1;
        if (source[index] === '}' && --depth === 0) {
            const code = source.slice(match.index, index + 1);
            const isAsync = /(^|[^\w])async\s*$/.test(source.slice(0, match.index));
            return isAsync ? `async ${code}` : code;
        }
    }
    throw new Error(`unterminated ${name} in ${file}`);
}

function load(code, scope) {
    const sandbox = { ...scope };
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox);
    return sandbox;
}

const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const streamingSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');

const transitionFailure = {
    ok: false,
    transition_id: 'tr-6aadba02783d4820b2399ff1f40c1db4',
    stage: 'target-rate',
    failure_latched: true,
    message: 'target hardware rate did not settle: expected=44100 active=48000 force=44100',
};

// FastAPI validation errors answer ``{"detail": [...]}``: structured, but with
// no ``message`` to render. Those must not end up as an empty toast.
const validationDetail = [
    { loc: ['body', 'target_url'], msg: 'field required', type: 'value_error.missing' },
];

function appFetchScope(payload) {
    return load(extractFunction(appSource, 'apiFetchJson', 'app.js'), {
        formatTransitionErrorDetail: formatTransitionErrorDetail,
        fetch: async () => ({ ok: false, status: 500, json: async () => payload }),
    });
}

const formatTransitionErrorDetail = load(
    extractFunction(appSource, 'formatTransitionErrorDetail', 'app.js'),
).formatTransitionErrorDetail;

(async () => {
    // -----------------------------------------------------------------------
    // app.js: shared formatter + the JSON fetch helper used by local play.
    // -----------------------------------------------------------------------
    assert.equal(
        formatTransitionErrorDetail(transitionFailure, 'fallback'),
        `${transitionFailure.message} (stage: target-rate)`,
    );
    assert.equal(formatTransitionErrorDetail({ message: 'Playback failed' }, 'fallback'), 'Playback failed');
    assert.equal(formatTransitionErrorDetail({}, 'fallback'), 'fallback');
    // Message-less structured details are serialized, never dropped to ''.
    assert.equal(
        formatTransitionErrorDetail(validationDetail, 'fallback'),
        JSON.stringify(validationDetail),
    );
    assert.equal(
        formatTransitionErrorDetail({ ok: false, errors: ['a', 'b'] }, 'fallback'),
        '{"ok":false,"errors":["a","b"]}',
    );
    const circularDetail = { ok: false };
    circularDetail.self = circularDetail;
    assert.equal(formatTransitionErrorDetail(circularDetail, 'fallback'), 'fallback');

    await assert.rejects(
        () => appFetchScope({ detail: transitionFailure }).apiFetchJson('/api/play', { method: 'POST' }),
        (error) => {
            assert.ok(error.message.includes('target hardware rate did not settle'), error.message);
            assert.ok(error.message.includes('stage: target-rate'), error.message);
            assert.ok(!error.message.includes('[object Object]'), error.message);
            return true;
        },
    );
    await assert.rejects(
        () => appFetchScope(null).apiFetchJson('/api/play', { method: 'POST' }),
        (error) => {
            assert.equal(error.message, 'HTTP 500 /api/play');
            return true;
        },
    );
    console.log('ok — app.js renders structured transition errors');

    // -----------------------------------------------------------------------
    // streaming.js: provider-facing normalization (TIDAL play and toggles).
    // -----------------------------------------------------------------------
    const friendlyError = load(
        extractFunction(streamingSource, 'friendlyError', 'streaming.js'),
        { formatTransitionErrorDetail: formatTransitionErrorDetail },
    ).friendlyError;

    assert.equal(
        friendlyError(transitionFailure),
        `${transitionFailure.message} (stage: target-rate)`,
    );
    // A structured payload without a message must never degrade to the object
    // (nor to an empty toast): it is serialized instead.
    assert.equal(
        friendlyError({ ok: false, stage: 'target-rate' }),
        '{"ok":false,"stage":"target-rate"}',
    );
    assert.equal(friendlyError(validationDetail), JSON.stringify(validationDetail));
    // The provider-specific normalization stays intact.
    assert.equal(friendlyError('TIDAL is not authenticated'), 'You need to sign in to continue.');
    assert.equal(friendlyError({ message: 'This track is not available' }), 'This track is currently unavailable.');
    assert.equal(friendlyError(undefined), 'Something went wrong.');

    // Every `throw new Error(await errorDetail(resp))` call site must carry the
    // readable message too: that Error text is what friendlyError() receives.
    const errorDetail = load(
        extractFunction(streamingSource, 'errorDetail', 'streaming.js'),
        { formatTransitionErrorDetail: formatTransitionErrorDetail },
    ).errorDetail;
    assert.equal(
        await errorDetail({ json: async () => ({ detail: transitionFailure }) }),
        `${transitionFailure.message} (stage: target-rate)`,
    );
    assert.equal(
        await errorDetail({ json: async () => ({ detail: validationDetail }) }),
        JSON.stringify(validationDetail),
    );
    console.log('ok — streaming.js renders structured transition errors');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
