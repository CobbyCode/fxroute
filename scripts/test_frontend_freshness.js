#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A page left open for days must pick up the assets of a later deploy: it
// reloads itself once the served shell lists other asset versions, but never
// while someone is using it, while work would be lost, or in a loop.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const MODULE_PATH = path.join(__dirname, '../static/frontend_freshness.js');
const BOOT = ['/static/style.css?v=1', '/static/app.js?v=1', '/static/streaming.js?v=1', '/static/favicon.svg'];
const shell = assets => `<html><head><link rel="icon" href="/static/favicon.svg?v=0.9.12">`
    + assets.map(url => (url.endsWith('.css') || url.includes('.css?')
        ? `<link rel="stylesheet" href="${url}">` : `<script src="${url}"></script>`)).join('\n')
    + '<img src="/static/Top40.png?v=3"></html>';
const BOOT_SHELL = shell(BOOT.slice(0, 3));

function harness({ servedShell = BOOT_SHELL, fetchStatus = 200, busy = false, storage = new Map(), demo = false } = {}) {
    // Fresh module instance per test: it keeps its state in the closure.
    delete require.cache[require.resolve(MODULE_PATH)];
    const freshness = require(MODULE_PATH);
    // The demo boot script sets this flag before the frontend modules start.
    const start = () => {
        if (demo) globalThis.FXROUTE_DEMO_BOOTED = true;
        try { freshness.start(); } finally { delete globalThis.FXROUTE_DEMO_BOOTED; }
    };
    let now = 1_000_000;
    let nextTimer = 1;
    const timers = new Map();
    const reloads = [];
    const fetches = [];
    const state = { servedShell, fetchStatus, busy, offline: false };
    freshness.init({
        fetch: async (url, options) => {
            fetches.push([url, options]);
            if (state.offline) throw new TypeError('Failed to fetch');
            return { ok: state.fetchStatus === 200, status: state.fetchStatus, text: async () => state.servedShell };
        },
        now: () => now,
        setTimeout: (callback, delay) => { const id = nextTimer++; timers.set(id, { callback, at: now + delay }); return id; },
        clearTimeout: id => timers.delete(id),
        readBootAssets: () => BOOT,
        listenForInput: () => {},
        isBusy: () => state.busy,
        reload: () => reloads.push(now),
        storage: () => ({ getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }),
    });
    const settle = () => new Promise(resolve => setImmediate(resolve));
    const advance = async ms => {
        const until = now + ms;
        for (;;) {
            const due = [...timers.entries()].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
            if (!due) break;
            now = due[1].at;
            timers.delete(due[0]);
            due[1].callback();
            await settle();
        }
        now = until;
        await settle();
    };
    return { freshness, start, state, reloads, fetches, storage, advance, settle, timers, setNow: value => { now = value; } };
}

const NEW_SHELL = shell(['/static/style.css?v=1', '/static/app.js?v=2', '/static/streaming.js?v=1']);

test('the signature keeps only versioned static JS and CSS, order and duplicates aside', () => {
    const { freshness } = harness();
    const markup = shell(['/static/b.js?v=2', '/static/a.js?v=1', '/static/a.js?v=1', '/static/c.css?v=3']);
    assert.equal(freshness.signature(freshness.markupAssets(markup)),
        '/static/a.js?v=1\n/static/b.js?v=2\n/static/c.css?v=3');
    assert.equal(freshness.signature(['/static/app.js', '/demo/ws.js?v=3', 'https://cdn.test/x.js?v=1']), '',
        'unversioned, non-static and foreign URLs never count');
});

test('a page that matches the served shell never reloads', async () => {
    const h = harness();
    h.start();
    await h.advance(2 * 60 * 60 * 1000);
    assert.ok(h.fetches.length >= 7, 'the periodic check keeps running');
    assert.deepEqual(h.reloads, []);
});

test('a reconnect after an outage finds the newer shell and reloads once the page is idle', async () => {
    const h = harness({ servedShell: NEW_SHELL });
    h.start();
    h.freshness.noteInput();
    await h.freshness.check('reconnect');
    assert.deepEqual(h.reloads, [], 'someone used the page a moment ago');
    await h.advance(59_000);
    assert.deepEqual(h.reloads, []);
    await h.advance(31_000);
    assert.equal(h.reloads.length, 1);
    assert.deepEqual(h.fetches[0], ['/', { cache: 'no-store' }], 'the shell is never read from the HTTP cache');
});

