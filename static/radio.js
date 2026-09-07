// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute radio station management.
 * Playback remains owned by app.js.
 */
(function () {
    let initialized = false;
    let api = null;
    let elements = null;
    const ONLINE_SEARCH_DEBOUNCE_MS = 350;
    const MAX_ONLINE_SEARCH_RESULTS = 30;
    const state = {
        get stations() { return api.getStations(); },
        set stations(value) { api.setStations(value); },
        catalogStations: [],
        onlineStations: null,
        onlineStatus: '',
        onlineRequestId: 0,
        onlineSearchTimer: null,
    };
    let showToast;
    let escapeHtml;
    let highlightActiveTrack;
    let extractDroppedUrl;
    let playRadio;

    function init(interfaceApi) {
        if (initialized) return api.publicApi;
        initialized = true;
        api = interfaceApi;
        showToast = api.showToast;
        escapeHtml = api.escapeHtml;
        highlightActiveTrack = api.highlightActiveTrack;
        extractDroppedUrl = api.extractDroppedUrl;
        playRadio = api.playStation;
        elements = {
        stationsGrid: document.getElementById('stations-grid'),
        stationSearchInput: document.getElementById('station-search'),
        stationSearchClear: document.getElementById('station-search-clear'),
        stationExportAllBtn: document.getElementById('station-export-all'),
        stationsEmptySearch: document.getElementById('stations-empty-search'),
        stationCatalogGrid: document.getElementById('station-catalog-grid'),
        stationCatalogLoading: document.getElementById('station-catalog-loading'),
        stationCatalogEmptySearch: document.getElementById('station-catalog-empty-search'),
        stationCatalogSection: document.getElementById('station-catalog-section'),
        stationSections: document.getElementById('radio-station-sections'),
        stationSearchResults: document.getElementById('station-search-results'),
        stationSearchStatus: document.getElementById('station-search-status'),
        stationSearchGrid: document.getElementById('station-search-grid'),
        toggleStationManageBtn: document.getElementById('toggle-station-manage'),
        closeStationManageBtn: document.getElementById('close-station-manage'),
        radioManagePanel: document.getElementById('radio-manage-panel'),
        stationNameGroup: document.getElementById('station-name-group'),
        stationName: document.getElementById('station-name'),
        stationImageGroup: document.getElementById('station-image-group'),
        stationImageUrl: document.getElementById('station-image-url'),
        stationUrlDropArea: document.getElementById('station-url-drop-area'),
        stationUrlHint: document.getElementById('station-url-hint'),
        stationUrl: document.getElementById('station-url'),
        stationSaveRow: document.getElementById('station-save-row'),
        stationSaveBtn: document.getElementById('station-save'),
        stationDeleteSelect: document.getElementById('station-delete-select'),
        stationExistingFields: document.getElementById('station-existing-fields'),
        stationExistingUrl: document.getElementById('station-existing-url'),
        stationExistingImageUrl: document.getElementById('station-existing-image-url'),
        stationUpdateBtn: document.getElementById('station-update'),
        stationDeleteBtn: document.getElementById('station-delete'),
        stationFormStatus: document.getElementById('station-form-status'),
        };
        setupStationActions();
        api.publicApi = { fetchStations, renderStations, renderStationDeleteOptions, setStations };
        return api.publicApi;
    }

    function setStations(stations) {
        state.stations = Array.isArray(stations) ? stations : [];
        renderStations();
        renderStationDeleteOptions();
    }

    function clearStationFormStatus() {
        if (elements.stationFormStatus) {
            elements.stationFormStatus.textContent = '';
        }
    }
    
    function selectedManagedStation() {
        const stationId = elements.stationDeleteSelect?.value || '';
        if (!stationId) return null;
        return (Array.isArray(state.stations) ? state.stations : []).find(item => item.id === stationId) || null;
    }
    
    function populateManagedStationFields() {
        const station = selectedManagedStation();
        const hasStation = !!station;
        if (elements.stationExistingFields) {
            elements.stationExistingFields.classList.toggle('hidden', !hasStation);
        }
        if (elements.stationExistingUrl) {
            elements.stationExistingUrl.value = hasStation ? (station.input_url || station.stream_url || '') : '';
        }
        if (elements.stationExistingImageUrl) {
            elements.stationExistingImageUrl.value = hasStation ? (station.custom_image_url || '') : '';
        }
    }
    
    function resetManagedStationForm() {
        if (elements.stationDeleteSelect) {
            elements.stationDeleteSelect.value = '';
        }
        populateManagedStationFields();
        updateStationActionButtons();
    }
    
    function setupStationActions() {
        if (elements.stationSaveBtn) elements.stationSaveBtn.addEventListener('click', () => saveStation());
        if (elements.stationUpdateBtn) {
            elements.stationUpdateBtn.addEventListener('click', saveManagedStationChanges);
        }
        if (elements.stationDeleteBtn) elements.stationDeleteBtn.addEventListener('click', deleteSelectedStation);
        if (elements.toggleStationManageBtn) elements.toggleStationManageBtn.addEventListener('click', () => toggleStationManagePanel(true));
        if (elements.closeStationManageBtn) elements.closeStationManageBtn.addEventListener('click', () => toggleStationManagePanel(false));
        elements.radioManagePanel?.querySelector('.manage-overlay-backdrop')?.addEventListener('click', () => toggleStationManagePanel(false));
        if (elements.stationUrl) {
            elements.stationUrl.addEventListener('input', () => {
                clearStationFormStatus();
                updateStationNameRequirement();
            });
            elements.stationUrl.addEventListener('paste', () => {
                requestAnimationFrame(() => handleStationUrlReady('Pasted station URL'));
            });
            elements.stationUrl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    handleStationUrlReady('Entered station URL');
                }
            });
        }
        if (elements.stationName) {
            elements.stationName.addEventListener('input', () => {
                clearStationFormStatus();
                updateStationActionButtons();
            });
            elements.stationName.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !elements.stationSaveBtn?.disabled) {
                    e.preventDefault();
                    saveStation();
                }
            });
        }
        if (elements.stationImageUrl) {
            elements.stationImageUrl.addEventListener('input', clearStationFormStatus);
        }
        if (elements.stationDeleteSelect) {
            elements.stationDeleteSelect.addEventListener('change', () => {
                populateManagedStationFields();
                updateStationActionButtons();
            });
        }
        if (elements.stationExistingUrl) {
            elements.stationExistingUrl.addEventListener('input', updateStationActionButtons);
        }
        if (elements.stationExistingImageUrl) {
            elements.stationExistingImageUrl.addEventListener('input', clearStationFormStatus);
        }
        if (elements.stationUrlDropArea) setupStationUrlDropArea();
        if (elements.stationSearchInput) {
            elements.stationSearchInput.addEventListener('input', () => {
                if (elements.stationSearchClear) {
                    elements.stationSearchClear.disabled = !elements.stationSearchInput.value;
                }
                clearOnlineResults(false);
                renderStations();
                scheduleOnlineStationSearch();
            });
        }
        if (elements.stationSearchClear) {
            elements.stationSearchClear.addEventListener('click', () => {
                elements.stationSearchInput.value = '';
                elements.stationSearchClear.disabled = true;
                clearOnlineResults();
                elements.stationSearchInput.focus();
            });
        }
        if (elements.stationExportAllBtn) {
            elements.stationExportAllBtn.addEventListener('click', exportAllStations);
        }
        updateStationNameRequirement();
    }

    function activateStationCard(card) {
        const stationId = card?.dataset?.stationId || '';
        if (stationId) playRadio(stationId);
    }

    function handleStationCardKeydown(event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        if (event.target?.closest?.('.station-card-fav')) return;
        event.preventDefault();
        activateStationCard(event.currentTarget);
    }

    function bindStationCardPlayback(container) {
        container?.querySelectorAll('.station-card[data-station-id]').forEach(card => {
            card.addEventListener('click', (event) => {
                if (event.target?.closest?.('.station-card-fav')) return;
                activateStationCard(card);
            });
            card.addEventListener('keydown', handleStationCardKeydown);
        });
    }

    function stationFavButtonHtml(active, extraAttrs) {
        const cls = active ? 'station-card-fav is-active' : 'station-card-fav';
        const heart = active ? '♥' : '♡';
        const pressed = active ? 'true' : 'false';
        const label = active ? 'Remove from My Stations' : 'Add to My Stations';
        return `<button type="button" class="${cls}" ${extraAttrs} aria-pressed="${pressed}" aria-label="${label}" title="${label}">${heart}</button>`;
    }

    function updateStationFavButton(button, active) {
        if (!button) return;
        button.classList.toggle('is-active', !!active);
        button.textContent = active ? '♥' : '♡';
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
        const label = active ? 'Remove from My Stations' : 'Add to My Stations';
        button.setAttribute('aria-label', label);
        button.title = label;
    }

    function bindStationFavButtons(container) {
        container?.querySelectorAll('.station-card-fav[data-station-fav]').forEach(button => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                togglePersonalStationFav(button.dataset.stationFav, button);
            });
        });
        container?.querySelectorAll('.station-card-fav[data-catalog-fav]').forEach(button => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleCatalogStationFav(button.dataset.catalogFav, button);
            });
        });
        container?.querySelectorAll('.station-card-fav[data-browser-fav]').forEach(button => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleOnlineStationFav(button.dataset.browserFav, button);
            });
        });
    }

    function findSavedStationForCatalog(catalogId) {
        const catalogStation = state.catalogStations.find(station => station.id === catalogId);
        if (catalogStation?.saved_station_id) {
            const byId = state.stations.find(station => station.id === catalogStation.saved_station_id);
            if (byId) return byId;
        }
        if (!catalogStation) return null;
        return state.stations.find(station => stationsMatch(station, catalogStation)) || null;
    }

    function isCatalogStationFavorited(catalogStation) {
        if (!catalogStation) return false;
        if (catalogStation.is_saved) return true;
        const savedStations = Array.isArray(state.stations) ? state.stations : [];
        if (catalogStation.saved_station_id
            && savedStations.some(station => station.id === catalogStation.saved_station_id)) {
            return true;
        }
        return savedStations.some(station => stationsMatch(station, catalogStation));
    }

    async function removeSavedStationById(savedId, button) {
        if (!savedId || button?.disabled) return false;
        const saved = state.stations.find(station => station.id === savedId);
        const title = saved?.title || 'Station';
        if (button) button.disabled = true;
        try {
            const resp = await fetch(`/api/stations/${encodeURIComponent(savedId)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to delete station');
            await fetchStations();
            showToast(`Removed from My Stations: ${title}`, 'success');
            return true;
        } catch (e) {
            if (button) button.disabled = false;
            showToast(e.message || 'Failed to delete station', 'error');
            return false;
        }
    }

    async function togglePersonalStationFav(stationId, button) {
        await removeSavedStationById(stationId, button);
    }

    async function toggleCatalogStationFav(catalogId, button) {
        if (!catalogId || button?.disabled) return;
        const catalogStation = state.catalogStations.find(station => station.id === catalogId);
        const saved = findSavedStationForCatalog(catalogId);
        if (saved || catalogStation?.is_saved) {
            const savedId = saved?.id || catalogStation?.saved_station_id || null;
            if (savedId) {
                await removeSavedStationById(savedId, button);
                return;
            }
            await fetchStations();
            return;
        }
        await addCatalogStation(catalogId, button);
    }

    async function toggleOnlineStationFav(stationUuid, button) {
        if (!stationUuid || button?.disabled) return;
        const onlineStation = Array.isArray(state.onlineStations)
            ? state.onlineStations.find(station => station.stationuuid === stationUuid)
            : null;
        let savedId = onlineStation?.saved_station_id || null;
        if (!savedId && onlineStation) {
            savedId = state.stations.find(station => stationsMatch(station, onlineStation))?.id || null;
        }
        if (onlineStation?.is_saved || savedId) {
            if (savedId) {
                await removeSavedStationById(savedId, button);
                return;
            }
            await fetchStations();
            return;
        }
        await addOnlineStation(stationUuid, button);
    }

    function toggleStationManagePanel(forceOpen = null) {
        if (!elements.radioManagePanel) return;
        const shouldOpen = forceOpen === null
            ? elements.radioManagePanel.classList.contains('hidden')
            : !!forceOpen;
        elements.radioManagePanel.classList.toggle('hidden', !shouldOpen);
        resetStationForm();
        resetManagedStationForm();
        if (shouldOpen) {
            window.FXRouteModal?.open(elements.radioManagePanel, {
                initialFocus: elements.closeStationManageBtn,
                onEscape: () => toggleStationManagePanel(false),
            });
        } else {
            window.FXRouteModal?.close(elements.radioManagePanel);
        }
    }
    async function fetchStations() {
        try {
            const [stationsResp, catalogResp] = await Promise.all([
                fetch('/api/stations'),
                fetch('/api/station-catalog'),
            ]);
            if (!stationsResp.ok) throw new Error('Failed to fetch stations');
            if (!catalogResp.ok) throw new Error('Failed to fetch station catalog');
            const stationsData = await stationsResp.json();
            const catalogData = await catalogResp.json();
            if (!Array.isArray(stationsData) || !Array.isArray(catalogData)) throw new Error('Failed to fetch stations');
            state.stations = stationsData;
            state.catalogStations = catalogData;
            syncOnlineSavedState();
            renderStations();
            renderStationDeleteOptions();
        } catch (e) {
            showToast('Failed to load stations', 'error');
        }
    }
    function exportAllStations() {
        if (!Array.isArray(state.stations) || !state.stations.length) {
            showToast('No stations to export', 'warning');
            return;
        }
        const exportData = state.stations.map(st => ({
            name: st.title || st.name || '',
            url: st.stream_url || '',
            logo: st.image_url || st.custom_image_url || '',
            genre: st.artist || '',
        }));
        const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'fxroute-radio-stations.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }
    async function importStationFile(data) {
        let items = [];
        try {
            if (!Array.isArray(data)) throw new Error('Invalid format: expected a JSON array');
            items = data.filter(item => item && (item.url || item.stream_url)).map(item => ({
                name: String(item.name ?? item.title ?? '').trim(),
                url: String(item.url ?? item.stream_url ?? '').trim(),
                logo: String(item.logo ?? item.image_url ?? item.custom_image_url ?? '').trim(),
                genre: String(item.genre ?? item.artist ?? '').trim(),
            })).filter(item => item.url);
        } catch (err) {
            showToast(err?.message || 'Invalid station file', 'error');
            return;
        }
        if (!items.length) {
            showToast('No valid stations found in file', 'error');
            return;
        }
        showToast('Importing ' + items.length + ' station' + (items.length > 1 ? 's' : '') + '…', 'info');
        try {
            const resp = await fetch('/api/stations/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(items),
            });
            if (!resp.ok) throw new Error('Import failed');
            const result = await resp.json();
            const ok = (result.results || []).filter(r => r.status === 'ok').length;
            const err = (result.results || []).filter(r => r.status === 'error').length;
            const skipped = (result.results || []).filter(r => r.status === 'skipped').length;
            if (ok > 0) {
                showToast('Imported ' + ok + ' station' + (ok > 1 ? 's' : '') + (err > 0 ? ', ' + err + ' failed' : '') + (skipped > 0 ? ', ' + skipped + ' skipped' : ''), err > 0 ? 'warning' : 'success');
                await fetchStations();
            } else {
                showToast('No stations imported' + (err > 0 ? ', ' + err + ' failed' : ''), 'error');
            }
        } catch (e) {
            showToast('Import failed: ' + (e.message || 'unknown error'), 'error');
        }
    }
    function stationArtFallbackSvg(station) {
        const title = String(station.title || station.name || 'Radio');
        const genre = String(station.artist || 'Radio');
        const seed = `${station.id || ''}-${title}`;
        let hash = 0;
        for (let i = 0; i < seed.length; i++) hash = ((hash << 5) - hash) + seed.charCodeAt(i);
        const palettes = [
            ['#6ee7b7', '#065f46', '#d1fae5'],
            ['#93c5fd', '#1e3a8a', '#dbeafe'],
            ['#c4b5fd', '#4c1d95', '#ede9fe'],
            ['#f9a8d4', '#9d174d', '#fce7f3'],
            ['#fcd34d', '#92400e', '#fef3c7'],
            ['#67e8f9', '#155e75', '#cffafe']
        ];
        const [bg, fg, accent] = palettes[Math.abs(hash) % palettes.length];
        const words = title.split(/\s+/).filter(Boolean);
        const initials = (words[0]?.[0] || '') + (words[1]?.[0] || words[0]?.[1] || '');
        const label = escapeHtml((initials || 'R').toUpperCase());
        const chip = escapeHtml(genre.slice(0, 16));
        const svg = `
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
                <defs>
                    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
                        <stop offset="0%" stop-color="${bg}"/>
                        <stop offset="100%" stop-color="${fg}"/>
                    </linearGradient>
                    <radialGradient id="glow" cx="30%" cy="22%" r="75%">
                        <stop offset="0%" stop-color="rgba(255,255,255,0.28)"/>
                        <stop offset="100%" stop-color="rgba(255,255,255,0)"/>
                    </radialGradient>
                </defs>
                <rect width="128" height="128" rx="24" fill="url(#g)"/>
                <rect x="1.5" y="1.5" width="125" height="125" rx="22.5" fill="none" stroke="rgba(255,255,255,0.10)"/>
                <rect width="128" height="128" rx="24" fill="url(#glow)"/>
                <circle cx="96" cy="28" r="9" fill="rgba(255,255,255,0.16)"/>
                <circle cx="96" cy="28" r="3.5" fill="${accent}" fill-opacity="0.9"/>
                <g fill="none" stroke="rgba(255,255,255,0.32)" stroke-width="3" stroke-linecap="round">
                    <path d="M28 97c9-9 25-9 34 0"/>
                    <path d="M22 90c13-13 34-13 47 0"/>
                    <path d="M16 83c17-18 43-18 59 0"/>
                </g>
                <text x="64" y="66" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="42" font-weight="800" letter-spacing="1" fill="white">${label}</text>
                <rect x="30" y="88" width="68" height="18" rx="9" fill="rgba(12,16,24,0.22)" stroke="rgba(255,255,255,0.14)"/>
                <text x="64" y="100.5" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="10.5" font-weight="700" fill="${accent}">${chip}</text>
            </svg>`;
        return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
    }
    
    function inferSomaStationSlug(station) {
        const knownLocalArtSlugs = new Set(['groovesalad', 'suburbsofgoa', 'thetrip', 'poptron', 'dubstep', 'live', 'gsclassic', '7soul']);
        const inputUrl = (station?.input_url || station?.url || '').trim();
        const match = inputUrl.match(/somafm\.com\/([^/?#]+)/i);
        if (match && match[1]) {
            const slug = match[1].replace(/(256|130)?\.pls$/i, '').trim().toLowerCase();
            if (knownLocalArtSlugs.has(slug)) return slug;
        }
        const title = (station?.title || station?.name || '').trim().toLowerCase();
        const knownSlugs = {
            'groove salad': 'groovesalad',
            'suburbs of goa': 'suburbsofgoa',
            'the trip': 'thetrip',
            'poptron': 'poptron',
            'dub step beyond': 'dubstep',
            'dubstep beyond': 'dubstep',
            'somafm live': 'live',
            'groove salad classic': 'gsclassic',
            'seven inch soul': '7soul',
        };
        return knownSlugs[title] || '';
    }
    
    function stationArtCandidates(station) {
        const seen = new Set();
        const candidates = [];
        const push = (value) => {
            const cleaned = (value || '').trim();
            if (!cleaned || seen.has(cleaned)) return;
            seen.add(cleaned);
            candidates.push(cleaned);
        };
    
        const somaSlug = inferSomaStationSlug(station);
        if (somaSlug) {
            push(`/fxroute/static/station-art/${somaSlug}.png`);
            push(`/fxroute/static/station-art/${somaSlug}.jpg`);
            push(`/fxroute/static/station-art/${somaSlug}.jpeg`);
            push(`/fxroute/static/station-art/${somaSlug}.webp`);
        }

        push(station.custom_image_url);
        push(station.favicon);
        push(station.image_url);
        push(station.logo_url);
        push(station.logo);
        push(station.image);
    
        push(stationArtFallbackSvg(station));
        return candidates;
    }
    
    function stationArtUrl(station) {
        return stationArtCandidates(station)[0] || stationArtFallbackSvg(station);
    }

    function stationArtCandidatesAttribute(candidates) {
        return encodeURIComponent(JSON.stringify(candidates));
    }

    function bindStationArtFallbacks(root) {
        root?.querySelectorAll('.station-art').forEach(img => {
            img.addEventListener('error', () => {
                let candidates = [];
                try {
                    candidates = JSON.parse(decodeURIComponent(img.dataset.artCandidates || ''));
                } catch {}
                const currentIndex = Number(img.dataset.artIndex || 0);
                const nextIndex = Number.isFinite(currentIndex) ? currentIndex + 1 : 1;
                const nextSrc = candidates[nextIndex];
                if (nextSrc) {
                    img.dataset.artIndex = String(nextIndex);
                    img.src = nextSrc;
                }
            });
        });
    }
    
    function getStationSearchQuery() {
        return (elements.stationSearchInput?.value || '').trim().toLowerCase();
    }
    
    function stationMatchesSearch(station, query) {
        if (!query) return true;
        const fields = [
            station.title || '',
            station.stream_url || '',
            station.input_url || '',
            station.image_url || '',
            station.custom_image_url || '',
            station.provider || '',
        ];
        return fields.some(f => f.toLowerCase().includes(query));
    }
    
    function renderStations() {
        const searching = !!getStationSearchQuery();
        elements.stationSections?.classList.toggle('hidden', searching);
        elements.stationSearchResults?.classList.toggle('hidden', !searching);
        if (searching) {
            renderSearchResults();
            return;
        }
        renderPersonalStations();
        renderCatalogStations();
        if (elements.stationSearchGrid) elements.stationSearchGrid.innerHTML = '';
        if (elements.stationSearchStatus) elements.stationSearchStatus.textContent = '';
    }

    function renderPersonalStations() {
        const loadingEl = document.querySelector('#tab-radio .content-state');
        if (state.stations.length === 0) {
            window.FXRouteContentState.set(loadingEl, 'empty', 'No stations yet. Open Manage to add one.');
            elements.stationsGrid.innerHTML = '';
            window.FXRouteContentState.hide(elements.stationsEmptySearch);
            renderStationDeleteOptions();
            return;
        }
        window.FXRouteContentState.hide(loadingEl);
        const query = getStationSearchQuery();
        const filtered = state.stations.filter(s => stationMatchesSearch(s, query));
        if (filtered.length === 0 && query) {
            elements.stationsGrid.innerHTML = '';
            window.FXRouteContentState.set(elements.stationsEmptySearch, 'empty', 'No saved stations found');
            return;
        }
        window.FXRouteContentState.hide(elements.stationsEmptySearch);
        elements.stationsGrid.innerHTML = filtered.map(station => {
            const artCandidates = stationArtCandidates(station);
            const artSrc = artCandidates[0] || stationArtFallbackSvg(station);
            const isFallbackArt = artSrc.startsWith('data:image/svg+xml');
            const wrapClass = isFallbackArt ? 'station-art-wrap station-art-wrap--fallback' : 'station-art-wrap station-art-wrap--real';
            const imgClass = isFallbackArt ? 'station-art station-art--fallback' : 'station-art station-art--real';
            return `
            <div class="station-card" data-station-id="${escapeHtml(station.id)}" role="button" tabindex="0">
                <div class="${wrapClass}">
                    <img class="${imgClass}" src="${escapeHtml(artSrc)}" data-art-candidates="${stationArtCandidatesAttribute(artCandidates)}" data-art-index="0" alt="${escapeHtml(station.title)}" loading="lazy" />
                </div>
                ${stationFavButtonHtml(true, `data-station-fav="${escapeHtml(station.id)}"`)}
                <div class="station-name">${escapeHtml(station.title)}</div>
            </div>`;
        }).join('');
        bindStationCardPlayback(elements.stationsGrid);
        bindStationFavButtons(elements.stationsGrid);
        bindStationArtFallbacks(elements.stationsGrid);
        highlightActiveTrack();
    }

    function renderCatalogStations() {
        if (!elements.stationCatalogGrid) return;
        window.FXRouteContentState.hide(elements.stationCatalogLoading);
        const query = getStationSearchQuery();
        // Display-only filter: favorited curated stations stay in My Stations
        // and are hidden here; nothing is deleted or moved.
        const filtered = state.catalogStations.filter(station =>
            !isCatalogStationFavorited(station) && stationMatchesSearch(station, query));
        if (filtered.length === 0) {
            elements.stationCatalogGrid.innerHTML = '';
            window.FXRouteContentState.set(elements.stationCatalogEmptySearch, 'empty',
                query ? 'No catalog stations found' : 'All curated stations are already in My Stations');
            return;
        }
        window.FXRouteContentState.hide(elements.stationCatalogEmptySearch);
        const providerOrder = ['Radio Paradise', 'SomaFM', 'FIP', 'Other Stations'];
        const renderCard = station => {
            const artCandidates = stationArtCandidates(station);
            const artSrc = artCandidates[0] || stationArtFallbackSvg(station);
            const isFallbackArt = artSrc.startsWith('data:image/svg+xml');
            const wrapClass = isFallbackArt ? 'station-art-wrap station-art-wrap--fallback' : 'station-art-wrap station-art-wrap--real';
            const imgClass = isFallbackArt ? 'station-art station-art--fallback' : 'station-art station-art--real';
            const fav = stationFavButtonHtml(!!station.is_saved, `data-catalog-fav="${escapeHtml(station.id)}"`);
            return `
            <div class="station-card catalog-station-card">
                <div class="${wrapClass}">
                    <img class="${imgClass}" src="${escapeHtml(artSrc)}" data-art-candidates="${stationArtCandidatesAttribute(artCandidates)}" data-art-index="0" alt="${escapeHtml(station.title)}" loading="lazy" />
                </div>
                ${fav}
                <div class="station-name">${escapeHtml(station.title)}</div>
            </div>`;
        };
        elements.stationCatalogGrid.innerHTML = providerOrder.map(provider => {
            const stations = filtered.filter(station => station.provider === provider);
            if (!stations.length) return '';
            return `
            <section class="station-catalog-group" aria-labelledby="station-catalog-${escapeHtml(provider.toLowerCase().replace(/[^a-z0-9]+/g, '-'))}">
                <h4 id="station-catalog-${escapeHtml(provider.toLowerCase().replace(/[^a-z0-9]+/g, '-'))}" class="station-catalog-group-title">${escapeHtml(provider)}</h4>
                <div class="stations-grid">${stations.map(renderCard).join('')}</div>
            </section>`;
        }).join('');
        bindStationFavButtons(elements.stationCatalogGrid);
        bindStationArtFallbacks(elements.stationCatalogGrid);
    }

    async function addCatalogStation(catalogId, button) {
        if (!catalogId || button?.disabled) return;
        if (button) button.disabled = true;
        let stationAdded = false;
        try {
            const resp = await fetch(`/api/station-catalog/${encodeURIComponent(catalogId)}/selection`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to add catalog station');
            stationAdded = true;
            const catalogStation = state.catalogStations.find(station => station.id === catalogId);
            if (catalogStation) catalogStation.is_saved = true;
            markCatalogStationSaved(button);
            await fetchStations();
            showToast(`Added to My Stations: ${data.station?.title || 'Station'}`, 'success');
        } catch (e) {
            if (button && !stationAdded) button.disabled = false;
            showToast(e.message || 'Failed to add catalog station', 'error');
        }
    }

    function markCatalogStationSaved(button) {
        if (!button) return;
        button.disabled = true;
        if (button.classList.contains('station-card-fav')) {
            updateStationFavButton(button, true);
            return;
        }
        button.classList.add('catalog-station-action--saved');
        button.textContent = 'Saved';
        button.title = 'Added to My Stations';
    }

    function clearOnlineResults(shouldRender = true) {
        if (state.onlineSearchTimer !== null) {
            clearTimeout(state.onlineSearchTimer);
            state.onlineSearchTimer = null;
        }
        state.onlineRequestId++;
        state.onlineStations = null;
        state.onlineStatus = '';
        if (shouldRender) renderStations();
    }

    function scheduleOnlineStationSearch() {
        if (state.onlineSearchTimer !== null) {
            clearTimeout(state.onlineSearchTimer);
            state.onlineSearchTimer = null;
        }
        const query = (elements.stationSearchInput?.value || '').trim();
        if (!query) return;
        state.onlineSearchTimer = setTimeout(() => {
            state.onlineSearchTimer = null;
            searchOnlineStations(query);
        }, ONLINE_SEARCH_DEBOUNCE_MS);
    }

    function syncOnlineSavedState() {
        if (!Array.isArray(state.onlineStations)) return;
        state.onlineStations.forEach(onlineStation => {
            const saved = state.stations.find(station => {
                const savedInput = (station.input_url || station.stream_url || '').trim();
                const savedStream = (station.stream_url || '').trim();
                return (onlineStation.url && savedInput === onlineStation.url)
                    || (onlineStation.url_resolved && savedStream === onlineStation.url_resolved);
            });
            onlineStation.is_saved = !!saved;
            onlineStation.saved_station_id = saved?.id || null;
        });
    }

    async function searchOnlineStations(queryOverride = '') {
        const query = (queryOverride || elements.stationSearchInput?.value || '').trim();
        if (!query) {
            clearOnlineResults();
            return;
        }
        const params = new URLSearchParams({ query });
        const requestId = ++state.onlineRequestId;
        state.onlineStations = [];
        state.onlineStatus = 'searching';
        renderSearchResults();
        try {
            const resp = await fetch(`/api/station-browser/search?${params.toString()}`);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Radio Browser is unavailable');
            if (requestId !== state.onlineRequestId) return;
            state.onlineStations = Array.isArray(data) ? data : [];
            state.onlineStatus = state.onlineStations.length ? 'results' : 'empty';
            syncOnlineSavedState();
        } catch (e) {
            if (requestId !== state.onlineRequestId) return;
            state.onlineStations = [];
            state.onlineStatus = 'error';
        } finally {
            if (requestId === state.onlineRequestId) {
                renderSearchResults();
            }
        }
    }

    function onlineStationMeta(station) {
        const parts = [];
        const location = [station.country, station.countrycode].filter(Boolean).join(' · ');
        if (location) parts.push(location);
        if (station.language) parts.push(station.language);
        if (station.codec) parts.push(`${station.codec}${station.bitrate ? ` ${station.bitrate} kbps` : ''}`);
        if (station.tags) parts.push(station.tags.split(',').slice(0, 2).join(', '));
        return parts.join(' · ') || 'Online station';
    }

    function stationsMatch(left, right) {
        const norm = (value) => String(value || '').trim();
        const leftUrls = [left.input_url, left.stream_url, left.url, left.url_resolved].map(norm).filter(Boolean);
        const rightUrls = new Set([right.input_url, right.stream_url, right.url, right.url_resolved].map(norm).filter(Boolean));
        return leftUrls.some(url => rightUrls.has(url));
    }

    function searchResultStations() {
        const query = getStationSearchQuery();
        const catalogMatches = state.catalogStations.filter(station => stationMatchesSearch(station, query));
        const onlineMatches = state.onlineStations || [];
        const personalStations = state.stations.filter(station => stationMatchesSearch(station, query));
        [...catalogMatches, ...onlineMatches].forEach(result => {
            const saved = state.stations.find(station =>
                station.id === result.saved_station_id || stationsMatch(station, result));
            if (saved && !personalStations.some(station => station.id === saved.id)) {
                personalStations.push(saved);
            }
        });
        const personal = personalStations.map(station => ({ ...station, searchSource: 'personal' }));
        // Display-only filter: favorited curated stations appear only under
        // My Stations, never simultaneously in the catalog group.
        const catalog = catalogMatches
            .filter(station => !isCatalogStationFavorited(station))
            .filter(station => !personal.some(saved => stationsMatch(saved, station)))
            .map(station => ({ ...station, searchSource: 'catalog' }));
        const online = onlineMatches
            .filter(station => !personal.some(saved => stationsMatch(saved, station)))
            .filter(station => !catalog.some(item => stationsMatch(item, station)))
            .map(station => ({ ...station, searchSource: 'online' }))
            .slice(0, MAX_ONLINE_SEARCH_RESULTS);
        return [...personal, ...catalog, ...online];
    }

    function renderSearchResults() {
        if (!elements.stationSearchGrid || !elements.stationSearchStatus) return;
        const results = searchResultStations();
        if (state.onlineStatus === 'searching') {
            elements.stationSearchStatus.textContent = 'Searching the web…';
        } else if (state.onlineStatus === 'error') {
            elements.stationSearchStatus.textContent = 'Online search is currently unavailable. Local results are shown.';
        } else if (!results.length) {
            elements.stationSearchStatus.textContent = 'No stations found';
        } else {
            elements.stationSearchStatus.textContent = '';
        }
        const resultGroups = [
            { source: 'personal', title: 'My Stations', hint: 'Saved locally' },
            { source: 'catalog', title: 'Station Catalog', hint: 'Curated local stations' },
            { source: 'online', title: 'Web Results', hint: 'Radio Browser' },
        ].map(group => ({
            ...group,
            stations: results.filter(station => station.searchSource === group.source),
        })).filter(group => group.stations.length);
        elements.stationSearchGrid.innerHTML = resultGroups.map(group => `
            <section class="station-result-group station-result-group--${group.source}">
                <div class="station-result-group-header">
                    <h4>${group.title}</h4>
                    <span>${group.hint}</span>
                </div>
                <div class="stations-grid">${group.stations.map(station => {
                    const artCandidates = stationArtCandidates(station);
                    const artSrc = artCandidates[0] || stationArtFallbackSvg(station);
                    const isFallbackArt = artSrc.startsWith('data:image/svg+xml');
                    const wrapClass = isFallbackArt ? 'station-art-wrap station-art-wrap--fallback' : 'station-art-wrap station-art-wrap--real';
                    const imgClass = isFallbackArt ? 'station-art station-art--fallback' : 'station-art station-art--real';
                    let fav = '';
                    if (station.searchSource === 'personal') {
                        fav = stationFavButtonHtml(true, `data-station-fav="${escapeHtml(station.id)}"`);
                    } else if (station.searchSource === 'catalog') {
                        fav = stationFavButtonHtml(!!station.is_saved, `data-catalog-fav="${escapeHtml(station.id)}"`);
                    } else if (station.searchSource === 'online') {
                        fav = station.stationuuid
                            ? stationFavButtonHtml(!!station.is_saved, `data-browser-fav="${escapeHtml(station.stationuuid)}"`)
                            : '';
                    }
                    const cardAttrs = station.searchSource === 'personal'
                        ? ` data-station-id="${escapeHtml(station.id)}" role="button" tabindex="0"`
                        : '';
                    const meta = station.searchSource === 'online'
                        ? `<div class="online-station-meta">${escapeHtml(onlineStationMeta(station))}</div>`
                        : '';
                    return `
                    <div class="station-card ${station.searchSource === 'personal' ? '' : 'catalog-station-card'}"${cardAttrs}>
                        <div class="${wrapClass}">
                            <img class="${imgClass}" src="${escapeHtml(artSrc)}" data-art-candidates="${stationArtCandidatesAttribute(artCandidates)}" data-art-index="0" alt="${escapeHtml(station.title)}" loading="lazy" />
                        </div>
                        ${fav}
                        <div class="station-name">${escapeHtml(station.title)}</div>
                        ${meta}
                    </div>`;
                }).join('')}</div>
            </section>`).join('');
        bindStationCardPlayback(elements.stationSearchGrid);
        bindStationFavButtons(elements.stationSearchGrid);
        bindStationArtFallbacks(elements.stationSearchGrid);
        highlightActiveTrack();
    }

    async function addOnlineStation(stationUuid, button) {
        if (!stationUuid || stationUuid === 'undefined' || button?.disabled) return;
        if (button) button.disabled = true;
        try {
            const resp = await fetch(`/api/station-browser/${encodeURIComponent(stationUuid)}/selection`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to add online station');
            const onlineStation = state.onlineStations?.find(station => station.stationuuid === stationUuid);
            if (onlineStation) {
                onlineStation.is_saved = true;
                onlineStation.saved_station_id = data.station?.id || null;
            }
            markCatalogStationSaved(button);
            await fetchStations();
            showToast(`Added to My Stations: ${data.station?.title || 'Station'}`, 'success');
        } catch (e) {
            if (button) button.disabled = false;
            showToast(e.message || 'Failed to add online station', 'error');
        }
    }
    let stationSaveInFlight = false;
    function updateStationActionButtons() {
        const value = (elements.stationUrl?.value || '').trim();
        const name = (elements.stationName?.value || '').trim();
        const isSoma = isSomaFmUrl(value);
        if (elements.stationSaveBtn) {
            elements.stationSaveBtn.disabled = stationSaveInFlight || !value || (!isSoma && !name);
        }
        const hasManagedStation = !!selectedManagedStation();
        const managedUrl = (elements.stationExistingUrl?.value || '').trim();
        if (elements.stationUpdateBtn) {
            elements.stationUpdateBtn.disabled = !hasManagedStation || !managedUrl;
        }
        if (elements.stationDeleteBtn) {
            elements.stationDeleteBtn.disabled = !hasManagedStation;
        }
    }
    
    function renderStationDeleteOptions() {
        if (!elements.stationDeleteSelect) return;
        if (state.stations.length === 0) {
            elements.stationDeleteSelect.innerHTML = '<option value="">No stations saved yet</option>';
            resetManagedStationForm();
            return;
        }
        const compactOptionTitle = title => {
            const fullTitle = String(title || '');
            return fullTitle.length > 32 ? `${fullTitle.slice(0, 29).trimEnd()}…` : fullTitle;
        };
        elements.stationDeleteSelect.innerHTML = ['<option value="">Select a station…</option>']
            .concat(state.stations.map(station => {
                const fullTitle = String(station.title || '');
                return `<option value="${escapeHtml(station.id)}" title="${escapeHtml(fullTitle)}" aria-label="${escapeHtml(fullTitle)}">${escapeHtml(compactOptionTitle(fullTitle))}</option>`;
            }))
            .join('');
        resetManagedStationForm();
    }
    function isSomaFmUrl(value) {
        return /https?:\/\/(?:[^/]*\.)?somafm\.com\//i.test((value || '').trim()) || /https?:\/\/[^\s]*somafm\.com\//i.test((value || '').trim());
    }
    
    function updateStationNameRequirement() {
        const value = (elements.stationUrl?.value || '').trim();
        const hasUrl = !!value;
        const isSoma = isSomaFmUrl(value);
        const needsManualName = hasUrl && !isSoma;
        if (isSoma && elements.stationImageUrl) {
            elements.stationImageUrl.value = '';
        }
        if (elements.stationNameGroup) {
            elements.stationNameGroup.classList.toggle('hidden', !needsManualName);
        }
        if (elements.stationSaveRow) {
            elements.stationSaveRow.classList.toggle('hidden', !needsManualName);
        }
        if (elements.stationImageGroup) {
            elements.stationImageGroup.classList.toggle('hidden', !needsManualName);
        }
        if (elements.stationUrlHint) {
            elements.stationUrlHint.textContent = !hasUrl
                ? 'SomaFM adds directly.'
                : isSoma
                    ? 'SomaFM detected. It will be added directly.'
                    : 'Other stream detected. Enter a name below, cover URL optional.';
        }
        updateStationActionButtons();
    }
    
    function setStationUrlValue(url, sourceLabel = '') {
        const cleaned = (url || '').trim();
        if (!cleaned || !elements.stationUrl) return;
        elements.stationUrl.value = cleaned;
        clearStationFormStatus();
        if (elements.stationUrlHint) {
            elements.stationUrlHint.textContent = sourceLabel ? `${sourceLabel}: ${cleaned}` : cleaned;
        }
        updateStationNameRequirement();
        elements.stationUrl.focus();
    }
    
    async function handleStationUrlReady(sourceLabel = '') {
        const value = (elements.stationUrl?.value || '').trim();
        const rawMatch = value.match(/https?:\/\/\S+/i);
        if (!rawMatch) {
            return;
        }
        const match = rawMatch[0].replace(/[),.;:"'!\]]+$/, '');
        if (!match) return;
        setStationUrlValue(match, sourceLabel || 'Station URL');
        if (isSomaFmUrl(match)) {
            await saveStation(match);
            return;
        }
        if (elements.stationNameGroup) elements.stationNameGroup.classList.remove('hidden');
        if (elements.stationImageGroup) elements.stationImageGroup.classList.remove('hidden');
        if (elements.stationSaveRow) elements.stationSaveRow.classList.remove('hidden');
        elements.stationName?.focus();
    }
    
    function setupStationUrlDropArea() {
        const area = elements.stationUrlDropArea;
        if (!area) return;
        const activate = () => elements.stationUrl?.focus();
        area.addEventListener('click', activate);
        area.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                activate();
            }
        });
        area.addEventListener('dragover', (e) => {
            e.preventDefault();
            area.classList.add('drag-over');
        });
        area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
        area.addEventListener('drop', async (e) => {
            e.preventDefault();
            area.classList.remove('drag-over');
            const file = e.dataTransfer?.files?.[0];
            if (file && (file.type === 'application/json' || (file.name || '').toLowerCase().endsWith('.json'))) {
                try {
                    const text = await file.text();
                    const data = JSON.parse(text);
                    if (!Array.isArray(data)) {
                        showToast('Invalid format: expected a JSON array', 'error');
                        return;
                    }
                    await importStationFile(data);
                } catch (err) {
                    showToast('Failed to read station file: ' + (err.message || 'unknown error'), 'error');
                }
                return;
            }
            const url = extractDroppedUrl(e.dataTransfer);
            if (!url) {
                showToast('No URL found in dropped content', 'error');
                return;
            }
            setStationUrlValue(url, 'Dropped station URL');
            await handleStationUrlReady('Dropped station URL');
        });
    }
    
    function resetStationForm() {
        if (elements.stationName) elements.stationName.value = '';
        if (elements.stationImageUrl) elements.stationImageUrl.value = '';
        if (elements.stationUrl) elements.stationUrl.value = '';
        clearStationFormStatus();
        updateStationNameRequirement();
        if (elements.stationDeleteSelect) {
            updateStationActionButtons();
        }
    }
    async function saveStation(urlOverride = null) {
        if (stationSaveInFlight) return;
        const name = (elements.stationName?.value || '').trim();
        const streamUrl = (urlOverride || elements.stationUrl?.value || '').trim();
        const customImageUrl = (elements.stationImageUrl?.value || '').trim();
        const soma = isSomaFmUrl(streamUrl);
        if (!streamUrl) {
            showToast('Please enter a station URL', 'error');
            return;
        }
        if (!name && !soma) {
            showToast('Please enter a station name for non-SomaFM streams', 'error');
            return;
        }
        stationSaveInFlight = true;
        updateStationActionButtons();
        if (elements.stationFormStatus) elements.stationFormStatus.textContent = soma ? 'Adding SomaFM station…' : 'Adding station…';
        try {
            const resp = await fetch('/api/stations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name || '', stream_url: streamUrl, custom_image_url: customImageUrl || '' }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to save station');
            await fetchStations();
            resetStationForm();
            showToast(`Added station: ${data.station?.title || name || 'Station'}`, 'success');
        } catch (e) {
            if (elements.stationFormStatus) elements.stationFormStatus.textContent = e.message || 'Failed to save station';
            showToast(e.message || 'Failed to save station', 'error');
        } finally {
            stationSaveInFlight = false;
            updateStationActionButtons();
        }
    }
    async function saveManagedStationChanges() {
        const station = selectedManagedStation();
        const streamUrl = (elements.stationExistingUrl?.value || '').trim();
        const customImageUrl = (elements.stationExistingImageUrl?.value || '').trim();
        if (!station) {
            showToast('Please select a station to edit', 'error');
            return;
        }
        if (!streamUrl) {
            showToast('Please enter a station URL', 'error');
            return;
        }
        const nextName = isSomaFmUrl(streamUrl) ? '' : (station.title || '');
        if (elements.stationUpdateBtn) elements.stationUpdateBtn.disabled = true;
        if (elements.stationDeleteBtn) elements.stationDeleteBtn.disabled = true;
        clearStationFormStatus();
        try {
            const resp = await fetch(`/api/stations/${encodeURIComponent(station.id)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: nextName, stream_url: streamUrl, custom_image_url: customImageUrl || '' }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to update station');
            await fetchStations();
            showToast(`Updated station: ${data.station?.title || station.title}`, 'success');
        } catch (e) {
            showToast(e.message || 'Failed to update station', 'error');
        } finally {
            updateStationActionButtons();
        }
    }
    
    async function deleteSelectedStation() {
        const stationId = elements.stationDeleteSelect.value;
        const station = state.stations.find(item => item.id === stationId);
        if (!stationId || !station) {
            showToast('Please select a station to delete', 'error');
            return;
        }
        if (!confirm(`Remove "${station.title}" from My Stations?`)) return;
        try {
            const resp = await fetch(`/api/stations/${encodeURIComponent(stationId)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to delete station');
            await fetchStations();
            showToast(`Removed from My Stations: ${station.title}`, 'success');
        } catch (e) {
            showToast(e.message || 'Failed to delete station', 'error');
        }
    }

    window.FXRouteRadio = { init };
}());
