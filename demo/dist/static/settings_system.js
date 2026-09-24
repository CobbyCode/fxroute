// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute settings system: Technical settings panel rows owned outside the
 * audio/output/measurement stacks - maintenance/update, device name, music
 * libraries, hardware controller and the system power menu.
 *
 * State/DOM/toast go through injected getters, panel and library refreshes
 * run through explicit callbacks. Browser-loadable UMD, no build step;
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteSettingsSystem = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ settings: {}, library: {}, samplerate: {}, wsConnected: false, powerCapabilities: null }),
        getElements: () => ({}),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        renderSettingsPanel: () => {},
        renderLibraryView: () => {},
        fetchLibraryStatus: async () => {},
        confirmDialog: (message) => (typeof confirm === 'function' ? confirm(message) : false),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    let musicLibraryRefreshTimer = null;

    function stopMusicLibraryRefresh() {
        if (musicLibraryRefreshTimer) {
            clearTimeout(musicLibraryRefreshTimer);
            musicLibraryRefreshTimer = null;
        }
    }

    function formatHardwareBool(value, onLabel = 'on', offLabel = 'off') {
        if (value === true) return onLabel;
        if (value === false) return offLabel;
        return 'unknown';
    }

    function renderHardwareController() {
        const hardware = deps.getState().settings?.hardware || {};
        const connected = !!hardware.connected;
        const status = hardware.status || {};
        const input = hardware.input || status.INPUT || 'unknown';
        if (deps.getElements().settingsHardwareSummary) {
            deps.getElements().settingsHardwareSummary.textContent = connected
                ? `Connected${hardware.device ? `: ${hardware.device}` : ''}`
                : 'Controller not detected.';
        }
        if (deps.getElements().settingsHardwareDetail) {
            if (connected) {
                const trigger = formatHardwareBool(hardware.trigger ?? status.TRIGGER, 'trigger active', 'trigger off');
                const power = formatHardwareBool(hardware.power ?? status.POWER, 'power on', 'power off');
                const auto = formatHardwareBool(hardware.auto ?? status.AUTO, 'auto on', 'auto off');
                deps.getElements().settingsHardwareDetail.textContent = `Input: ${input} · ${trigger} · ${power} · ${auto}`;
            } else {
                const note = Array.isArray(hardware.notes) && hardware.notes.length ? hardware.notes[0] : 'USB controller is optional.';
                deps.getElements().settingsHardwareDetail.textContent = note;
            }
        }
        [
            deps.getElements().settingsHardwareRcaBtn,
            deps.getElements().settingsHardwareXlrBtn,
            deps.getElements().settingsHardwarePressBtn,
            deps.getElements().settingsHardwareAutoOnBtn,
            deps.getElements().settingsHardwareAutoOffBtn,
        ].forEach((button) => {
            if (button) button.disabled = !connected || !!hardware.pending;
        });
    }


    function updateLogFromResult(data = {}) {
        const parts = [];
        if (data.stdout) parts.push(String(data.stdout).trim());
        if (data.stderr) parts.push(String(data.stderr).trim());
        return parts.filter(Boolean).join('\n\n');
    }

    function parseUpdateSummary(logText = '') {
        const currentMatch = logText.match(/Current:\s*([^\n]+)/);
        const remoteMatch = logText.match(/Remote:\s*([^\n]+)/);
        const current = currentMatch ? currentMatch[1].trim() : '';
        const remote = remoteMatch ? remoteMatch[1].trim() : '';
        if (current && remote) return `Current ${current} · Latest ${remote}`;
        return current ? `Current ${current}` : 'Version status checked.';
    }

    function parseUpdateVersion(value = '') {
        return String(value || '').replace(/\s*\([^)]*\).*$/, '').trim();
    }

    function parseUpdateInfo(logText = '') {
        const currentMatch = logText.match(/Current:\s*([^\n]+)/);
        const remoteMatch = logText.match(/Remote:\s*([^\n]+)/);
        const completedMatch = logText.match(/Update completed:\s*([^\n]+)/);
        const completedVersion = parseUpdateVersion(completedMatch ? completedMatch[1] : '');
        const currentVersion = completedVersion || parseUpdateVersion(currentMatch ? currentMatch[1] : '');
        const latestVersion = parseUpdateVersion(remoteMatch ? remoteMatch[1] : '');
        const updateAvailable = logText.includes('Update available.')
            ? true
            : logText.includes('FXRoute is already up to date.')
                || logText.includes('Update completed:')
                ? false
                : null;
        return { currentVersion, latestVersion, updateAvailable };
    }

    function maintenanceStatusText(maintenance = {}) {
        if (maintenance.dirtyBlock) return 'Local source changes. Update disabled';
        if (maintenance.hasError) return 'Update check failed';
        if (maintenance.restartPending) return 'Restarting FXRoute';
        if (maintenance.pending) return maintenance.updateAvailable === true ? 'Updating FXRoute' : 'Checking for updates';
        if (maintenance.updateAvailable === true) return 'Update available';
        if (maintenance.updateAvailable === false) return 'FXRoute is up to date';
        return maintenance.latestSummary || 'Version status not checked yet.';
    }

    function renderMaintenancePanel() {
        const maintenance = deps.getState().settings?.maintenance || {};
        const currentVersion = maintenance.currentVersion || maintenance.installedVersion || '';
        const latestVersion = maintenance.latestVersion || '';
        const showLatest = !!latestVersion && latestVersion !== currentVersion;
        const showDetails = !!maintenance.detailsExpanded
            || (!!maintenance.pending && maintenance.operation === 'update')
            || !!maintenance.restartPending
            || (!!maintenance.hasError && !maintenance.userCollapsedDetails)
            || !!maintenance.dirtyBlock;
        if (deps.getElements().settingsMaintenanceStatus) {
            deps.getElements().settingsMaintenanceStatus.textContent = maintenanceStatusText(maintenance);
        }
        if (deps.getElements().settingsMaintenanceCurrent) {
            deps.getElements().settingsMaintenanceCurrent.textContent = currentVersion || 'Unknown';
        }
        if (deps.getElements().settingsMaintenanceLatestRow) {
            deps.getElements().settingsMaintenanceLatestRow.classList.toggle('hidden', !showLatest);
        }
        if (deps.getElements().settingsMaintenanceLatest) {
            deps.getElements().settingsMaintenanceLatest.textContent = latestVersion || 'Unknown';
        }
        if (deps.getElements().settingsMaintenanceDetail) {
            deps.getElements().settingsMaintenanceDetail.textContent = maintenance.detail || '';
        }
        if (deps.getElements().settingsUpdateLog) {
            deps.getElements().settingsUpdateLog.textContent = maintenance.log || 'No update log available yet.';
        }
        if (deps.getElements().settingsUpdateDetailsToggle) {
            deps.getElements().settingsUpdateDetailsToggle.textContent = showDetails ? 'Hide update details' : 'Show update details';
            deps.getElements().settingsUpdateDetailsToggle.setAttribute('aria-expanded', showDetails ? 'true' : 'false');
        }
        if (deps.getElements().settingsUpdateDetails) {
            deps.getElements().settingsUpdateDetails.classList.toggle('hidden', !showDetails);
        }
        if (deps.getElements().settingsUpdateCheckBtn) {
            deps.getElements().settingsUpdateCheckBtn.disabled = !!maintenance.pending || !!maintenance.restartPending;
        }
        if (deps.getElements().settingsUpdateRunBtn) {
            const updateDisabled = !!maintenance.pending || !!maintenance.restartPending || maintenance.updateAvailable !== true;
            deps.getElements().settingsUpdateRunBtn.disabled = updateDisabled;
            deps.getElements().settingsUpdateRunBtn.classList.toggle('btn-primary', maintenance.updateAvailable === true && !updateDisabled);
            deps.getElements().settingsUpdateRunBtn.classList.toggle('btn-secondary', maintenance.updateAvailable !== true || updateDisabled);
        }
        if (deps.getElements().settingsRestoreRunBtn) {
            const restoreDisabled = !!maintenance.pending || !!maintenance.restartPending;
            deps.getElements().settingsRestoreRunBtn.disabled = restoreDisabled;
        }
        if (deps.getElements().settingsRestoreSection) {
            deps.getElements().settingsRestoreSection.classList.toggle('hidden', !maintenance.dirtyBlock);
        }
    }

    async function checkFxrouteUpdate(options = {}) {
        const silent = !!options.silent;
        if (!silent) {
            deps.getState().settings.maintenance.pending = true;
            deps.getState().settings.maintenance.operation = 'check';
            deps.getState().settings.maintenance.detail = 'Checking GitHub for updates…';
            deps.getState().settings.maintenance.hasError = false;
            deps.getState().settings.maintenance.userCollapsedDetails = false;
            deps.renderSettingsPanel();
        }
        let data = {};
        try {
            const resp = await fetch('/api/system/update');
            data = await resp.json().catch(() => ({}));
            const logText = updateLogFromResult(data);
            if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Update check failed');
            const updateInfo = parseUpdateInfo(logText);
            deps.getState().settings.maintenance = {
                ...deps.getState().settings.maintenance,
                installedVersion: data.installed_version || '',
                latestSummary: parseUpdateSummary(logText),
                detail: updateInfo.updateAvailable ? 'Update available.' : 'FXRoute is up to date.',
                ...updateInfo,
                log: logText,
                pending: false,
                restartPending: false,
                operation: '',
                userCollapsedDetails: false,
                hasError: false,
            };
        } catch (error) {
            const errorMsg = error.message || 'Update check failed';
            const isDirtyBlock = errorMsg.includes('Local changes detected');
            deps.getState().settings.maintenance = {
                ...deps.getState().settings.maintenance,
                installedVersion: data.installed_version || deps.getState().settings.maintenance.installedVersion || '',
                latestSummary: isDirtyBlock
                    ? 'Local source changes detected. Update disabled to protect this checkout.'
                    : 'Update check failed.',
                detail: errorMsg,
                log: errorMsg,
                pending: false,
                restartPending: false,
                operation: '',
                userCollapsedDetails: false,
                hasError: true,
                dirtyBlock: isDirtyBlock,
            };
        }
        deps.renderSettingsPanel();
    }

    async function waitForFxrouteRestart() {
        const deadline = Date.now() + 30000;
        await new Promise(resolve => setTimeout(resolve, 1200));
        while (Date.now() < deadline) {
            try {
                const resp = await fetch('/api/status', { cache: 'no-store' });
                if (resp.ok) {
                    deps.getState().settings.maintenance.restartPending = false;
                    deps.getState().settings.maintenance.operation = '';
                    deps.getState().settings.maintenance.detail = 'Reload/restart completed. Refresh the page if the interface still shows old assets.';
                    deps.renderSettingsPanel();
                    deps.showToast('FXRoute restart completed', 'success');
                    return;
                }
            } catch (e) {
                // The service is expected to disappear briefly during restart.
            }
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
        deps.getState().settings.maintenance.restartPending = false;
        deps.getState().settings.maintenance.operation = '';
        deps.getState().settings.maintenance.detail = 'Update finished, but restart confirmation timed out. Check fxroute-status on the host.';
        deps.renderSettingsPanel();
    }

    async function runFxrouteUpdate() {
        deps.getState().settings.maintenance.pending = true;
        deps.getState().settings.maintenance.operation = 'update';
        deps.getState().settings.maintenance.detail = 'Updating FXRoute…';
        deps.getState().settings.maintenance.hasError = false;
        deps.getState().settings.maintenance.userCollapsedDetails = false;
        deps.renderSettingsPanel();
        let data = {};
        try {
            const resp = await fetch('/api/system/update', { method: 'POST' });
            data = await resp.json().catch(() => ({}));
            const logText = updateLogFromResult(data);
            if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Update failed');
            const updateInfo = parseUpdateInfo(logText);
            deps.getState().settings.maintenance = {
                ...deps.getState().settings.maintenance,
                installedVersion: data.installed_version || deps.getState().settings.maintenance.installedVersion,
                latestSummary: 'Update completed.',
                detail: data.restart_scheduled ? 'Restarting FXRoute service…' : 'Update completed.',
                currentVersion: updateInfo.currentVersion || data.installed_version || deps.getState().settings.maintenance.currentVersion,
                latestVersion: updateInfo.latestVersion || deps.getState().settings.maintenance.latestVersion,
                updateAvailable: false,
                log: logText,
                pending: false,
                restartPending: !!data.restart_scheduled,
                operation: data.restart_scheduled ? 'update' : '',
                userCollapsedDetails: false,
                hasError: false,
                dirtyBlock: false,
            };
            deps.renderSettingsPanel();
            if (data.restart_scheduled) {
                void waitForFxrouteRestart();
            } else {
                deps.showToast('FXRoute update completed', 'success');
            }
        } catch (error) {
            const errorMsg = error.message || 'Update failed';
            const isDirtyBlock = errorMsg.includes('Local changes detected');
            deps.getState().settings.maintenance.pending = false;
            deps.getState().settings.maintenance.restartPending = false;
            deps.getState().settings.maintenance.operation = '';
            deps.getState().settings.maintenance.installedVersion = data.installed_version || deps.getState().settings.maintenance.installedVersion || '';
            deps.getState().settings.maintenance.latestSummary = isDirtyBlock
                ? 'Local source changes detected. Update disabled to protect this checkout.'
                : 'Update failed.';
            deps.getState().settings.maintenance.detail = errorMsg;
            deps.getState().settings.maintenance.log = errorMsg;
            deps.getState().settings.maintenance.hasError = true;
            deps.getState().settings.maintenance.userCollapsedDetails = false;
            deps.getState().settings.maintenance.dirtyBlock = isDirtyBlock;
            deps.renderSettingsPanel();
            deps.showToast(errorMsg, 'error');
        }
    }

    async function restoreFxrouteToPublic() {
        const confirmMsg = [
            'This will reset the FXRoute checkout to the current public release on GitHub. ',
            'Tracked source changes will be saved as a patch file and untracked files as an archive, both in backups/. ',
            'User data, music, config, and runtime cache are not affected. ',
            'The service will restart after restore.\n\nContinue?'
        ].join('');
        if (!deps.confirmDialog(confirmMsg)) return;

        deps.getState().settings.maintenance.pending = true;
        deps.getState().settings.maintenance.operation = 'restore';
        deps.getState().settings.maintenance.detail = 'Restoring to public release…';
        deps.getState().settings.maintenance.hasError = false;
        deps.getState().settings.maintenance.userCollapsedDetails = false;
        deps.renderSettingsPanel();
        let data = {};
        try {
            const resp = await fetch('/api/system/restore', { method: 'POST' });
            data = await resp.json().catch(() => ({}));
            const logText = updateLogFromResult(data);
            if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Restore failed');
            deps.getState().settings.maintenance = {
                ...deps.getState().settings.maintenance,
                installedVersion: data.installed_version || deps.getState().settings.maintenance.installedVersion,
                latestSummary: 'Restore completed.',
                detail: 'Restarting FXRoute service…',
                currentVersion: data.installed_version || deps.getState().settings.maintenance.currentVersion,
                latestVersion: '',
                updateAvailable: null,
                log: logText,
                pending: false,
                restartPending: true,
                operation: 'restore',
                userCollapsedDetails: false,
                hasError: false,
                dirtyBlock: false,
            };
            deps.renderSettingsPanel();
            void waitForFxrouteRestart();
        } catch (error) {
            const errorMsg = error.message || 'Restore failed';
            deps.getState().settings.maintenance.pending = false;
            deps.getState().settings.maintenance.restartPending = false;
            deps.getState().settings.maintenance.operation = '';
            deps.getState().settings.maintenance.installedVersion = data.installed_version || deps.getState().settings.maintenance.installedVersion || '';
            deps.getState().settings.maintenance.latestSummary = 'Restore failed.';
            deps.getState().settings.maintenance.detail = errorMsg;
            deps.getState().settings.maintenance.log = errorMsg;
            deps.getState().settings.maintenance.hasError = true;
            deps.getState().settings.maintenance.userCollapsedDetails = false;
            deps.getState().settings.maintenance.dirtyBlock = false;
            deps.renderSettingsPanel();
            deps.showToast(errorMsg, 'error');
        }
    }


    async function applyDeviceName(value) {
        const name = String(value || '').trim().toLowerCase();
        if (!name) {
            deps.showToast('Enter a device name first', 'info');
            return;
        }
        if (!/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(name) || name === 'localhost') {
            deps.showToast('Use only lowercase letters, digits and hyphens (no leading/trailing hyphen)', 'error');
            return;
        }
        deps.getState().settings.deviceName.pending = true;
        renderDeviceNameSettings();
        try {
            const resp = await fetch('/api/system/device-name', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hostname: name }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Could not change the device name');
            deps.getState().settings.deviceName.value = data.hostname || name;
            if (deps.getElements().settingsDeviceNameInput) deps.getElements().settingsDeviceNameInput.value = deps.getState().settings.deviceName.value;
            deps.showToast(data.changed === false ? 'Device name unchanged.' : `Device name set to ${deps.getState().settings.deviceName.value}.local`, 'success');
        } catch (error) {
            deps.showToast(error.message || 'Could not change the device name', 'error');
        } finally {
            deps.getState().settings.deviceName.pending = false;
            renderDeviceNameSettings();
        }
    }

    function renderDeviceNameSettings() {
        if (!deps.getElements().settingsDeviceNameInput) return;
        if (!deps.getState().settings.deviceName.loaded) {
            deps.getElements().settingsDeviceNameInput.placeholder = 'Loading…';
            return;
        }
        if (typeof document !== 'undefined' && document.activeElement !== deps.getElements().settingsDeviceNameInput) {
            deps.getElements().settingsDeviceNameInput.value = deps.getState().settings.deviceName.value || '';
            deps.getElements().settingsDeviceNameInput.placeholder = deps.getState().settings.deviceName.value || 'fxroute';
        }
        const changeBlocked = deps.getState().settings.deviceName.canChange === false;
        deps.getElements().settingsDeviceNameInput.disabled = changeBlocked || deps.getState().settings.deviceName.pending;
        if (deps.getElements().settingsDeviceNameApply) {
            deps.getElements().settingsDeviceNameApply.disabled = deps.getState().settings.deviceName.pending || changeBlocked;
        }
        if (deps.getElements().settingsDeviceNameHint) {
            deps.getElements().settingsDeviceNameHint.textContent = deps.getState().settings.deviceName.canChange === false
                ? 'This system does not support changing the device name here.'
                : `Reach this FXRoute as http://${deps.getState().settings.deviceName.value || 'fxroute'}.local:8000. A change takes effect after a moment.`;
        }
    }


    function musicLibrarySelectModel(musicLibrary = {}) {
        const libraries = Array.isArray(musicLibrary.libraries) ? musicLibrary.libraries : [];
        if (libraries.length === 0 && musicLibrary.loading) {
            return {
                html: '<option value="">Discovering network shares…</option>',
                value: '',
                disabled: true,
            };
        }
        const hasSmb = libraries.some((library) => library && library.type === 'smb');
        if (musicLibrary.scanning && !hasSmb) {
            return {
                html: '<option value="">Scanning network shares…</option>',
                value: '',
                disabled: true,
            };
        }
        return {
            html: libraries
                .map((library) => `<option value="${deps.escapeHtml(library.id || '')}">${deps.escapeHtml(library.label || '')}</option>`)
                .join('') || '<option value="local">Local</option>',
            value: musicLibrary.active_id || 'local',
            disabled: !!musicLibrary.pending || !!musicLibrary.loading,
        };
    }

    async function fetchMusicLibraries() {
        deps.getState().settings.musicLibrary = { ...(deps.getState().settings.musicLibrary || {}), loading: true };
        deps.renderSettingsPanel();
        try {
            const resp = await fetch('/api/music-libraries');
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to discover music libraries');
            deps.getState().settings.musicLibrary = { ...data, pending: false, loading: false, scanning: !!data.discovery_refreshing };
            deps.renderSettingsPanel();
            // Stale-while-revalidate: the response above already shows cached
            // shares; when the backend refreshed in the background, fetch once
            // more so new shares appear without another user action. The timer
            // dies with the dialog (stopSettingsStatusPolling).
            if (musicLibraryRefreshTimer) {
                clearTimeout(musicLibraryRefreshTimer);
                musicLibraryRefreshTimer = null;
            }
            if (data.discovery_refreshing) {
                musicLibraryRefreshTimer = setTimeout(() => {
                    musicLibraryRefreshTimer = null;
                    if (deps.getElements().settingsPanel && !deps.getElements().settingsPanel.classList.contains('hidden')) {
                        void fetchMusicLibraries();
                    }
                }, 5000);
            }
        } catch (error) {
            deps.getState().settings.musicLibrary = { ...(deps.getState().settings.musicLibrary || {}), loading: false, scanning: false };
            deps.renderSettingsPanel();
            console.debug('Failed to discover music libraries', error);
        }
    }

    async function selectMusicLibrary(libraryId) {
        const previousId = deps.getState().settings.musicLibrary.active_id;
        if (musicLibraryRefreshTimer) {
            clearTimeout(musicLibraryRefreshTimer);
            musicLibraryRefreshTimer = null;
        }
        deps.getState().settings.musicLibrary.pending = true;
        deps.renderSettingsPanel();
        try {
            const resp = await fetch('/api/music-libraries/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: libraryId }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to switch music library');
            deps.getState().settings.musicLibrary = { ...data, pending: false };
            deps.getState().library.tracks = [];
            deps.getState().library.selectedTrackIds = [];
            deps.getState().library.currentFolder = '';
            deps.getState().library.albums = [];
            deps.getState().library.albumsLoaded = false;
            deps.getState().library.albumDetail = null;
            deps.getState().library.scanning = true;
            deps.renderSettingsPanel();
            deps.renderLibraryView();
            await deps.fetchLibraryStatus();
            deps.showToast('Music library switched', 'success');
        } catch (error) {
            deps.getState().settings.musicLibrary.active_id = previousId;
            deps.getState().settings.musicLibrary.pending = false;
            deps.renderSettingsPanel();
            deps.showToast(error.message || 'Failed to switch music library', 'error');
        }
    }

    async function addManualMusicLibrary(url) {
        if (musicLibraryRefreshTimer) {
            clearTimeout(musicLibraryRefreshTimer);
            musicLibraryRefreshTimer = null;
        }
        try {
            const resp = await fetch('/api/music-libraries/manual', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to add network share');
            deps.getState().settings.musicLibrary = { ...data, pending: false };
            deps.renderSettingsPanel();
            await selectMusicLibrary(data.entry.id);
        } catch (error) {
            deps.showToast(error.message || 'Failed to add network share', 'error');
        }
    }


    function normalizeHardwareStatus(data = {}) {
        return {
            available: data.available !== false,
            connected: !!data.connected,
            device: data.device || null,
            status: data.status || {},
            raw: data.raw || null,
            input: data.input || data.status?.INPUT || null,
            power: data.power ?? data.status?.POWER ?? null,
            trigger: data.trigger ?? data.status?.TRIGGER ?? null,
            auto: data.auto ?? data.status?.AUTO ?? null,
            notes: Array.isArray(data.notes) ? data.notes : [],
            pending: false,
        };
    }

    async function fetchHardwareStatus() {
        try {
            const resp = await fetch('/api/hardware/status');
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to fetch hardware status');
            deps.getState().settings.hardware = normalizeHardwareStatus(data);
        } catch (e) {
            deps.getState().settings.hardware = {
                available: false,
                connected: false,
                device: null,
                status: {},
                raw: null,
                input: null,
                power: null,
                trigger: null,
                auto: null,
                notes: [e.message || 'Failed to fetch hardware status'],
                pending: false,
            };
        }
        deps.renderSettingsPanel();
    }

    async function runHardwareCommand(endpoint, successMessage) {
        deps.getState().settings.hardware.pending = true;
        deps.renderSettingsPanel();
        try {
            const resp = await fetch(endpoint, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Hardware command failed');
            deps.getState().settings.hardware = normalizeHardwareStatus(data);
            if (deps.getState().settings.hardware.connected) {
                deps.showToast(successMessage, 'success');
            } else {
                const note = deps.getState().settings.hardware.notes?.[0];
                deps.showToast(note || 'Hardware controller not connected', 'warning');
            }
        } catch (e) {
            deps.getState().settings.hardware.pending = false;
            deps.showToast(e.message || 'Hardware command failed', 'error');
            void fetchHardwareStatus();
        }
        deps.renderSettingsPanel();
    }


    function updatePowerButtonConnectionState() {
        if (!deps.getElements().powerMenuToggle) return;
        const online = !!deps.getState().wsConnected;
        deps.getElements().powerMenuToggle.classList.toggle('is-online', online);
        deps.getElements().powerMenuToggle.title = online ? 'FXRoute online' : 'FXRoute offline';
        deps.getElements().powerMenuToggle.setAttribute('aria-label', online ? 'System power (online)' : 'System power (offline)');
    }


    // System power menu (suspend / shut down via systemd-logind / D-Bus)
    // =================================================================
    // The backend reports the textual logind CanSuspend / CanPowerOff state
    // and exposes two narrow POST endpoints (suspend / power-off).  This
    // module only fetches capabilities, toggles a tiny dropdown and asks
    // for confirmation before the destructive POST.
    const POWER_CAPABILITIES_REFRESH_MS = 60 * 1000;
    const POWER_CONFIRM_SHUTDOWN = 'Shut down the host now? Active playback will stop.';
    const POWER_CONFIRM_SUSPEND = 'Suspend the host now? Active playback will stop.';

    function setupPowerMenu() {
        if (!deps.getElements().powerMenuRoot || !deps.getElements().powerMenuToggle || !deps.getElements().powerMenu) return;
        deps.getElements().powerMenuToggle.addEventListener('click', ev => {
            ev.stopPropagation();
            const open = !deps.getElements().powerMenu.classList.contains('hidden');
            setPowerMenuOpen(!open);
        });
        deps.getElements().powerMenu.addEventListener('click', ev => ev.stopPropagation());
        if (deps.getElements().powerSuspend) {
            deps.getElements().powerSuspend.addEventListener('click', () => {
                setPowerMenuOpen(false);
                handlePowerAction('suspend');
            });
        }
        if (deps.getElements().powerShutdown) {
            deps.getElements().powerShutdown.addEventListener('click', () => {
                setPowerMenuOpen(false);
                handlePowerAction('power-off');
            });
        }
        document.addEventListener('click', () => setPowerMenuOpen(false));
        document.addEventListener('keydown', ev => {
            if (ev.key === 'Escape') setPowerMenuOpen(false);
        });
        refreshPowerCapabilities();
        setInterval(refreshPowerCapabilities, POWER_CAPABILITIES_REFRESH_MS);
    }

    function setPowerMenuOpen(open) {
        if (!deps.getElements().powerMenu || !deps.getElements().powerMenuToggle) return;
        deps.getElements().powerMenu.classList.toggle('hidden', !open);
        deps.getElements().powerMenuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function refreshPowerCapabilities() {
        fetch('/api/system/power')
            .then(resp => resp.ok ? resp.json() : Promise.reject(new Error(`HTTP ${resp.status}`)))
            .then(applyPowerCapabilities)
            .catch(() => applyPowerCapabilities({ available: false, suspend: 'unavailable', power_off: 'unavailable', suspend_supported: false, power_off_supported: false }));
    }

    function applyPowerCapabilities(caps) {
        deps.getState().powerCapabilities = caps || {};
        const suspendSupported = !!(caps && caps.suspend_supported);
        const powerOffSupported = !!(caps && caps.power_off_supported);
        if (!deps.getElements().powerMenuRoot) return;
        const anySupported = suspendSupported || powerOffSupported;
        deps.getElements().powerMenuRoot.classList.toggle('hidden', !anySupported);
        if (deps.getElements().powerSuspend) {
            deps.getElements().powerSuspend.classList.toggle('hidden', !suspendSupported);
        }
        if (deps.getElements().powerShutdown) {
            deps.getElements().powerShutdown.classList.toggle('hidden', !powerOffSupported);
        }
        // Close the menu when both items disappear mid-refresh.
        if (!anySupported) setPowerMenuOpen(false);
    }

    async function handlePowerAction(action) {
        const isShutdown = action === 'power-off';
        const button = isShutdown ? deps.getElements().powerShutdown : deps.getElements().powerSuspend;
        const confirmMsg = isShutdown ? POWER_CONFIRM_SHUTDOWN : POWER_CONFIRM_SUSPEND;
        if (!deps.confirmDialog(confirmMsg)) return;

        if (button) {
            button.dataset.pending = 'true';
            button.classList.add('active');
        }
        const endpoint = isShutdown ? '/api/system/power/power-off' : '/api/system/power/suspend';
        const pendingLabel = isShutdown ? 'Shutting down…' : 'Suspending…';

        try {
            const resp = await fetch(endpoint, { method: 'POST' });
            if (!resp.ok) {
                const detail = await resp.json().catch(() => ({}));
                const message = (detail && detail.detail) || `Power action failed (${resp.status})`;
                deps.showToast(message, 'error');
                return;
            }
            // Expect the host to drop the websocket within a few seconds.
            deps.showToast(pendingLabel, 'info');
        } catch (e) {
            deps.showToast(e && e.message ? e.message : 'Power action failed', 'error');
        } finally {
            if (button) {
                button.dataset.pending = 'false';
                button.classList.remove('active');
            }
        }
    }
    return {
        init,
        stopMusicLibraryRefresh,
        updateLogFromResult,
        parseUpdateSummary,
        parseUpdateVersion,
        parseUpdateInfo,
        maintenanceStatusText,
        renderMaintenancePanel,
        checkFxrouteUpdate,
        waitForFxrouteRestart,
        runFxrouteUpdate,
        restoreFxrouteToPublic,
        applyDeviceName,
        renderDeviceNameSettings,
        musicLibrarySelectModel,
        fetchMusicLibraries,
        selectMusicLibrary,
        addManualMusicLibrary,
        formatHardwareBool,
        renderHardwareController,
        normalizeHardwareStatus,
        fetchHardwareStatus,
        runHardwareCommand,
        updatePowerButtonConnectionState,
        setupPowerMenu,
        setPowerMenuOpen,
        refreshPowerCapabilities,
        applyPowerCapabilities,
        handlePowerAction,
    };
});
