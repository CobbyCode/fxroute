#!/usr/bin/env node
// Structured transition failures must stay readable on every playback path.
//
// /api/play and the provider toggles answer a failed transition with
// {"detail": {"ok": false, "stage": "target-rate", "message": "..."}} (see
// PlaybackTransitionFailure.as_status in playback/transition/models.py).
// Stringifying that object produced the bare "[object Object]" toast during
// the .104 playback outage; these helpers must render stage + message instead.
//
// Message-less structured details (FastAPI validation lists, status objects
// without a message) must never leak raw JSON or internal fields into toasts
// or error states: they map to a generic user-facing fallback while the
// structured payload is kept for diagnosis via console.warn.

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

function extractAssignedFunction(source, name, file) {
    // Matches `let <name> = function (...) { ... }` (streaming.js default).
    const match = new RegExp(`${name}\\s*=\\s*function\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name} assignment in ${file}`);
    const parenAt = match.index + match[0].length - 1;
    let parens = 0;
    let brace = -1;
    for (let index = parenAt; index < source.length; index += 1) {
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
            const paramsAndBody = source.slice(parenAt, index + 1);
            return `var ${name} = function${paramsAndBody};`;
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
// no ``message`` to render. Those must map to generic user text, never to raw
// JSON or an empty toast.
const validationDetail = [
    { loc: ['body', 'target_url'], msg: 'field required', type: 'value_error.missing' },
];

function appFetchScope(payload, formatter) {
    return load(extractFunction(appSource, 'apiFetchJson', 'app.js'), {
        formatTransitionErrorDetail: formatter,
        fetch: async () => ({ ok: false, status: 500, json: async () => payload }),
    });
}

const appFormatterCode = extractFunction(appSource, 'formatTransitionErrorDetail', 'app.js');
const streamingFormatterCode = extractAssignedFunction(streamingSource, 'formatTransitionErrorDetail', 'streaming.js');

function loadFormatter(code, warnings) {
    return load(code, {
        console: { warn: (...args) => { warnings.push(args); } },
    });
}

(async () => {
    // No structured detail may be coerced to a string before the formatter:
    // `new Error(object)` is what produced "[object Object]".
    assert.ok(!/new Error\(\s*(d\?\.\s*detail|data\.detail)\s*\|\|/.test(streamingSource),
        'streaming.js still coerces detail objects via new Error(detail || ...)');
    assert.ok(streamingSource.includes("formatTransitionErrorDetail(d?.detail, 'Login failed')"),
        'login error paths must use the shared formatter');
    assert.ok(streamingSource.includes("formatTransitionErrorDetail(data.detail, 'Failed to save TIDAL playlist')"),
        'playlist-create error path must use the shared formatter');
    assert.ok(streamingSource.includes("formatTransitionErrorDetail(data.detail, 'Failed to add tracks to TIDAL playlist')"),
        'playlist-add error path must use the shared formatter');
    assert.ok(streamingSource.includes("formatTransitionErrorDetail(data.detail, 'Failed to update favorite')"),
        'favorite error path must use the shared formatter');
    // Neither formatter may serialize message-less details into the UI.
    assert.ok(!/JSON\.stringify\(detail\)/.test(appFormatterCode),
        'app.js formatter must not JSON-serialize details into UI text');
    assert.ok(!/JSON\.stringify\(detail\)/.test(streamingFormatterCode),
        'streaming.js formatter must not JSON-serialize details into UI text');
    // The whitespace-bypassing raw fallback must be gone.
    assert.ok(!/typeof raw === 'string' \? raw/.test(streamingSource),
        'friendlyError must not fall back to the untrimmed raw string');

    const appWarnings = [];
    const streamingWarnings = [];
    const formatTransitionErrorDetail = loadFormatter(appFormatterCode, appWarnings).formatTransitionErrorDetail;
    const streamingFormatter = loadFormatter(streamingFormatterCode, streamingWarnings).formatTransitionErrorDetail;

    // -----------------------------------------------------------------------
    // app.js: shared formatter + the JSON fetch helper used by local play.
    // -----------------------------------------------------------------------
    assert.equal(
        formatTransitionErrorDetail(transitionFailure, 'fallback'),
        `${transitionFailure.message} (stage: target-rate)`,
    );
    assert.equal(formatTransitionErrorDetail({ message: 'Playback failed' }, 'fallback'), 'Playback failed');
    assert.equal(formatTransitionErrorDetail('  Playback failed  ', 'fallback'), 'Playback failed');
    assert.equal(formatTransitionErrorDetail('   ', 'fallback'), 'fallback');
    assert.equal(formatTransitionErrorDetail({}, 'fallback'), 'fallback');
    // Message-less structured details map to the generic fallback instead of
    // leaking raw JSON or internal fields into the UI.
    assert.equal(formatTransitionErrorDetail(validationDetail, 'fallback'), 'fallback');
    assert.equal(
        formatTransitionErrorDetail({ ok: false, errors: ['a', 'b'] }, 'fallback'),
        'fallback',
    );
    assert.equal(
        formatTransitionErrorDetail({ ok: false, stage: 'target-rate' }, 'fallback'),
        'fallback',
    );
    const circularDetail = { ok: false };
    circularDetail.self = circularDetail;
    assert.equal(formatTransitionErrorDetail(circularDetail, 'fallback'), 'fallback');
    assert.ok(appWarnings.length > 0, 'message-less details must be kept for diagnosis via console.warn');

    await assert.rejects(
        () => appFetchScope({ detail: transitionFailure }, formatTransitionErrorDetail).apiFetchJson('/api/play', { method: 'POST' }),
        (error) => {
            assert.ok(error.message.includes('target hardware rate did not settle'), error.message);
            assert.ok(error.message.includes('stage: target-rate'), error.message);
            assert.ok(!error.message.includes('[object Object]'), error.message);
            return true;
        },
    );
    await assert.rejects(
        () => appFetchScope({ detail: validationDetail }, formatTransitionErrorDetail).apiFetchJson('/api/play', { method: 'POST' }),
        (error) => {
            assert.equal(error.message, 'HTTP 500 /api/play');
            assert.ok(!error.message.includes('target_url'), error.message);
            assert.ok(!error.message.includes('[object Object]'), error.message);
            return true;
        },
    );
    await assert.rejects(
        () => appFetchScope(null, formatTransitionErrorDetail).apiFetchJson('/api/play', { method: 'POST' }),
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
    // Message-less payloads map to generic user text, never to raw JSON.
    assert.equal(friendlyError({ ok: false, stage: 'target-rate' }), 'Something went wrong.');
    assert.equal(friendlyError(validationDetail), 'Something went wrong.');
    assert.equal(friendlyError('   '), 'Something went wrong.');
    assert.ok(!friendlyError(validationDetail).includes('target_url'), 'no internal fields in UI text');
    assert.ok(!friendlyError({ ok: false, stage: 'target-rate' }).includes('"ok"'), 'no internal fields in UI text');
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
        '',
    );
    console.log('ok — streaming.js renders structured transition errors');

    // -----------------------------------------------------------------------
    // Drift coverage: the streaming.js default must behave like the canonical
    // app.js formatter, standalone and after injection.
    // -----------------------------------------------------------------------
    const driftCases = [
        ['  Playback failed  ', 'fallback'],
        ['   ', 'fallback'],
        [{ message: 'Playback failed' }, 'fallback'],
        [{ message: '  Playback failed  ', stage: '  target-rate  ' }, 'fallback'],
        [{ message: 'failed at target-rate', stage: 'target-rate' }, 'fallback'],
        [transitionFailure, 'fallback'],
        [validationDetail, 'fallback'],
        [{ ok: false, stage: 'target-rate' }, 'fallback'],
        [{}, 'fallback'],
        [circularDetail, 'fallback'],
        [undefined, 'fallback'],
        [undefined, ''],
        [null, 'Login failed'],
    ];
    for (const [input, fallback] of driftCases) {
        assert.equal(
            streamingFormatter(input, fallback),
            formatTransitionErrorDetail(input, fallback),
            `formatter drift for input ${String(input)}`,
        );
    }
    console.log('ok — formatters stay in sync');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
