#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The local root certificate link must stay same-origin: the settings page
// may be served over HTTP or HTTPS, and an absolute http:// rewrite turns
// the link into mixed content on HTTPS pages, where browsers silently block
// the insecure download (click appears to do nothing, no request is sent).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

const urlFn = appJs.match(/function settingsCertificateUrl\(\) \{[\s\S]*?\n\}/);
assert.ok(urlFn, 'app.js must own settingsCertificateUrl()');
const urlFnCode = urlFn[0].replace(/\/\/[^\n]*/g, '');
assert.doesNotMatch(urlFnCode, /http:\/\//,
    'settingsCertificateUrl() must not rewrite the link to absolute http://');
assert.match(urlFnCode, /return '\/api\/certificate\/local-root'/,
    'settingsCertificateUrl() must return the same-origin relative URL');

assert.match(indexHtml, /id="settings-certificate-link"[^>]*href="\/api\/certificate\/local-root"/,
    'the settings anchor must ship a same-origin relative href');
assert.match(indexHtml, /\/static\/app\.js\?v=\d+\.\d+\.\d+/,
    'the served shell must reference a versioned app.js');

console.log('PASS  scripts/test_certificate_download_link.js');
