// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute provider settings: install/update/service, visibility toggle and
 * the Qobuz/TIDAL account login dialogs for the Technical settings panel.
 * Owns the provider rows, the busy marking and the login modals; app-owned
 * rows (device name), streaming tab refreshes and runtime polling/playback
 * stay behind explicit callbacks. Browser-loadable UMD, no build step;
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteProviderSettings = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const PROVIDER_UNINSTALL_CONFIRM = {
        spotify: 'Remove Spotify Connect (spotifyd and desktop integration) from this machine? FXRoute itself stays installed. You can reinstall it later.',
        qobuz: 'Remove the Qobuz renderer (qbzd) from this machine? FXRoute itself stays installed. You can reinstall it later.',
        tidal: 'Remove the TIDAL backend from FXRoute? Your TIDAL session stays on disk. You can reinstall it later.',
    };

    let deps = {
        getState: () => ({ settings: { providers: { list: [], loaded: false, pendingOperation: null, operationLog: '' } } }),
        getElements: () => ({}),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        getModal: () => (root && root.FXRouteModal) || null,
        confirmDialog: (message) => (typeof confirm === 'function' ? confirm(message) : false),
        onAdminPayload: () => {},
        applyProviderEnabledToStreaming: () => {},
        refreshStreamingFlags: () => {},
        refreshStreamingTab: () => {},
    };
    const providerVisibilityRequestIds = new Map();
    const qobuzLoginState = { loginUrl: '', finishing: false, wired: false };
    const tidalLoginState = { loginUrl: '', finishing: false, wired: false };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    async function fetchProviderAdmin() {
        try {
            const resp = await fetch('/api/streaming/providers/admin');
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to load providers');
            deps.getState().settings.providers.list = data.providers || [];
            deps.getState().settings.providers.loaded = true;
            // Device name rides the same payload; app.js owns that row and fills
            // it through the callback so this module never touches device state.
            deps.onAdminPayload(data);
            renderProviderSettings();
        } catch (error) {
            console.debug('Failed to load provider admin state', error);
        }
    }

    async function setProviderEnabled(providerId, enabled) {
        const requestId = (providerVisibilityRequestIds.get(providerId) || 0) + 1;
        providerVisibilityRequestIds.set(providerId, requestId);
        const provider = deps.getState().settings.providers.list.find((p) => p.id === providerId);
        const previous = provider ? provider.enabled : undefined;
        const previousApplied = provider ? provider.enabled !== false : enabled !== false;
        if (provider) provider.enabled = enabled;
        deps.applyProviderEnabledToStreaming(providerId, enabled);
        renderProviderSettings();
        try {
            const resp = await fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/enabled`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Could not save provider visibility');
        } catch (error) {
            if (providerVisibilityRequestIds.get(providerId) !== requestId) return;
            const currentProvider = deps.getState().settings.providers.list.find((p) => p.id === providerId);
            if (currentProvider) currentProvider.enabled = previous;
            deps.applyProviderEnabledToStreaming(providerId, previousApplied);
            renderProviderSettings();
            deps.showToast(error?.message || 'Could not save provider visibility', 'error');
        }
    }

    function renderProviderOperation(providerId, detail, log) {
        deps.getState().settings.providers.pendingOperation = providerId;
        deps.getState().settings.providers.operationLog = log || '';
        if (deps.getElements().settingsProviderOperation) {
            deps.getElements().settingsProviderOperation.classList.toggle('hidden', !log);
            if (deps.getElements().settingsProviderOperationLog) deps.getElements().settingsProviderOperationLog.textContent = log || '';
        }
        if (detail) deps.showToast(detail, 'success');
        renderProviderSettings();
    }

    function isProviderOpBusy(providerId) {
        const pending = deps.getState().settings.providers.pendingOperation;
        return !!pending && pending !== providerId;
    }

    function providerBusyAttribute(providerId) {
        return isProviderOpBusy(providerId) ? ' data-provider-busy="1"' : '';
    }

    function wireProviderActionButtons() {
        if (!deps.getElements().settingsProvidersList) return;
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-busy="1"] button').forEach((button) => {
            button.disabled = true;
            button.title = 'Another provider operation is running';
        });
    }

    async function runProviderInstall(providerId) {
        if (deps.getState().settings.providers.pendingOperation) {
            deps.showToast('Another provider operation is already running. Please wait for it to finish.', 'info');
            return;
        }
        deps.getState().settings.providers.pendingOperation = providerId;
        renderProviderOperation(providerId, '', '');
        try {
            const resp = await fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/install`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Installation failed');
            renderProviderOperation(providerId, data.installed ? 'Provider installed/updated.' : 'Install finished.', data.log || '');
            // A Settings uninstall disables the provider; a reinstall must activate
            // it again exactly like the first install, or its checkbox and tab stay off.
            const provider = deps.getState().settings.providers.list.find((p) => p.id === providerId);
            if (provider && provider.enabled === false) await setProviderEnabled(providerId, true);
        } catch (error) {
            renderProviderOperation(providerId, '', error.message || 'Installation failed');
            deps.showToast(error.message || 'Installation failed', 'error');
        } finally {
            deps.getState().settings.providers.pendingOperation = null;
            renderProviderSettings();
            void fetchProviderAdmin();
            deps.refreshStreamingFlags();
        }
    }

    async function runProviderUninstall(providerId) {
        if (deps.getState().settings.providers.pendingOperation) {
            deps.showToast('Another provider operation is already running. Please wait for it to finish.', 'info');
            return;
        }
        if (!deps.confirmDialog(PROVIDER_UNINSTALL_CONFIRM[providerId] || 'Remove this provider?')) return;
        deps.getState().settings.providers.pendingOperation = providerId;
        renderProviderOperation(providerId, '', '');
        try {
            const resp = await fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/uninstall`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Uninstall failed');
            renderProviderOperation(providerId, 'Provider removed.', data.log || '');
            await setProviderEnabled(providerId, false);
        } catch (error) {
            renderProviderOperation(providerId, '', error.message || 'Uninstall failed');
            deps.showToast(error.message || 'Uninstall failed', 'error');
        } finally {
            deps.getState().settings.providers.pendingOperation = null;
            renderProviderSettings();
            void fetchProviderAdmin();
            deps.refreshStreamingFlags();
        }
    }

    async function runProviderServiceAction(providerId, action) {
        if (deps.getState().settings.providers.pendingOperation) {
            deps.showToast('Another provider operation is already running. Please wait for it to finish.', 'info');
            return;
        }
        deps.getState().settings.providers.pendingOperation = providerId;
        renderProviderSettings();
        try {
            const resp = await fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/service/${action}`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || `Service ${action} failed`);
            deps.showToast(action === 'stop' ? 'Service stopped.' : (action === 'restart' ? 'Service restarted.' : 'Service started.'), 'success');
        } catch (error) {
            deps.showToast(error.message || `Service ${action} failed`, 'error');
        } finally {
            deps.getState().settings.providers.pendingOperation = null;
            renderProviderSettings();
            void fetchProviderAdmin();
            deps.refreshStreamingFlags();
        }
    }

    async function beginQobuzLogin() {
        try {
            const resp = await fetch('/api/streaming/qobuz/auth/login', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Could not start the Qobuz login');
            if (!data.login_url) throw new Error('Qobuz login did not return a sign-in URL');
            openQobuzLoginModal(data.login_url);
        } catch (error) {
            deps.showToast(error.message || 'Could not start the Qobuz login', 'error');
        }
    }

    function setQobuzLoginStatus(message, mode = '') {
        if (!deps.getElements().qobuzLoginStatus) return;
        deps.getElements().qobuzLoginStatus.textContent = message || '';
        deps.getElements().qobuzLoginStatus.classList.toggle('switching', mode === 'busy');
    }

    function setQobuzLoginBusy(busy) {
        qobuzLoginState.finishing = !!busy;
        if (deps.getElements().qobuzLoginFinishBtn) deps.getElements().qobuzLoginFinishBtn.disabled = !!busy;
        if (deps.getElements().qobuzLoginCancelBtn) deps.getElements().qobuzLoginCancelBtn.disabled = !!busy;
        if (deps.getElements().qobuzLoginCloseBtn) deps.getElements().qobuzLoginCloseBtn.disabled = !!busy;
        if (deps.getElements().qobuzLoginRedirect) deps.getElements().qobuzLoginRedirect.disabled = !!busy;
    }

    function openQobuzLoginModal(loginUrl) {
        if (!deps.getElements().qobuzLoginPanel) return;
        qobuzLoginState.loginUrl = String(loginUrl || '');
        if (deps.getElements().qobuzLoginUrl) deps.getElements().qobuzLoginUrl.value = qobuzLoginState.loginUrl;
        if (deps.getElements().qobuzLoginOpen) deps.getElements().qobuzLoginOpen.href = qobuzLoginState.loginUrl || '#';
        if (deps.getElements().qobuzLoginRedirect && ((typeof document !== 'undefined' && document.activeElement) || null) !== deps.getElements().qobuzLoginRedirect) {
            deps.getElements().qobuzLoginRedirect.value = '';
        }
        setQobuzLoginBusy(false);
        setQobuzLoginStatus('Waiting for sign-in…');
        deps.getElements().qobuzLoginPanel.classList.remove('hidden');
        // Stacked above the settings dialog: the qbzd listener keeps running
        // while the operator switches tabs; only explicit Cancel ends it.
        deps.getModal()?.open(deps.getElements().qobuzLoginPanel, {
            initialFocus: deps.getElements().qobuzLoginOpen,
            onEscape: () => { if (!qobuzLoginState.finishing) void cancelQobuzLogin(); },
        });
    }

    function closeQobuzLoginModal() {
        if (!deps.getElements().qobuzLoginPanel) return;
        deps.getElements().qobuzLoginPanel.classList.add('hidden');
        deps.getModal()?.close(deps.getElements().qobuzLoginPanel);
    }

    async function cancelQobuzLogin() {
        // Explicit cancel only: never fired by blur, tab switch, or backdrop.
        try {
            await fetch('/api/streaming/qobuz/auth/login/cancel', { method: 'POST' });
        } catch (_error) {
            // Best-effort cleanup; the next Connect click restarts the flow.
        } finally {
            setQobuzLoginBusy(false);
            closeQobuzLoginModal();
        }
    }

    async function copyQobuzLoginUrl() {
        const url = qobuzLoginState.loginUrl || deps.getElements().qobuzLoginUrl?.value || '';
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            deps.showToast('Sign-in link copied.', 'success');
        } catch (_error) {
            try {
                deps.getElements().qobuzLoginUrl?.focus();
                deps.getElements().qobuzLoginUrl?.select();
            } catch (_selectError) { /* input unavailable */ }
            const ok = (typeof document !== 'undefined' && document.execCommand) ? document.execCommand('copy') : false;
            deps.showToast(ok ? 'Sign-in link copied.' : 'Copy the sign-in URL manually.', ok ? 'success' : 'info');
        }
    }

    async function finishQobuzLoginFromModal() {
        if (qobuzLoginState.finishing) return;
        const pasted = String(deps.getElements().qobuzLoginRedirect?.value || '').trim();
        if (!pasted || pasted === qobuzLoginState.loginUrl) {
            setQobuzLoginStatus('Paste the redirect URL from the Qobuz sign-in tab, then press Connect.');
            deps.showToast('Paste the redirect URL first — the login is still waiting.', 'info');
            deps.getElements().qobuzLoginRedirect?.focus();
            return;
        }
        setQobuzLoginBusy(true);
        setQobuzLoginStatus('Completing the Qobuz login…', 'busy');
        try {
            const resp = await fetch('/api/streaming/qobuz/auth/login/finish', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ redirect_url: pasted }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Qobuz login failed');
            if (data.ok === false) {
                throw new Error(data.output || 'qbzd rejected the pasted URL');
            }
            closeQobuzLoginModal();
            deps.showToast(data.authenticated ? 'Qobuz account connected.' : 'Login finished — verifying…', 'success');
        } catch (error) {
            setQobuzLoginStatus(error.message || 'Qobuz login failed — check the pasted URL and try again.');
            deps.showToast(error.message || 'Qobuz login failed', 'error');
        } finally {
            setQobuzLoginBusy(false);
            void fetchProviderAdmin();
        }
    }

    function setupQobuzLoginModal() {
        if (qobuzLoginState.wired || !deps.getElements().qobuzLoginPanel) return;
        qobuzLoginState.wired = true;
        deps.getElements().qobuzLoginCopy?.addEventListener('click', () => void copyQobuzLoginUrl());
        deps.getElements().qobuzLoginFinishBtn?.addEventListener('click', () => void finishQobuzLoginFromModal());
        deps.getElements().qobuzLoginCancelBtn?.addEventListener('click', () => void cancelQobuzLogin());
        deps.getElements().qobuzLoginCloseBtn?.addEventListener('click', () => void cancelQobuzLogin());
        deps.getElements().qobuzLoginRedirect?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                void finishQobuzLoginFromModal();
            }
        });
        // Backdrop clicks must not cancel: the operator leaves this modal open
        // while completing the sign-in in another tab.
    }

    async function beginTidalLogin() {
        try {
            const resp = await fetch('/api/streaming/tidal/auth/pkce', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Could not start the TIDAL login');
            if (!data.url) throw new Error('TIDAL login did not return a sign-in URL');
            openTidalLoginModal(data.url);
        } catch (error) {
            deps.showToast(error.message || 'Could not start the TIDAL login', 'error');
        }
    }

    function setTidalLoginStatus(message, mode = '') {
        if (!deps.getElements().tidalLoginStatus) return;
        deps.getElements().tidalLoginStatus.textContent = message || '';
        deps.getElements().tidalLoginStatus.classList.toggle('switching', mode === 'busy');
    }

    function setTidalLoginBusy(busy) {
        tidalLoginState.finishing = !!busy;
        if (deps.getElements().tidalLoginFinishBtn) deps.getElements().tidalLoginFinishBtn.disabled = !!busy;
        if (deps.getElements().tidalLoginCancelBtn) deps.getElements().tidalLoginCancelBtn.disabled = !!busy;
        if (deps.getElements().tidalLoginCloseBtn) deps.getElements().tidalLoginCloseBtn.disabled = !!busy;
        if (deps.getElements().tidalLoginRedirect) deps.getElements().tidalLoginRedirect.disabled = !!busy;
    }

    function openTidalLoginModal(loginUrl) {
        if (!deps.getElements().tidalLoginPanel) return;
        tidalLoginState.loginUrl = String(loginUrl || '');
        if (deps.getElements().tidalLoginUrl) deps.getElements().tidalLoginUrl.value = tidalLoginState.loginUrl;
        if (deps.getElements().tidalLoginOpen) deps.getElements().tidalLoginOpen.href = tidalLoginState.loginUrl || '#';
        if (deps.getElements().tidalLoginRedirect && ((typeof document !== 'undefined' && document.activeElement) || null) !== deps.getElements().tidalLoginRedirect) {
            deps.getElements().tidalLoginRedirect.value = '';
        }
        setTidalLoginBusy(false);
        setTidalLoginStatus('Waiting for sign-in…');
        deps.getElements().tidalLoginPanel.classList.remove('hidden');
        // Same stacking as the Qobuz dialog: the PKCE flow keeps no server-side
        // listener, but the operator still switches tabs to sign in; only
        // explicit Cancel/Close ends the dialog.
        deps.getModal()?.open(deps.getElements().tidalLoginPanel, {
            initialFocus: deps.getElements().tidalLoginOpen,
            onEscape: () => { if (!tidalLoginState.finishing) closeTidalLoginModal(); },
        });
    }

    function closeTidalLoginModal() {
        if (!deps.getElements().tidalLoginPanel) return;
        deps.getElements().tidalLoginPanel.classList.add('hidden');
        deps.getModal()?.close(deps.getElements().tidalLoginPanel);
    }

    async function copyTidalLoginUrl() {
        const url = tidalLoginState.loginUrl || deps.getElements().tidalLoginUrl?.value || '';
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            deps.showToast('Sign-in link copied.', 'success');
        } catch (_error) {
            try {
                deps.getElements().tidalLoginUrl?.focus();
                deps.getElements().tidalLoginUrl?.select();
            } catch (_selectError) { /* input unavailable */ }
            const ok = (typeof document !== 'undefined' && document.execCommand) ? document.execCommand('copy') : false;
            deps.showToast(ok ? 'Sign-in link copied.' : 'Copy the sign-in URL manually.', ok ? 'success' : 'info');
        }
    }

    async function finishTidalLoginFromModal() {
        if (tidalLoginState.finishing) return;
        const pasted = String(deps.getElements().tidalLoginRedirect?.value || '').trim();
        if (!pasted || pasted === tidalLoginState.loginUrl) {
            setTidalLoginStatus('Paste the redirect URL from the TIDAL sign-in tab, then press Connect.');
            deps.showToast('Paste the redirect URL first — the login is still waiting.', 'info');
            deps.getElements().tidalLoginRedirect?.focus();
            return;
        }
        setTidalLoginBusy(true);
        setTidalLoginStatus('Completing the TIDAL login…', 'busy');
        try {
            const resp = await fetch('/api/streaming/tidal/auth/pkce/finish', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ redirect_url: pasted }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'TIDAL login failed');
            closeTidalLoginModal();
            deps.showToast('TIDAL connected', 'success');
        } catch (error) {
            setTidalLoginStatus(error.message || 'TIDAL login failed — check the pasted URL and try again.');
            deps.showToast(error.message || 'TIDAL login failed', 'error');
        } finally {
            setTidalLoginBusy(false);
            void fetchProviderAdmin();
            deps.refreshStreamingTab();
        }
    }

    function setupTidalLoginModal() {
        if (tidalLoginState.wired || !deps.getElements().tidalLoginPanel) return;
        tidalLoginState.wired = true;
        deps.getElements().tidalLoginCopy?.addEventListener('click', () => void copyTidalLoginUrl());
        deps.getElements().tidalLoginFinishBtn?.addEventListener('click', () => void finishTidalLoginFromModal());
        deps.getElements().tidalLoginCancelBtn?.addEventListener('click', () => closeTidalLoginModal());
        deps.getElements().tidalLoginCloseBtn?.addEventListener('click', () => closeTidalLoginModal());
        deps.getElements().tidalLoginRedirect?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                void finishTidalLoginFromModal();
            }
        });
        // Backdrop clicks must not cancel: the operator leaves this modal open
        // while completing the sign-in in another tab.
    }

    async function qobuzLogout() {
        if (!deps.confirmDialog('Disconnect the Qobuz account? Playback stops until you sign in again. Your Qobuz library and favorites stay on your Qobuz account.')) return;
        try {
            const resp = await fetch('/api/streaming/qobuz/auth/logout', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Disconnect failed');
            deps.showToast('Qobuz account disconnected.', 'success');
        } catch (error) {
            deps.showToast(error.message || 'Disconnect failed', 'error');
        } finally {
            void fetchProviderAdmin();
        }
    }

    async function tidalLogout() {
        if (!deps.confirmDialog('Disconnect the TIDAL account? Playback stops until you sign in again. Your TIDAL library and favorites stay on your TIDAL account.')) return;
        try {
            const resp = await fetch('/api/streaming/tidal/auth/logout', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Disconnect failed');
            deps.showToast('TIDAL account disconnected.', 'success');
        } catch (error) {
            deps.showToast(error.message || 'Disconnect failed', 'error');
        } finally {
            void fetchProviderAdmin();
            deps.refreshStreamingTab();
        }
    }

    function providerAdminButtonHtml(provider) {
        const busy = deps.getState().settings.providers.pendingOperation === provider.id;
        const buttons = [];
        const connected = provider.authenticated === true;
        if (provider.installed) {
            // Account providers (TIDAL, Qobuz) show exactly one auth action for
            // their real state: Disconnect while connected, Connect otherwise.
            // Uninstall is only offered while disconnected. Order is Update
            // first, Connect/Disconnect last, so a line wrap pushes the auth
            // action down while Update and Uninstall keep their position.
            // spotifyd has no account login (Spotify Connect pairs from the
            // Spotify app).
            if (provider.id === 'tidal') {
                buttons.push(`<button type="button" class="btn-secondary" data-provider-install="${provider.id}"${busy ? ' disabled' : ''}>${busy ? 'Updating…' : 'Update…'}</button>`);
                if (connected) {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-tidal-logout="1"${busy ? ' disabled' : ''}>Disconnect</button>`);
                } else {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-uninstall="${provider.id}"${busy ? ' disabled' : ''}>Uninstall…</button>`);
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-tidal-login="1"${busy ? ' disabled' : ''}>Connect</button>`);
                }
            } else if (provider.id === 'qobuz') {
                buttons.push(`<button type="button" class="btn-secondary" data-provider-install="${provider.id}"${busy ? ' disabled' : ''}>${busy ? 'Updating…' : 'Update…'}</button>`);
                if (!connected) {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-uninstall="${provider.id}"${busy ? ' disabled' : ''}>Uninstall…</button>`);
                }
                // Restart is recovery, not a primary action: it makes qbzd
                // re-read a (restored) credential file without SSH and acts as
                // Start on an inactive unit. Only show it while qbzd is down.
                // It stays before the auth action so Connect/Disconnect is last.
                if (!provider.available) {
                    // Binary present but daemon never set up (manually placed
                    // binary or interrupted install): route through the regular
                    // installer run, which adopts the binary, pins the volume
                    // contract, creates/starts the user service and records the
                    // install state. Install is otherwise only offered while not
                    // installed, which dead-ends exactly this state.
                    buttons.push(`<button type="button" class="btn-primary" data-provider-install="${provider.id}"${busy ? ' disabled' : ''}>Complete setup…</button>`);
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-service="restart" data-provider-id="${provider.id}"${busy ? ' disabled' : ''}>Restart</button>`);
                }
                if (connected) {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-qobuz-logout="1"${busy ? ' disabled' : ''}>Disconnect</button>`);
                } else {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-qobuz-login="1"${busy ? ' disabled' : ''}>Connect</button>`);
                }
            } else if (provider.id !== 'spotify') {
                const label = provider.available ? 'Restart' : 'Start';
                buttons.push(`<button type="button" class="btn-secondary" data-provider-service="start" data-provider-id="${provider.id}"${busy ? ' disabled' : ''}>${label}</button>`);
                if (provider.available) {
                    buttons.push(`<button type="button" class="btn-secondary" data-provider-service="stop" data-provider-id="${provider.id}"${busy ? ' disabled' : ''}>Stop</button>`);
                }
                buttons.push(`<button type="button" class="btn-secondary" data-provider-uninstall="${provider.id}"${busy ? ' disabled' : ''}>Uninstall…</button>`);
            } else {
                buttons.push(`<button type="button" class="btn-secondary" data-provider-install="${provider.id}"${busy ? ' disabled' : ''}>${busy ? 'Updating…' : 'Update…'}</button>`);
                buttons.push(`<button type="button" class="btn-secondary" data-provider-uninstall="${provider.id}"${busy ? ' disabled' : ''}>Uninstall…</button>`);
            }
        } else if (provider.implemented !== false) {
            buttons.push(`<button type="button" class="btn-primary" data-provider-install="${provider.id}"${busy ? ' disabled' : ''}>${busy ? 'Installing…' : 'Install…'}</button>`);
        }
        return buttons.join('');
    }

    function renderProviderSettings() {
        if (!deps.getElements().settingsProvidersList) return;
        const providers = deps.getState().settings.providers.list;
        if (!providers.length) {
            deps.getElements().settingsProvidersList.innerHTML = '<p class="settings-inline-note">Provider state unavailable.</p>';
            return;
        }
        deps.getElements().settingsProvidersList.innerHTML = providers.map((provider) => {
            const installed = provider.installed === true;
            const statusText = !installed
                ? 'Not installed'
                : (provider.authenticated === false && provider.id !== 'spotify'
                    ? 'Installed · not connected'
                    : (provider.available ? 'Installed · ready' : 'Installed'));
            const checked = provider.enabled !== false;
            return `
                <div class="settings-provider-row" data-provider-row="${deps.escapeHtml(provider.id)}"${providerBusyAttribute(provider.id)}>
                    <div class="settings-provider-info">
                        <label class="settings-provider-toggle">
                            <input type="checkbox" data-provider-enabled="${deps.escapeHtml(provider.id)}"${checked ? ' checked' : ''} />
                            <span>${deps.escapeHtml(provider.name)}</span>
                        </label>
                        <span class="settings-provider-status">${deps.escapeHtml(statusText)}</span>
                    </div>
                    <div class="settings-provider-actions">${providerAdminButtonHtml(provider)}</div>
                </div>`;
        }).join('');
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-enabled]').forEach((input) => {
            input.addEventListener('change', (event) => {
                setProviderEnabled(event.target.getAttribute('data-provider-enabled'), event.target.checked);
            });
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-install]').forEach((button) => {
            button.addEventListener('click', () => runProviderInstall(button.getAttribute('data-provider-install')));
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-uninstall]').forEach((button) => {
            button.addEventListener('click', () => runProviderUninstall(button.getAttribute('data-provider-uninstall')));
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-service]').forEach((button) => {
            button.addEventListener('click', () => runProviderServiceAction(button.getAttribute('data-provider-id'), button.getAttribute('data-provider-service')));
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-qobuz-login]').forEach((button) => {
            button.addEventListener('click', () => void beginQobuzLogin());
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-qobuz-logout]').forEach((button) => {
            button.addEventListener('click', () => void qobuzLogout());
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-tidal-login]').forEach((button) => {
            button.addEventListener('click', () => {
                const provider = deps.getState().settings.providers.list.find((p) => p.id === 'tidal');
                if (provider && provider.enabled === false) setProviderEnabled('tidal', true);
                void beginTidalLogin();
            });
        });
        deps.getElements().settingsProvidersList.querySelectorAll('[data-provider-tidal-logout]').forEach((button) => {
            button.addEventListener('click', () => void tidalLogout());
        });
        wireProviderActionButtons();
    }

    return {
        init,
        fetchProviderAdmin,
        setProviderEnabled,
        renderProviderOperation,
        isProviderOpBusy,
        providerBusyAttribute,
        wireProviderActionButtons,
        runProviderInstall,
        runProviderUninstall,
        runProviderServiceAction,
        beginQobuzLogin,
        setQobuzLoginStatus,
        setQobuzLoginBusy,
        openQobuzLoginModal,
        closeQobuzLoginModal,
        cancelQobuzLogin,
        copyQobuzLoginUrl,
        finishQobuzLoginFromModal,
        setupQobuzLoginModal,
        beginTidalLogin,
        setTidalLoginStatus,
        setTidalLoginBusy,
        openTidalLoginModal,
        closeTidalLoginModal,
        copyTidalLoginUrl,
        finishTidalLoginFromModal,
        setupTidalLoginModal,
        qobuzLogout,
        tidalLogout,
        providerAdminButtonHtml,
        renderProviderSettings,
    };
});
