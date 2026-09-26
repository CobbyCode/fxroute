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
const modalJs = fs.readFileSync(path.join(root, 'static', 'modal.js'), 'utf8');
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
check('shared modal manager is exposed', /FXRouteModal\s*=/.test(modalJs));
check('modal manager uses inert background state', /\.inert\s*=/.test(modalJs));
check('app uses the shared modal manager', /FXRouteModal/.test(appJs));
check('settings uses the shared modal manager', /settingsPanel[\s\S]*?FXRouteModal/.test(appJs));
check('radio management uses the shared modal manager', /radioManagePanel[\s\S]*?FXRouteModal/.test(radioJs));
check('modal manager restores focus on close', /opener[\s\S]*?\.focus\(\)/.test(modalJs));

// The visible FXRoute brand remains the settings trigger, while its function
// has one consistent accessible name and tooltip label.
check('settings trigger accessible name is Settings', /id="open-settings"[\s\S]*?aria-label="Settings"/.test(html));
check('visible brand is excluded from the trigger name', /class="brand-text-block" aria-hidden="true"/.test(html));
check('settings trigger has the Settings tooltip label', /id="open-settings"[\s\S]*?data-tooltip="Settings"/.test(html));
check('settings trigger has no old technical-settings label', !/Open technical settings/.test(html));
// Header hints use the app-wide tooltip layer (static/tooltip.js): one fixed
// bubble on <body>, opened on hover and on keyboard focus.
const tooltipJs = fs.readFileSync(path.join(root, 'static/tooltip.js'), 'utf8');
check('tooltip layer is loaded by the page', /<script src="\/static\/tooltip\.js\?v=[^"]+"><\/script>/.test(html));
check('tooltip layer opens on keyboard focus', tooltipJs.includes("addEventListener('focusin'") && tooltipJs.includes("matches(':focus-visible')"));
check('tooltip bubble does not participate in layout flow', /\.app-tooltip\s*\{[^}]*position:\s*fixed/.test(css));

// The power button reuses the same custom tooltip style as the brand trigger
// instead of the native browser title tooltip.
check('power button uses the custom tooltip attribute', /id="power-menu-toggle"[\s\S]*?data-tooltip="System power"/.test(html));
check('power button no longer uses the native title tooltip', !/id="power-menu-toggle"[^>]*title="/.test(html));
check('power button keeps its accessible name', /id="power-menu-toggle"[\s\S]*?aria-label="System power"/.test(html));
check('power tooltip is anchored to the button edge', /id="power-menu-toggle"[^>]*data-tooltip-anchor="end"/.test(html));
check('power tooltip stays hidden while the menu is open', /aria-expanded'\) === 'true'\) return ''/.test(tooltipJs));

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
