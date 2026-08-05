#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for revealNowPlayingCoverWhenReady in static/app.js.
// Verifies: same-origin covers keep the fetch()/blob path, external covers
// load directly via <img> (no fetch -> no CORS), load/decode failures stay
// silent, and the abort/DOM-existence/cue-removal logic is preserved.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
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

const KEXP_URL = 'https://www.kexp.org/static/assets/img/logo-header.svg';
const LOCAL_URL = '/api/tracks/cover/42';
const ORIGIN = 'http://fxroute.local';

function makeHarness({ decode, supportDecode = true, fetchImpl, realTimers = false } = {}) {
    const calls = { fetch: [], removal: [], revoke: [] };
    const timers = new Map();
    let timerId = 0;

    const fakeSetTimeout = (cb, ms) => {
        const id = ++timerId;
        timers.set(id, { cb, ms });
        return id;
    };
    const fakeClearTimeout = (id) => timers.delete(id);

    let blobCounter = 0;
    class MockURL extends URL {
        static createObjectURL() {
            blobCounter += 1;
            return `blob:mock-${blobCounter}`;
        }
        static revokeObjectURL(url) {
            calls.revoke.push(url);
        }
    }

    const sandbox = {
        URL: MockURL,
        AbortController,
        DOMException,
        setTimeout: realTimers ? setTimeout : fakeSetTimeout,
        clearTimeout: realTimers ? clearTimeout : fakeClearTimeout,
        window: { location: { href: `${ORIGIN}/`, origin: ORIGIN } },
        document: { body: { contains: (el) => el.inDocument !== false } },
        scheduleNowPlayingCueRemoval: (cue, ms) => { calls.removal.push(ms); },
        fetch: fetchImpl || (async () => { throw new Error('unexpected fetch'); }),
    };
    vm.createContext(sandbox);
    vm.runInContext('var nowPlayingCueCoverAbort = null;', sandbox);
    vm.runInContext(extractFunction('revealNowPlayingCoverWhenReady'), sandbox);

    function flushTimers() {
        for (const { cb } of [...timers.values()]) cb();
        timers.clear();
    }

    function makeImg({ decode: decodeOverride, supportDecode: supportOverride } = {}) {
        const useDecode = supportOverride !== undefined ? supportOverride : supportDecode;
        const img = {
            src: '',
            classes: new Set(),
            listeners: {},
            naturalWidth: 0,
            decode: useDecode ? (decodeOverride || decode || (async () => {})) : undefined,
            addEventListener(ev, cb) {
                (img.listeners[ev] = img.listeners[ev] || []).push(cb);
            },
            getAttribute(name) {
                return name === 'src' ? img.src : null;
            },
            removeAttribute(name) {
                if (name === 'src') img.src = '';
            },
            fire(ev) {
                (img.listeners[ev] || []).slice().forEach((cb) => cb());
            },
            classList: {
                add: (...names) => names.forEach((n) => img.classes.add(n)),
                remove: (...names) => names.forEach((n) => img.classes.delete(n)),
            },
        };
        return img;
    }

    function makeCue(inDocument = true) {
        const cue = {
            classes: new Set(),
            inDocument,
            classList: {
                add: (...names) => names.forEach((n) => cue.classes.add(n)),
                remove: (...names) => names.forEach((n) => cue.classes.delete(n)),
            },
        };
        return cue;
    }

    return { sandbox, calls, flushTimers, makeImg, makeCue };
}

function okFetch() {
    return async (url, opts) => {
        // default stub recorded per-harness via calls in the factory closure;
        // this variant is replaced below per test where needed.
        throw new Error('stub not wired');
    };
}

const cases = [];

async function run(label, fn) {
    try {
        await fn();
        cases.push({ label, pass: true });
    } catch (err) {
        cases.push({ label, pass: false, err });
    }
}