test('work in progress postpones the reload until it ends', async () => {
    const h = harness({ servedShell: NEW_SHELL, busy: true });
    h.start();
    await h.advance(2 * 60 * 1000);
    await h.freshness.check('reconnect');
    await h.advance(10 * 60 * 1000);
    assert.deepEqual(h.reloads, [], 'a running measurement or transfer is never interrupted');
    h.state.busy = false;
    await h.advance(30_000);
    assert.equal(h.reloads.length, 1);
});

test('a reload that did not make the page current is not repeated for the same shell', async () => {
    const storage = new Map();
    const first = harness({ servedShell: NEW_SHELL, storage });
    first.start();
    await first.advance(2 * 60 * 1000);
    await first.freshness.check('reconnect');
    assert.equal(first.reloads.length, 1);
    // The page boots again with the same stale assets, e.g. a cache that ignores the new URLs.
    const second = harness({ servedShell: NEW_SHELL, storage });
    second.start();
    await second.advance(2 * 60 * 1000);
    await second.freshness.check('reconnect');
    await second.advance(60 * 60 * 1000);
    assert.deepEqual(second.reloads, [], 'never a reload loop');
    // A further deploy is a different shell and reloads again.
    second.state.servedShell = shell(['/static/style.css?v=1', '/static/app.js?v=3', '/static/streaming.js?v=1']);
    await second.freshness.check('reconnect');
    await second.advance(60_000);
    assert.equal(second.reloads.length, 1);
});

test('an unavailable or failing shell never reloads and is checked again later', async () => {
    const h = harness();
    h.start();
    await h.advance(2 * 60 * 1000);
    h.state.offline = true;
    await h.freshness.check('reconnect');
    h.state.offline = false;
    h.state.fetchStatus = 503;
    await h.freshness.check('reconnect');
    h.state.fetchStatus = 200;
    h.state.servedShell = '<html>maintenance page</html>';
    await h.freshness.check('reconnect');
    await h.advance(60 * 60 * 1000);
    assert.deepEqual(h.reloads, []);
    h.state.servedShell = NEW_SHELL;
    await h.advance(15 * 60 * 1000);
    await h.advance(60_000);
    assert.equal(h.reloads.length, 1, 'the periodic check picks the deploy up without any reconnect');
});

test('a deploy that is rolled back cancels the pending reload', async () => {
    const h = harness({ servedShell: NEW_SHELL, busy: true });
    h.start();
    await h.advance(2 * 60 * 1000);
    await h.freshness.check('reconnect');
    h.state.servedShell = BOOT_SHELL;
    await h.freshness.check('reconnect');
    h.state.busy = false;
    await h.advance(10 * 60 * 1000);
    assert.deepEqual(h.reloads, []);
});

test('regular triggers share one fetch per minute, a reconnect is never swallowed', async () => {
    const h = harness();
    h.start();
    await h.freshness.check('visible');
    await h.freshness.check('visible');
    await h.freshness.check('interval');
    assert.equal(h.fetches.length, 1);
    await h.freshness.check('reconnect');
    assert.equal(h.fetches.length, 2);
});

test('the demo and an unstarted module do nothing', async () => {
    const demo = harness({ servedShell: NEW_SHELL, demo: true });
    demo.start();
    await demo.freshness.check('reconnect');
    await demo.advance(60 * 60 * 1000);
    assert.deepEqual([demo.fetches.length, demo.reloads.length], [0, 0]);
    const idle = harness({ servedShell: NEW_SHELL });
    await idle.freshness.check('reconnect');
    assert.deepEqual([idle.fetches.length, idle.reloads.length], [0, 0]);
});

test('the shipped shell loads the module before app.js and app.js wires the busy check', () => {
    const indexHtml = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    const moduleAt = indexHtml.indexOf('/static/frontend_freshness.js?v=');
    const appAt = indexHtml.indexOf('/static/app.js?v=');
    assert.ok(moduleAt > 0 && moduleAt < appAt, 'the module must exist before app.js initialises it');
    const app = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
    assert.match(app, /FXRouteFrontendFreshness\?\.init\(\{\s*isBusy: isFrontendReloadBlocked/);
    assert.match(app, /FXRouteFrontendFreshness\?\.start\(\)/);
    const busy = app.slice(app.indexOf('function isFrontendReloadBlocked'));
    for (const guard of ['isMeasurementPanelOpen()', 'hasActiveMeasurementJob()', "'uploading'", "'downloading'"]) {
        assert.ok(busy.slice(0, 600).includes(guard), `reload must wait for ${guard}`);
    }
});
