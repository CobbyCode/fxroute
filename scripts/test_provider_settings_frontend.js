#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the provider settings module:
// install/update/service, visibility toggle and the Qobuz/TIDAL login
// dialogs render through static/provider_settings.js; app.js keeps thin
// delegating wrappers plus the device-name row and streaming refreshes.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const ProviderSettings = require('../static/provider_settings.js');
const { escapeHtml } = require('../static/ui_helpers.js');

function stubClassList() {
    return { add() {}, remove() {}, toggle() {} };
}

function stubLoginElements() {
    const el = () => ({ value: '', href: '', textContent: '', disabled: false,
        classList: stubClassList(), focus() {}, select() {},
        addEventListener() {} });
    return {
        qobuzLoginStatus: el(), qobuzLoginFinishBtn: el(), qobuzLoginCancelBtn: el(),
        qobuzLoginCloseBtn: el(), qobuzLoginRedirect: el(), qobuzLoginPanel: el(),
        qobuzLoginUrl: el(), qobuzLoginOpen: el(), qobuzLoginCopy: el(),
        tidalLoginStatus: el(), tidalLoginFinishBtn: el(), tidalLoginCancelBtn: el(),
        tidalLoginCloseBtn: el(), tidalLoginRedirect: el(), tidalLoginPanel: el(),
        tidalLoginUrl: el(), tidalLoginOpen: el(), tidalLoginCopy: el(),
    };
}

function stubList() {
    const handlers = {};
    return {
        el: { innerHTML: '', querySelectorAll: (sel) => handlers[sel] || [] },
        on(sel, items) { handlers[sel] = items; },
    };
}

function makeHarness({ providers = [], fetchImpl = null } = {}) {
    const state = { settings: { providers: {
        list: providers, loaded: false, pendingOperation: null, operationLog: '' } } };
    const list = stubList();
    const elements = { settingsProvidersList: list.el,
        settingsProviderOperation: { classList: stubClassList() },
        settingsProviderOperationLog: { textContent: '' },
        ...stubLoginElements() };
    const calls = { toasts: [], streamingEnabled: [], flags: 0, tabs: 0,
        adminPayloads: [], opens: [], closes: [] };
    const modal = {
        open: (root, options) => calls.opens.push({ root, options }),
        close: (root) => calls.closes.push(root),
    };
    ProviderSettings.init({
        getState: () => state,
        getElements: () => elements,
        showToast: (message, type) => calls.toasts.push({ message, type }),
        escapeHtml,
        getModal: () => modal,
        confirmDialog: () => true,
        onAdminPayload: (data) => calls.adminPayloads.push(data),
        applyProviderEnabledToStreaming: (id, enabled) => calls.streamingEnabled.push([id, enabled]),
        refreshStreamingFlags: () => { calls.flags += 1; },
        refreshStreamingTab: () => { calls.tabs += 1; },
    });
    if (fetchImpl) globalThis.fetch = fetchImpl;
    return { state, elements, list, calls, modal };
}

function jsonResponse(payload, ok = true) {
    return { ok, status: ok ? 200 : 500, json: async () => payload };
}

const realFetch = globalThis.fetch;

// Module boundary: canonical owner plus app.js delegation and script order.
assert.equal(typeof ProviderSettings.fetchProviderAdmin, 'function');
assert.equal(typeof ProviderSettings.renderProviderSettings, 'function');
assert.equal(typeof ProviderSettings.providerAdminButtonHtml, 'function');
assert.equal(typeof ProviderSettings.runProviderInstall, 'function');
assert.equal(typeof ProviderSettings.runProviderUninstall, 'function');
assert.equal(typeof ProviderSettings.beginQobuzLogin, 'function');
assert.equal(typeof ProviderSettings.beginTidalLogin, 'function');
for (const name of ['fetchProviderAdmin', 'renderProviderSettings', 'beginQobuzLogin', 'beginTidalLogin',
    'setupQobuzLoginModal', 'setupTidalLoginModal']) {
    assert.match(appSource, new RegExp(`function ${name}\\(`), `app.js must keep a ${name} wrapper`);
}
assert.match(appSource, /FXRouteProviderSettings/, 'app.js wrappers must delegate to the provider module');
assert.match(indexSource, /provider_settings\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('provider_settings.js') < indexSource.indexOf('/static/app.js'),
    'provider module must load before app.js');