(async () => {
    // 1. External cover: direct img.src, no fetch() at all.
    await run('external cover loads directly without fetch', async () => {
        const h = makeHarness();
        h.sandbox.fetch = async () => { h.calls.fetch.push('unexpected'); return { ok: false }; };
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.equal(img.src, KEXP_URL, 'img.src is the external URL directly');
        assert.equal(h.calls.fetch.length, 0, 'no fetch() for external cover');
        assert.ok(img.classes.has('is-ready'), 'img is-ready');
        assert.ok(cue.classes.has('has-cover'), 'cue has-cover');
        assert.deepEqual(h.calls.removal, [3600], 'cue removal scheduled with 3600');
    });

    // 2. Same-origin cover: fetch()/blob path preserved.
    await run('same-origin cover keeps fetch/blob path', async () => {
        const h = makeHarness();
        h.sandbox.fetch = async (url, opts) => {
            h.calls.fetch.push({ url, opts });
            return { ok: true, blob: async () => new Blob(['x']), json: async () => ({ available: true }) };
        };
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, LOCAL_URL);
        assert.equal(h.calls.fetch.length, 1, 'fetch called once');
        assert.equal(h.calls.fetch[0].url, LOCAL_URL, 'fetched the local cover URL');
        assert.equal(h.calls.fetch[0].opts.cache, 'force-cache', 'force-cache kept');
        assert.ok(h.calls.fetch[0].opts.signal, 'abort signal passed');
        assert.equal(img.src, 'blob:mock-1', 'img.src is a blob object URL');
        assert.ok(img.classes.has('is-ready') && cue.classes.has('has-cover'), 'classes set');
        h.flushTimers();
        assert.deepEqual(h.calls.revoke, ['blob:mock-1'], 'object URL revoked later');
    });

    // 3. External decode failure stays silent, no classes.
    await run('external decode rejection stays silent', async () => {
        const h = makeHarness({ decode: async () => { throw new Error('decode failed'); } });
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.equal(img.src, KEXP_URL, 'src was still assigned');
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes');
        assert.equal(h.calls.removal.length, 0, 'no removal scheduled');
    });

    // 4. Same-origin fetch failure stays silent.
    await run('same-origin fetch failure stays silent', async () => {
        const h = makeHarness();
        h.sandbox.fetch = async () => ({ ok: false });
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, LOCAL_URL);
        assert.equal(img.src, '', 'no src assigned');
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes');
        assert.equal(h.calls.removal.length, 0, 'no removal scheduled');
    });

    // 5. Same-origin network error stays silent (fetch throws).
    await run('same-origin network error stays silent', async () => {
        const h = makeHarness();
        h.sandbox.fetch = async () => { throw new TypeError('Failed to fetch'); };
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, LOCAL_URL);
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes');
    });

    // 6. coverInfoUrl gate still runs first.
    await run('coverInfoUrl unavailable aborts before cover fetch', async () => {
        const h = makeHarness();
        h.sandbox.fetch = async (url) => {
            h.calls.fetch.push(url);
            if (url.startsWith('/api/tracks/cover-info/')) {
                return { ok: true, json: async () => ({ available: false }) };
            }
            return { ok: true, blob: async () => new Blob(['x']), json: async () => ({}) };
        };
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, LOCAL_URL, '/api/tracks/cover-info/42');
        assert.deepEqual(h.calls.fetch, ['/api/tracks/cover-info/42'], 'only the info URL fetched');
        assert.ok(!cue.classes.has('has-cover'), 'no has-cover');
    });

    // 7. DOM-existence check preserved.
    await run('cue removed from DOM prevents classes', async () => {
        const h = makeHarness();
        const img = h.makeImg();
        const cue = h.makeCue(false);
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.equal(img.src, KEXP_URL, 'src assigned');
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes when cue gone');
    });

    // 8. Abort-identity check preserved (newer cue wins).
    await run('abort identity check preserved', async () => {
        const h = makeHarness({
            decode: async () => { h.sandbox.nowPlayingCueCoverAbort = new AbortController(); },
        });
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes after superseded');
    });

    // 9. No img.decode support: waits on load event; error path silent.
    await run('no decode support waits for load event', async () => {
        const h = makeHarness({ supportDecode: false });
        const img = h.makeImg();
        const cue = h.makeCue();
        const pending = h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.equal(img.src, KEXP_URL, 'src assigned before load');
        img.fire('load');
        await pending;
        assert.ok(img.classes.has('is-ready') && cue.classes.has('has-cover'), 'classes after load');
    });

    await run('no decode support error stays silent', async () => {
        const h = makeHarness({ supportDecode: false });
        const img = h.makeImg();
        const cue = h.makeCue();
        const pending = h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        img.fire('error');
        await pending;
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'no classes after error');
    });

    // 10. Protocol-relative external URL is treated as external.
    await run('protocol-relative URL treated as external', async () => {
        const h = makeHarness();
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, '//cdn.example.com/logo.svg');
        assert.equal(img.src, '//cdn.example.com/logo.svg', 'direct src');
        assert.equal(h.calls.fetch.length, 0, 'no fetch');
        assert.ok(img.classes.has('is-ready') && cue.classes.has('has-cover'), 'classes set');
    });

    // 11. Empty cover URL is a no-op.
    await run('empty cover URL is a no-op', async () => {
        const h = makeHarness();
        const img = h.makeImg();
        const cue = h.makeCue();
        await h.sandbox.revealNowPlayingCoverWhenReady(cue, img, '');
        assert.equal(img.src, '', 'no src');
        assert.equal(h.calls.fetch.length, 0, 'no fetch');
        assert.equal(h.calls.removal.length, 0, 'no removal');
    });

    // 12. External slow image (fake timers): the abort terminates the wait and
    // a late load success must not mark the cue ready.
    await run('external slow image: abort terminates wait, late success ignored', async () => {
        const h = makeHarness();
        let lateResolve = () => {};
        const slowDecode = new Promise((res) => { lateResolve = res; });
        const img = h.makeImg({ decode: () => slowDecode });
        const cue = h.makeCue();
        const pending = h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        assert.equal(img.src, KEXP_URL, 'src assigned while loading');
        assert.ok(!img.classes.has('is-ready'), 'not ready while loading');
        h.flushTimers(); // fires the 2500ms abort timer
        await pending;
        assert.equal(img.src, '', 'src removed to cancel the pending request');
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'not ready after abort');
        assert.equal(h.calls.removal.length, 0, 'no removal scheduled');
        lateResolve(); // image would have loaded after the timeout
        await Promise.resolve();
        await Promise.resolve();
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'late success does not mark ready');
        assert.equal(img.src, '', 'late success does not restore src');
    });

    // 13. Real-timer variant: an external image loading longer than 2500ms —
    // the wait must end at the abort, not at the (late) load success.
    await run('external image >2500ms: wait ends at abort, late success ignored', async () => {
        const h = makeHarness({ realTimers: true });
        let lateResolve = () => {};
        const slowDecode = new Promise((res) => { lateResolve = res; });
        const img = h.makeImg({ decode: () => slowDecode });
        const cue = h.makeCue();
        const t0 = Date.now();
        const pending = h.sandbox.revealNowPlayingCoverWhenReady(cue, img, KEXP_URL);
        await pending;
        const elapsed = Date.now() - t0;
        assert.ok(elapsed >= 2400, `wait ended at the ~2500ms abort (took ${elapsed}ms)`);
        assert.equal(img.src, '', 'src removed on abort');
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'not ready after abort');
        lateResolve(); // image finally loads after the timeout
        await Promise.resolve();
        await Promise.resolve();
        assert.ok(!img.classes.has('is-ready') && !cue.classes.has('has-cover'), 'late success does not mark ready');
    });

    const failed = cases.filter((c) => !c.pass);
    for (const c of failed) {
        console.error(`FAIL ${c.label}:`, c.err);
    }
    const total = cases.length;
    console.log(`ok — ${total - failed.length}/${total} revealNowPlayingCoverWhenReady cases`);
    if (failed.length) process.exit(1);
})();
