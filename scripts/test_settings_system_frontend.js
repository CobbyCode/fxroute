#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the settings system module:
// maintenance/update, device name, music libraries, hardware controller
// and the power menu render through static/settings_system.js; app.js keeps
// the panel wiring plus polling/panel orchestration behind init callbacks.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const SettingsSystem = require('../static/settings_system.js');
const { escapeHtml } = require('../static/ui_helpers.js');

const MOVED = [
    'updateLogFromResult', 'parseUpdateSummary', 'parseUpdateVersion',
    'parseUpdateInfo', 'maintenanceStatusText', 'renderMaintenancePanel',
    'checkFxrouteUpdate', 'waitForFxrouteRestart', 'runFxrouteUpdate',
    'restoreFxrouteToPublic', 'applyDeviceName', 'renderDeviceNameSettings',
    'musicLibrarySelectModel', 'fetchMusicLibraries', 'selectMusicLibrary',
    'addManualMusicLibrary', 'formatHardwareBool', 'renderHardwareController',
    'normalizeHardwareStatus', 'fetchHardwareStatus', 'runHardwareCommand',
    'updatePowerButtonConnectionState', 'setupPowerMenu', 'setPowerMenuOpen',
    'refreshPowerCapabilities', 'applyPowerCapabilities', 'handlePowerAction',
];

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('settings_system.js?v=') < indexSource.indexOf('app.js?v='),
    'settings_system.js must load before app.js',
);
for (const name of MOVED) {
    assert.equal(typeof SettingsSystem[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in settings_system.js`,
    );
}

// app.js wiring reaches the module through the SettingsSystem alias.
for (const snippet of [
    'window.FXRouteSettingsSystem?.init({',
    'SettingsSystem.setupPowerMenu()',
    'SettingsSystem.updatePowerButtonConnectionState()',
    'SettingsSystem.checkFxrouteUpdate(',
    'SettingsSystem.fetchMusicLibraries()',
    'SettingsSystem.fetchHardwareStatus()',
    'SettingsSystem.renderMaintenancePanel()',
    'SettingsSystem.renderDeviceNameSettings()',
    'SettingsSystem.renderHardwareController()',
    'SettingsSystem.musicLibrarySelectModel(',
    'SettingsSystem.stopMusicLibraryRefresh()',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}

// Pure helpers keep their contracts.
assert.equal(SettingsSystem.maintenanceStatusText({ dirtyBlock: true }), 'Local source changes. Update disabled');
assert.equal(SettingsSystem.maintenanceStatusText({ hasError: true }), 'Update check failed');
assert.equal(SettingsSystem.maintenanceStatusText({ updateAvailable: true }), 'Update available');
assert.equal(SettingsSystem.maintenanceStatusText({ updateAvailable: false }), 'FXRoute is up to date');
assert.equal(SettingsSystem.formatHardwareBool(true, 'a', 'b'), 'a');
assert.equal(SettingsSystem.formatHardwareBool(false, 'a', 'b'), 'b');
assert.equal(SettingsSystem.formatHardwareBool(null, 'a', 'b'), 'unknown');
assert.equal(
    SettingsSystem.parseUpdateSummary('Current: 0.9.11\nRemote: 0.9.12'),
    'Current 0.9.11 · Latest 0.9.12',
);
assert.equal(SettingsSystem.parseUpdateVersion('0.9.12 (abc123…)'), '0.9.12');

// Select-model loading contract through the canonical module.
SettingsSystem.init({ escapeHtml });
const loadingModel = SettingsSystem.musicLibrarySelectModel({ active_id: 'local', libraries: [], loading: true });
assert.match(loadingModel.html, /Discovering network shares/);
assert.equal(loadingModel.disabled, true);

// Panel renders through injected state/elements only.
{
    const state = { settings: { maintenance: { updateAvailable: true, pending: false } }, wsConnected: true };
    const powerAttrs = {};
    const powerToggle = { classList: { toggle() {} }, title: '', setAttribute(name, value) { powerAttrs[name] = value; } };
    const elements = {
        settingsMaintenanceStatus: { textContent: '' },
        settingsMaintenanceCurrent: { textContent: '' },
        settingsMaintenanceLatestRow: { classList: { toggle() {} } },
        settingsMaintenanceLatest: { textContent: '' },
        settingsMaintenanceDetail: { textContent: '' },
        settingsUpdateLog: { textContent: '' },
        settingsUpdateDetailsToggle: { textContent: '', setAttribute() {} },
        settingsUpdateDetails: { classList: { toggle() {} } },
        settingsUpdateCheckBtn: { disabled: false },
        settingsUpdateRunBtn: { disabled: false, classList: { toggle() {} } },
        settingsRestoreRunBtn: { disabled: false },
        settingsRestoreSection: { classList: { toggle() {} } },
        powerMenuToggle: powerToggle,
    };
    const calls = { renders: 0, toasts: [] };
    SettingsSystem.init({
        getState: () => state,
        getElements: () => elements,
        showToast: (message, type) => calls.toasts.push({ message, type }),
        escapeHtml,
        renderSettingsPanel: () => { calls.renders += 1; },
        renderLibraryView: () => {},
        fetchLibraryStatus: async () => {},
        confirmDialog: () => true,
    });
    SettingsSystem.renderMaintenancePanel();
    assert.equal(elements.settingsMaintenanceStatus.textContent, 'Update available');
    assert.equal(elements.settingsUpdateRunBtn.disabled, false);
    SettingsSystem.updatePowerButtonConnectionState();
    // The button carries a data-tooltip; a native title would show a second,
    // competing tooltip next to the app bubble.
    assert.equal(powerAttrs['data-tooltip'], 'FXRoute online');
    assert.equal(powerToggle.title, '');
}

console.log('settings system frontend: ok');
