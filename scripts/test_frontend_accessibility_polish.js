#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression contracts for the focused UI/accessibility polish pass.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const radioJs = fs.readFileSync(path.join(root, 'static', 'radio.js'), 'utf8');

let passed = 0;
function check(name, condition) {
    assert.ok(condition, name);
    passed += 1;
}

// Radio cards must keep their existing div-based layout while supporting the
// keyboard activation model promised by role="button".
check('radio cards bind a keyboard activation handler', /station-card[\s\S]*?addEventListener\('keydown'/.test(radioJs));
check('radio cards handle Enter and Space', /event\.key !== 'Enter' && event\.key !== ' '/.test(radioJs));
check('radio card keyboard activation prevents page scrolling', /event\.preventDefault\(\)/.test(radioJs));

// Mobile browser zoom must remain available.
check('viewport does not disable user zoom', !/user-scalable\s*=\s*["']no["']/i.test(html));
check('viewport does not cap zoom at one', !/maximum-scale\s*=\s*["']1(?:\.0)?["']/i.test(html));

// All dialog ownership and background inerting should be centralized.
check('shared modal manager is exposed', /window\.FXRouteModal\s*=/.test(appJs));
check('modal manager uses inert background state', /\.inert\s*=/.test(appJs));
check('settings uses the shared modal manager', /settingsPanel[\s\S]*?FXRouteModal/.test(appJs));
check('radio management uses the shared modal manager', /radioManagePanel[\s\S]*?FXRouteModal/.test(radioJs));
check('modal manager restores focus on close', /opener[\s\S]*?\.focus\(\)/.test(appJs));

// The visible FXRoute brand remains the settings trigger, while its function
// has one consistent accessible name and tooltip label.
check('settings trigger accessible name is Settings', /id="open-settings"[\s\S]*?aria-label="Settings"/.test(html));
check('visible brand is excluded from the trigger name', /class="brand-text-block" aria-hidden="true"/.test(html));
check('settings trigger has the Settings tooltip label', /id="open-settings"[\s\S]*?data-tooltip="Settings"/.test(html));
check('settings trigger has no old technical-settings label', !/Open technical settings/.test(html));
check('settings tooltip is rendered by the existing trigger pattern', /\.brand-lockup-button\[data-tooltip\]::after/.test(css));
check('settings tooltip appears on keyboard focus', /\.brand-lockup-button:focus-visible::after/.test(css));
check('settings tooltip does not participate in layout flow', /\.brand-lockup-button\[data-tooltip\]::after[\s\S]*?position:\s*absolute/.test(css));

// Measurement graph controls get an explicit two-row mobile layout.
check('mobile measurement toolbar uses a grid', /@media \(max-width: 599px\)[\s\S]*?\.measurement-graph-header-actions[\s\S]*?display:\s*grid/.test(css));
check('mobile measurement view controls occupy row one', /\.measurement-view-toggle[\s\S]*?grid-row:\s*1/.test(css));
check('mobile measurement selects occupy row two', /#measurement-assist-mode[\s\S]*?grid-row:\s*2/.test(css));
check('mobile measurement selects have usable width', /#measurement-assist-mode[\s\S]*?min-width:\s*0/.test(css));

// Track context must remain visible on phones instead of being hidden with the
// brand subtitle and other compact-only content.
check('mobile rules do not hide track metadata with the brand subtitle', !/@media \(max-width: 600px\)[\s\S]*?\.brand-subtitle,\s*\.track-artist,\s*\.subtitle-full[\s\S]*?display:\s*none/.test(css));
check('mobile rules keep track metadata visible and compact', /@media \(max-width: 600px\)[\s\S]*?\.track-artist\s*\{[\s\S]*?display:\s*block/.test(css));

// The clickable cover opens a dialog and therefore needs keyboard semantics.
check('playback cover is a keyboard-accessible dialog trigger', /id="playback-cover"[\s\S]*?role="button"[\s\S]*?aria-label="Open now-playing details"/.test(html));
check('playback cover dialog has a focus target', /id="cover-detail-card"[^>]*tabindex="-1"/.test(html));

console.log(`PASS  scripts/test_frontend_accessibility_polish.js (${passed} checks)`);