(async () => {
    // Button visibility matrix: account providers show exactly one auth
    // action for their real state; Spotify has no login at all.
    {
        const { state } = makeHarness();
        const html = (provider) => ProviderSettings.providerAdminButtonHtml(
            { pendingOperation: null, ...provider });
        const tidalRow = html({ id: 'tidal', installed: true, authenticated: false });
        assert.match(tidalRow, /data-provider-tidal-login/, 'disconnected TIDAL offers Connect');
        assert.match(tidalRow, /data-provider-uninstall/, 'disconnected TIDAL offers Uninstall');
        assert.doesNotMatch(tidalRow, /Disconnect/, 'disconnected TIDAL shows no Disconnect');
        const tidalOn = html({ id: 'tidal', installed: true, authenticated: true });
        assert.match(tidalOn, /Disconnect/, 'connected TIDAL offers Disconnect');
        assert.doesNotMatch(tidalOn, /data-provider-tidal-login/, 'connected TIDAL shows no Connect');
        const qobuzDown = html({ id: 'qobuz', installed: true, authenticated: false, available: false });
        assert.match(qobuzDown, /Complete setup/, 'daemon-down Qobuz offers Complete setup');
        assert.match(qobuzDown, /data-provider-service="restart"/, 'daemon-down Qobuz offers Restart');
        assert.match(qobuzDown, /data-provider-qobuz-login/, 'daemon-down Qobuz offers Connect last');
        const spotify = html({ id: 'spotify', installed: true });
        assert.match(spotify, /Update/, 'Spotify offers Update');
        assert.match(spotify, /Uninstall/, 'Spotify offers Uninstall');
        assert.doesNotMatch(spotify, /Connect|Disconnect/, 'Spotify has no account action');
        const fresh = html({ id: 'tidal', installed: false, implemented: true });
        assert.match(fresh, /Install/, 'not-installed provider offers Install');
        state.settings.providers.pendingOperation = 'qobuz';
        const ownOp = html({ id: 'qobuz', installed: true });
        assert.match(ownOp, /Updating/, 'the running provider names its own operation');
        assert.doesNotMatch(ownOp, /data-provider-busy/, 'the running row itself is not marked busy');
        state.settings.providers.pendingOperation = null;
    }

    // Row rendering: status vocabulary and per-row actions stay identical.
    {
        const { state, elements } = makeHarness({ providers: [
            { id: 'spotify', name: 'Spotify', installed: true, available: true, enabled: true },
            { id: 'tidal', name: 'TIDAL', installed: true, available: true,
                authenticated: false, enabled: false },
        ] });
        ProviderSettings.renderProviderSettings();
        assert.match(elements.settingsProvidersList.innerHTML, /Spotify/);
        assert.match(elements.settingsProvidersList.innerHTML, /Installed · ready/);
        assert.match(elements.settingsProvidersList.innerHTML, /Installed · not connected/);
        assert.match(elements.settingsProvidersList.innerHTML, /data-provider-tidal-login/);
        assert.equal(state.settings.providers.pendingOperation, null);
    }

    // Busy marking: while one operation runs, every other row is marked and
    // its buttons are disabled with an explanatory title.
    {
        const { state, list } = makeHarness({ providers: [
            { id: 'qobuz', name: 'Qobuz', installed: true, enabled: true },
            { id: 'tidal', name: 'TIDAL', installed: true, enabled: true },
        ] });
        state.settings.providers.pendingOperation = 'qobuz';
        ProviderSettings.renderProviderSettings();
        assert.match(list.el.innerHTML, /data-provider-busy="1"/, 'other rows are marked busy');
        const button = { disabled: false, title: '' };
        list.on('[data-provider-busy="1"] button', [button]);
        ProviderSettings.wireProviderActionButtons();
        assert.equal(button.disabled, true, 'busy-row buttons are disabled');
        assert.equal(button.title, 'Another provider operation is running');
        state.settings.providers.pendingOperation = null;
    }

    // Visibility toggle: optimistic apply, server confirm, rollback on error.
    {
        const fetches = [];
        const { state, calls } = makeHarness({ providers: [
            { id: 'qobuz-vis', name: 'Qobuz', installed: true, enabled: true } ],
        fetchImpl: async (url, options) => {
            fetches.push([url, JSON.parse(options.body)]);
            return jsonResponse({});
        } });
        await ProviderSettings.setProviderEnabled('qobuz-vis', false);
        assert.equal(state.settings.providers.list[0].enabled, false);
        assert.deepEqual(calls.streamingEnabled, [['qobuz-vis', false]]);
        assert.equal(fetches.length, 1);
        assert.match(fetches[0][0], /qobuz-vis\/enabled/);
    }
    {
        const { state, calls } = makeHarness({ providers: [
            { id: 'qobuz-rollback', name: 'Qobuz', installed: true, enabled: true } ],
        fetchImpl: async () => jsonResponse({ detail: 'nope' }, false) });
        await ProviderSettings.setProviderEnabled('qobuz-rollback', false);
        assert.equal(state.settings.providers.list[0].enabled, true, 'failed toggle rolls back');
        assert.deepEqual(calls.streamingEnabled.at(-1), ['qobuz-rollback', true]);
        assert.equal(calls.toasts.at(-1).type, 'error');
    }

    // Admin fetch fills the provider rows and hands the device payload to app.
    {
        const payload = { providers: [{ id: 'spotify', name: 'Spotify', installed: true }],
            device_name: 'fxroute', device_name_can_change: true };
        const { state, calls, elements } = makeHarness({
            fetchImpl: async () => jsonResponse(payload) });
        await ProviderSettings.fetchProviderAdmin();
        assert.equal(state.settings.providers.loaded, true);
        assert.equal(state.settings.providers.list.length, 1);
        assert.equal(calls.adminPayloads.length, 1);
        assert.equal(calls.adminPayloads[0].device_name, 'fxroute');
        assert.match(elements.settingsProvidersList.innerHTML, /Spotify/);
    }

    // Install reactivates a disabled provider; uninstall disables it.
    {
        const posted = [];
        const { state } = makeHarness({ providers: [
            { id: 'qobuz-re', name: 'Qobuz', installed: true, enabled: false } ],
        fetchImpl: async (url, options) => {
            posted.push(url);
            if (url.endsWith('/install')) return jsonResponse({ installed: true, log: 'ok' });
            if (url.endsWith('/enabled')) return jsonResponse({ enabled: true });
            if (url.endsWith('/admin')) return jsonResponse({ providers: [
                { id: 'qobuz-re', name: 'Qobuz', installed: true, enabled: true }] });
            return jsonResponse({});
        } });
        await ProviderSettings.runProviderInstall('qobuz-re');
        assert.equal(state.settings.providers.list[0].enabled, true);
        assert.ok(posted.some((url) => url.endsWith('/install')));
        assert.ok(posted.some((url) => url.endsWith('/enabled')));
        assert.equal(state.settings.providers.pendingOperation, null);
        // Settle the fire-and-forget admin refresh before the next harness.
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));
    }
    {
        const { state } = makeHarness({ providers: [
            { id: 'tidal-re', name: 'TIDAL', installed: true, enabled: true } ],
        fetchImpl: async (url) => {
            if (url.endsWith('/uninstall')) return jsonResponse({ log: 'ok' });
            if (url.endsWith('/enabled')) return jsonResponse({ enabled: false });
            if (url.endsWith('/admin')) return jsonResponse({ providers: [
                { id: 'tidal-re', name: 'TIDAL', installed: true, enabled: false }] });
            return jsonResponse({});
        } });
        await ProviderSettings.runProviderUninstall('tidal-re');
        // Settle the fire-and-forget admin refresh before asserting.
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(state.settings.providers.list[0].enabled, false);
        assert.equal(state.settings.providers.pendingOperation, null);
    }

    // Qobuz login dialog: open wires status/focus/modal, close unwinds it.
    {
        const { elements, calls } = makeHarness({
            fetchImpl: async () => jsonResponse({ login_url: 'https://sign.in/q' }) });
        await ProviderSettings.beginQobuzLogin();
        assert.equal(elements.qobuzLoginUrl.value, 'https://sign.in/q');
        assert.equal(elements.qobuzLoginStatus.textContent, 'Waiting for sign-in…');
        assert.equal(calls.opens.length, 1);
        assert.equal(calls.opens[0].root, elements.qobuzLoginPanel);
        await ProviderSettings.cancelQobuzLogin();
        assert.deepEqual(calls.closes, [elements.qobuzLoginPanel]);
        assert.equal(elements.qobuzLoginStatus.textContent, 'Waiting for sign-in…');
    }

    // TIDAL Connect from the settings row auto-enables a disabled provider.
    {
        const posted = [];
        const { list, elements } = makeHarness({ providers: [
            { id: 'tidal', name: 'TIDAL', installed: true, enabled: false } ],
        fetchImpl: async (url, options) => {
            posted.push(url);
            if (url.endsWith('/enabled')) return jsonResponse({ enabled: true });
            if (url.includes('/pkce/finish')) return jsonResponse({ ok: true });
            if (url.includes('/pkce')) return jsonResponse({ url: 'https://tidal.sign/in' });
            if (url.endsWith('/admin')) return jsonResponse({ providers: [] });
            return jsonResponse({});
        } });
        const button = { attrs: { 'data-provider-tidal-login': '1' },
            getAttribute(k) { return this.attrs[k]; },
            addEventListener(ev, fn) { this.handler = fn; } };
        list.on('[data-provider-tidal-login]', [button]);
        ProviderSettings.renderProviderSettings();
        assert.equal(typeof button.handler, 'function');
        button.handler();
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));
        assert.ok(posted.some((url) => url.endsWith('/tidal/enabled')),
            'connect auto-enables a disabled TIDAL provider');
        assert.equal(elements.tidalLoginStatus.textContent !== '', true);
    }

    globalThis.fetch = realFetch;
    console.log('provider settings frontend tests: ok');
})().catch((error) => {
    globalThis.fetch = realFetch;
    console.error(error);
    process.exit(1);
});
