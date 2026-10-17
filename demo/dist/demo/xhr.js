// Demo XMLHttpRequest: the library upload posts its file through XHR
// (static/library_ui.js uploadTrackFile), so the demo layer answers it like
// the fetch interceptor answers everything else — same routes, same simulated
// state. Anything that is not an API call falls back to the real XHR, so
// ordinary asset loads keep working.
//
// Loaded after routes.js: the API requests go through window.fetch, which by
// then is the demo interceptor.
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_XHR_LOADED) return;
    window.FXROUTE_DEMO_XHR_LOADED = true;

    const RealXHR = window.XMLHttpRequest;

    // Minimal event plumbing: the app registers listeners and reads the
    // upload progress of a big file body.
    function makeTarget() {
        return {
            _listeners: {},
            addEventListener(type, listener) {
                if (typeof listener !== 'function') return;
                (this._listeners[type] = this._listeners[type] || []).push(listener);
            },
            removeEventListener(type, listener) {
                const list = this._listeners[type];
                if (!list) return;
                const index = list.indexOf(listener);
                if (index >= 0) list.splice(index, 1);
            },
            _emit(type, event) {
                const handler = this[`on${type}`];
                if (typeof handler === 'function') handler.call(this, event);
                for (const listener of (this._listeners[type] || []).slice()) {
                    listener.call(this, event);
                }
            },
        };
    }

    const UNSENT = 0, OPENED = 1, HEADERS_RECEIVED = 2, LOADING = 3, DONE = 4;

    class DemoXMLHttpRequest {
        constructor() {
            this.readyState = UNSENT;
            this.status = 0;
            this.statusText = '';
            this.responseText = '';
            this.response = '';
            this.responseType = '';
            this.responseURL = '';
            this.timeout = 0;
            this.withCredentials = false;
            this.upload = makeTarget();
            this._target = makeTarget();
            this._method = 'GET';
            this._url = '';
            this._headers = {};
            this._responseHeaders = {};
            this._aborted = false;
            this._delegate = null;
        }

        open(method, url, async = true) {
            this._method = String(method || 'GET').toUpperCase();
            this._url = String(url || '');
            this._async = async !== false;
            this.readyState = OPENED;
            this._target._emit('readystatechange', { type: 'readystatechange', target: this });
        }

        setRequestHeader(name, value) {
            this._headers[String(name)] = String(value);
        }

        getResponseHeader(name) {
            const key = Object.keys(this._responseHeaders)
                .find(entry => entry.toLowerCase() === String(name).toLowerCase());
            return key ? this._responseHeaders[key] : null;
        }

        getAllResponseHeaders() {
            return Object.entries(this._responseHeaders)
                .map(([name, value]) => `${name}: ${value}`)
                .join('\r\n');
        }

        addEventListener(type, listener) { this._target.addEventListener(type, listener); }
        removeEventListener(type, listener) { this._target.removeEventListener(type, listener); }
        get onreadystatechange() { return this._target.onreadystatechange; }
        set onreadystatechange(handler) { this._target.onreadystatechange = handler; }
        get onload() { return this._target.onload; }
        set onload(handler) { this._target.onload = handler; }
        get onerror() { return this._target.onerror; }
        set onerror(handler) { this._target.onerror = handler; }
        get onabort() { return this._target.onabort; }
        set onabort(handler) { this._target.onabort = handler; }
        get onprogress() { return this._target.onprogress; }
        set onprogress(handler) { this._target.onprogress = handler; }

        send(body) {
            const path = this._url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
            // Non-API requests (artwork pools and the like) stay real.
            if (!path.startsWith('/api/')) {
                this._delegate = new RealXHR();
                const delegate = this._delegate;
                delegate.open(this._method, this._url, this._async);
                for (const [name, value] of Object.entries(this._headers)) delegate.setRequestHeader(name, value);
                delegate.responseType = this.responseType;
                delegate.onload = () => this._settleFromDelegate(delegate);
                delegate.onerror = (event) => this._target._emit('error', { type: 'error', target: this, ...event });
                delegate.send(body);
                return;
            }
            this._body = body;
            // Progress of the request body, like a real upload reports it.
            const size = Number(body && body.size || 0);
            const total = size || 1;
            this._emitUpload(0, total);
            setTimeout(() => this._emitUpload(Math.floor(total / 2), total), 15);
            setTimeout(() => this._emitUpload(total, total), 30);
            setTimeout(() => this._respond(), 60);
        }

        abort() {
            this._aborted = true;
            if (this._delegate) {
                this._delegate.abort();
                return;
            }
            this.readyState = DONE;
            this._target._emit('abort', { type: 'abort', target: this });
            this._target._emit('loadend', { type: 'loadend', target: this });
        }

        _emitUpload(loaded, total) {
            this.upload._emit('progress', {
                type: 'progress', target: this.upload, lengthComputable: true,
                loaded, total, loadedBytes: loaded, totalBytes: total,
            });
        }

        _settleFromDelegate(delegate) {
            this.status = delegate.status;
            this.statusText = delegate.statusText;
            this.responseText = delegate.responseText;
            this.response = delegate.response;
            this.readyState = DONE;
            this._target._emit('load', { type: 'load', target: this });
            this._target._emit('loadend', { type: 'loadend', target: this });
        }

        async _respond() {
            if (this._aborted) return;
            try {
                const response = await window.fetch(this._url, { method: this._method, body: this._body });
                const text = await response.text();
                if (this._aborted) return;
                this.status = response.status;
                this.statusText = response.status === 200 ? 'OK' : '';
                this.responseURL = this._url;
                this._responseHeaders = { 'Content-Type': (response.headers && response.headers.get('Content-Type')) || 'application/json' };
                this.readyState = HEADERS_RECEIVED;
                this._target._emit('readystatechange', { type: 'readystatechange', target: this });
                this.readyState = LOADING;
                this._target._emit('readystatechange', { type: 'readystatechange', target: this });
                this.responseText = text;
                this.response = text;
                this.readyState = DONE;
                this._target._emit('readystatechange', { type: 'readystatechange', target: this });
                this._target._emit('progress', {
                    type: 'progress', target: this, lengthComputable: true,
                    loaded: text.length, total: text.length,
                });
                this._target._emit('load', { type: 'load', target: this });
                this._target._emit('loadend', { type: 'loadend', target: this });
            } catch (error) {
                if (this._aborted) return;
                this.status = 0;
                this.readyState = DONE;
                this._target._emit('error', { type: 'error', target: this, error });
                this._target._emit('loadend', { type: 'loadend', target: this });
            }
        }
    }

    window.XMLHttpRequest = DemoXMLHttpRequest;
})();
