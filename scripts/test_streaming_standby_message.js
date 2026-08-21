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

// Standby: idle spotifyd daemon reads as ready, with Connect guidance.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped', spotifyd_standby: true }),
    {
        title: 'Spotify is ready.',
        message: 'spotifyd is waiting for a Spotify Connect session. Start playback from any Spotify app and select FXRoute.',
    },
);

// Without the flag the classic not-running message is preserved.
assert.deepEqual(
    notPlayingContent('spotify', { status: 'Stopped' }),
    { title: 'Spotify is not running.', message: '' },
);
assert.deepEqual(
    notPlayingContent('spotify', {}),
    { title: 'Spotify is not running.', message: '' },
);

// Missing data object must not throw.
assert.deepEqual(
    notPlayingContent('spotify'),
    { title: 'Spotify is not running.', message: '' },
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

// The served shell must reference a versioned streaming.js (pattern-based;
// the concrete number is owned by static/index.html).
assert.match(html, /\/static\/streaming\.js\?v=\d+\.\d+\.\d+/);

console.log('PASS test_streaming_standby_message.js');
