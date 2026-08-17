#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the single authoritative playback-owner truth in the footer.
//
// getBackendFooterOwner() must resolve the owner exclusively from the
// authoritative playback payload (state.playback.playback_owner, published by
// the backend on the playback broadcast after every source commit). Provider
// status payloads (spotify/qobuz) carry metadata/transport state only; their
// playback_owner copies must never be consulted, so the footer never has to
// choose between copies of different ages (the stale-copy race).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = {
    state: { playback: {} },
    window: { __spotifyLastData: null, __qobuzLastData: null },
};
vm.createContext(sandbox);
vm.runInContext(extractFunction('getBackendFooterOwner'), sandbox);

const owner = sandbox.getBackendFooterOwner;
let passed = 0;
function check(name, actual, expected) {
    assert.equal(actual, expected, name);
    passed += 1;
}

// Authoritative playback payload drives the owner.
check('qobuz from authoritative playback payload', owner({ playback_owner: 'qobuz' }), 'qobuz');
check('spotify from authoritative playback payload', owner({ playback_owner: 'spotify' }), 'spotify');
check('local', owner({ playback_owner: 'local' }), 'local');
check('radio maps to local', owner({ playback_owner: 'radio' }), 'local');
check('tidal maps to local', owner({ playback_owner: 'tidal' }), 'local');
check('no owner -> null', owner({}), null);
check('missing payload -> null', owner(undefined), null);

// Provider payload copies are NEVER consulted, even when the authoritative
// playback payload is stale or absent.
sandbox.window.__qobuzLastData = { playback_owner: 'qobuz', title: 'T', status: 'Playing' };
sandbox.window.__spotifyLastData = { playback_owner: 'spotify', title: 'S', status: 'Playing' };
check('stale playback_owner cannot be overridden by provider copy',
    owner({ playback_owner: 'local' }), 'local');
check('absent authoritative owner ignores provider copies', owner({}), null);

// A stale authoritative owner also wins over a fresher provider copy: the
// backend republishes the owner on the playback channel on every commit, so
// the footer follows that channel instead of racing the provider payload.
check('old committed owner wins over provider copy',
    owner({ playback_owner: 'spotify' }), 'spotify');

console.log(`PASS  scripts/test_footer_owner_truth.js (${passed} checks)`);
