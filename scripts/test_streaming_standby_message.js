#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused checks for the Spotify standby empty-state.
//
// spotifyd 0.4.x only exposes MPRIS while a Connect session is active, so the
// backend flags `spotifyd_standby` on idle status reads. This verifies that
// the shipped streaming.js maps that flag to a "ready" empty state instead of
// "Spotify is not running.", and that renderNowPlaying stays identity-free by
// delegating to the shared helper.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    let depth = 0, quote = '', escaped = false;
    for (let i = brace; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

// Execute the real shipped helper, not a copy.
const notPlayingContent = new Function(
    `return ${extractFunction(src, 'notPlayingContent')}`,
)();
const buildStatusBits = new Function(
    `return ${extractFunction(src, 'buildStatusBits')}`,
)();

assert.deepEqual(buildStatusBits('spotify', { connected: true }), ['Connected']);
assert.deepEqual(buildStatusBits('spotify', { spotifyd_standby: true }), ['Connected']);
assert.deepEqual(buildStatusBits('spotify', { connected: false }), []);

// Standby: idle spotifyd daemon reads as ready, with Connect guidance.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped', spotifyd_standby: true }),
    {
        title: 'Ready for Spotify Connect.',
        message: '',
    },
);

// Standby parity: idle qbzd (daemon up, device deselected — the app keeps
// the last track paused) reads as ready.
assert.deepEqual(
    notPlayingContent('qobuz', { status: 'Paused', title: 'Diamonds', qbzd_standby: true }),
    {
        title: 'Ready for Qobuz Connect.',
        message: '',
    },
);
assert.deepEqual(
    notPlayingContent('qobuz', { status: 'Stopped', qbzd_standby: true }),
    {
        title: 'Ready for Qobuz Connect.',
        message: '',
    },
);

// Without the flag the classic messages are preserved.
assert.deepEqual(
    notPlayingContent('qobuz', { status: 'Stopped' }),
    { title: 'Nothing is playing. Start a track from the Qobuz app.', message: '' },
);

// A visible MPRIS player can be connected but stopped; that is different
// from Spotify not running at all.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped', connected: true }),
    { title: 'Nothing is playing.', message: '' },
);

// The flags never leak across providers.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped', qbzd_standby: true }),
    { title: 'Spotify is not running.', message: 'Start it to use Spotify Connect.' },
);

// Without the flag the classic not-running message is preserved, with the
// same start guidance the qbzd unavailable state offers.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped' }),
    { title: 'Spotify is not running.', message: 'Start it to use Spotify Connect.' },
);
assert.deepEqual(
    notPlayingContent('spotify', {}),
    { title: 'Spotify is not running.', message: 'Start it to use Spotify Connect.' },
);

// Missing data object must not throw.
assert.deepEqual(
    notPlayingContent('spotify'),
    { title: 'Spotify is not running.', message: 'Start it to use Spotify Connect.' },
);

// Other providers keep their messages; the flag never leaks into them.
assert.deepEqual(
    notPlayingContent('qobuz', { spotifyd_standby: true }),
    { title: 'Nothing is playing. Start a track from the Qobuz app.', message: '' },
);
assert.deepEqual(
    notPlayingContent('tidal', {}),
    { title: 'Nothing is playing.', message: '' },
);

// The renderer must delegate to the helper (identity branching lives there,
// enforced by test_streaming_ui_structure.js).
const renderNowPlaying = extractFunction(src, 'renderNowPlaying');
assert.ok(
    renderNowPlaying.includes('notPlayingContent(providerId, data)'),
    'renderNowPlaying must route the stopped state through notPlayingContent',
);
const unavailableBranch = renderNowPlaying.indexOf("data.available === false");
const unauthenticatedBranch = renderNowPlaying.indexOf("data.authenticated === false");
const standbyBranch = renderNowPlaying.indexOf('data.spotifyd_standby || data.qbzd_standby');
assert.ok(unavailableBranch >= 0 && unavailableBranch < standbyBranch,
    'real backend failures must take priority over standby copy');
assert.ok(unauthenticatedBranch >= 0 && unauthenticatedBranch < standbyBranch,
    'authentication failures must take priority over standby copy');

// The served shell must reference a versioned streaming.js (pattern-based;
// the concrete number is owned by static/index.html).
assert.match(html, /\/static\/streaming\.js\?v=\d+\.\d+\.\d+/);

console.log('PASS test_streaming_standby_message.js');
