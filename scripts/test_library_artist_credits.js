#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Artist credits in the Library Artists index: a "feat." credit splits into
// the main artist and its guests; "&" and other joins stay as written; the
// track and album tags are never changed.

const assert = require('node:assert/strict');
const Library = require('../static/library_ui.js');
const { nameKey } = require('../static/ui_helpers.js');

const state = { library: { albums: [], tracks: [] } };
Library.init({ getState: () => state, getElements: () => ({}), nameKey });

const split = Library.splitArtistCredit;

// --- splitting ------------------------------------------------------------
assert.deepEqual(split('Afu-Ra feat. Masta Killa'), ['Afu-Ra', 'Masta Killa']);
assert.deepEqual(split('Aphrodite FEAT Miss Bunty'), ['Aphrodite', 'Miss Bunty']);
assert.deepEqual(split('A ft. B'), ['A', 'B']);
assert.deepEqual(split('A featuring B'), ['A', 'B']);
assert.deepEqual(split('A (feat. B)'), ['A', 'B']);
assert.deepEqual(split('A [feat. B]'), ['A', 'B']);
assert.deepEqual(split('A feat. B feat. C'), ['A', 'B', 'C']);
assert.deepEqual(split('A feat. A'), ['A'], 'a guest equal to the main artist adds nothing');
// Other joins stay as written, on both sides of the "feat.".
assert.deepEqual(split('Simon & Garfunkel'), ['Simon & Garfunkel']);
assert.deepEqual(split('A feat. Lady Blue & Q'), ['A', 'Lady Blue & Q']);
assert.deepEqual(split('A & B feat. C'), ['A & B', 'C']);
assert.deepEqual(split('A, B feat. C, D'), ['A, B', 'C, D']);
// Only the featuring joins split; names that merely contain the letters do not.
assert.deepEqual(split('Daft Punk'), ['Daft Punk']);
assert.deepEqual(split('Soft Cell'), ['Soft Cell']);
assert.deepEqual(split('Featherweight'), ['Featherweight']);
assert.deepEqual(split('Feat. Nobody'), ['Feat. Nobody'], 'no main artist: keep the credit whole');
assert.deepEqual(split(''), []);
assert.deepEqual(split(null), []);

// --- index ------------------------------------------------------------------
function track(id, artist, album, albumArtist) {
    return { id, title: id, artist, album, album_artist: albumArtist || '' };
}
state.library.albums = [
    { id: 'a1', name: 'Solo Album', artist: 'Main', year: 2000 },
    { id: 'a2', name: 'Mixed Bag', artist: 'Various Artists', year: 2005 },
    { id: 'a3', name: 'Duo Album', artist: 'Left & Right', year: 2010 },
];
state.library.tracks = [
    track('t1', 'Main', 'Solo Album', 'Main'),
    track('t2', 'Main feat. Guest', 'Solo Album', 'Main'),
    track('t3', 'Delta feat. Golf', 'Mixed Bag'),
    track('t4', 'Foxtrot', 'Mixed Bag'),
    track('t5', 'Left & Right feat. Hotel & India', 'Duo Album', 'Left & Right'),
    track('t6', 'Solo', 'Loose'),
];
const original = JSON.stringify([state.library.albums, state.library.tracks]);
const index = Library.buildArtistIndex(state.library.albums, state.library.tracks);
const names = Array.from(index.values()).map(entry => entry.name).sort();
assert.deepEqual(names, ['Delta', 'Foxtrot', 'Golf', 'Guest', 'Hotel & India', 'Left & Right', 'Main', 'Solo']);
assert.ok(!names.some(name => /feat/i.test(name)), 'no entry carries a featuring credit');
assert.ok(!names.includes('Various Artists'));

const ids = key => index.get(key).trackIds;
assert.deepEqual(ids('main'), ['t1', 't2'], 'the album artist keeps its tracks');
assert.deepEqual(ids('guest'), ['t2'], 'a guest on the artist\'s own album gets the track');
assert.deepEqual(ids('delta'), ['t3']);
assert.deepEqual(ids('golf'), ['t3'], 'a guest on a compilation track gets the track');
assert.deepEqual(ids('left right'), ['t5'], '"&" stays one artist');
assert.deepEqual(ids('hotel india'), ['t5']);
assert.equal(index.get('guest').albums.length, 0, 'a guest does not receive the album');
assert.equal(index.get('main').albums.length, 1);

// An album credited "A feat. B" lists under both; the tags describe A only.
state.library.albums = [{ id: 'a4', name: 'Collab', artist: 'Lead feat. Visitor', mb_artist_id: 'mb-lead', artist_description: 'Lead bio' }];
state.library.tracks = [track('t7', 'Lead feat. Visitor', 'Collab', 'Lead feat. Visitor')];
const collab = Library.buildArtistIndex(state.library.albums, state.library.tracks);
assert.deepEqual(Array.from(collab.keys()).sort(), ['lead', 'visitor']);
assert.equal(collab.get('lead').mbid, 'mb-lead');
assert.equal(collab.get('visitor').mbid, '', 'the main artist MBID is not lent to a guest');
assert.equal(collab.get('visitor').description, '');
assert.equal(collab.get('visitor').albums.length, 1);
assert.deepEqual(collab.get('visitor').trackIds, ['t7']);

// The original metadata is untouched.
state.library.albums = JSON.parse(original)[0];
state.library.tracks = JSON.parse(original)[1];
Library.buildArtistIndex(state.library.albums, state.library.tracks);
assert.equal(JSON.stringify([state.library.albums, state.library.tracks]), original);
assert.equal(state.library.tracks[1].artist, 'Main feat. Guest');

console.log('PASS  scripts/test_library_artist_credits.js');
