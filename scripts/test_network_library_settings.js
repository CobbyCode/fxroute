#!/usr/bin/env node
// Exercise the shared settings controller at its HTTP boundary.
const assert = require('node:assert/strict');
const settings = require('../static/settings_system.js');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
    const libraries = [
        { id: 'local', type: 'local', label: 'Local' },
        { id: 'smb:nas:Music', type: 'smb', label: 'SMB — nas / Music', mounted: false },
        { id: 'nfs:nas:/music', type: 'nfs', label: 'NFS — nas:/music', mounted: false },
        { id: 'manual', type: 'action', label: 'Add manually' },
    ];
    const model = settings.musicLibraryRemovalModel({ active_id: 'local', libraries });
    assert.equal(model.disabled, false, 'offline shares must remain removable');
    assert.match(model.html, /smb:nas:Music/);
    assert.match(model.html, /nfs:nas:\/music/);
    assert.doesNotMatch(model.html, /value="local"|value="manual"/);
    const app = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
    const start = app.indexOf('    if (elements.settingsMusicLibraryRemove) {', app.indexOf('function renderSettingsPanel'));
    const end = app.indexOf('\n    renderProviderSettings();', start);
    assert.ok(start > 0 && end > start);
    const select = { value: 'nfs:nas:/music', innerHTML: '' };
    vm.runInNewContext(app.slice(start, end), {
        elements: { settingsMusicLibraryRemove: {}, settingsMusicLibraryRemoveSelect: select, settingsMusicLibraryRemoveConfirm: {} },
        musicLibrary: { libraries, active_id: 'smb:nas:Music' },
        SettingsSystem: settings,
        isSelectFocused: () => false, // Focus is on the confirmation button.
    });
    assert.equal(select.value, 'nfs:nas:/music', 'redraw must preserve the chosen removal target');

    for (const type of ['smb', 'nfs']) {
        const id = type === 'smb' ? 'smb:nas:Music' : 'nfs:nas:/music';
        const state = { settings: { musicLibrary: { active_id: id, libraries } }, library: {} };
        const requests = [];
        settings.init({ getState: () => state });
        global.fetch = async (url, options) => {
            requests.push([url, JSON.parse(options.body)]);
            return { ok: true, json: async () => ({ active_id: 'local', libraries }) };
        };
        await settings.removeMusicLibrary(id);
        assert.deepEqual(requests, [
            ['/api/music-libraries/select', { id: 'local' }],
            ['/api/music-libraries/remove', { id }],
        ]);
        assert.equal(state.settings.musicLibrary.pending, false);
    }

    const state = { settings: { musicLibrary: { active_id: 'local', libraries } }, library: {} };
    settings.init({ getState: () => state });
    global.fetch = async (url, options) => {
        assert.equal(url, '/api/music-libraries/remove');
        assert.deepEqual(JSON.parse(options.body), { id: 'nfs:nas:/music' });
        return { ok: true, json: async () => ({ active_id: 'local', libraries: [libraries[0], libraries[1], libraries[3]] }) };
    };
    await settings.removeMusicLibrary('nfs:nas:/music');
    assert.equal(state.settings.musicLibrary.libraries.some(l => l.type === 'nfs'), false);
    console.log('network library settings: ok');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
