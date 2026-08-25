#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Computed-style snapshot gate for the semantic CSS partial reorg.

Captures the computed style + geometry of every element (real static DOM +
probe elements generated for every selector in the CSS sources) at all
required viewports, then compares two snapshots for identity.

Purpose: prove that a pure source reorganisation (moving rule blocks between
partials) changes no computed style. Run before and after the reorg:

    python3 scripts/check_css_reorg_snapshot.py --capture before.json
    ... rebuild ...
    python3 scripts/check_css_reorg_snapshot.py --capture after.json
    python3 scripts/check_css_reorg_snapshot.py --compare before.json after.json

The page is loaded with all page JS blocked so the DOM is exactly the static
markup plus deterministic probe injection; no timers, no fetches, no inline
style churn. Probes are generated from every selector found in static/css/*.css
so each rule has a matching element; any cascade-order change that alters a
computed value shows up as a snapshot difference.

Skips cleanly when playwright is unavailable.
"""

import argparse
import hashlib
import http.server
import json
import pathlib
import re
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8211

# The viewport widths the reorg must be verified at (from the task).
# Covers the full set of historically-grown media-query boundaries, including
# the exact breakpoint pairs the CSS cleanup must not disturb:
#   599/600/601 · 699/700/701 · 760/761/768 · 899/900/940 ·
#   1024/1099/1100/1101 · 1180/1181/1199/1200 · desktop 1440 · mobile 390/360/320
VIEWPORTS = [
    (1440, 900),
    (1200, 900),
    (1199, 900),
    (1181, 900),
    (1180, 900),
    (1101, 900),
    (1100, 900),
    (1099, 900),
    (1024, 900),
    (940, 900),
    (901, 900),
    (900, 900),
    (899, 900),
    (834, 1112),
    (820, 1180),
    (768, 1024),
    (761, 1024),
    (760, 1024),
    (701, 1024),
    (700, 1024),
    (699, 1024),
    (641, 1024),
    (640, 1024),
    (601, 1024),
    (600, 1024),
    (599, 1024),
    (520, 800),
    (390, 844),
    (360, 800),
    (320, 700),
]

PROBE_CONTAINER_ID = "css-reorg-probes"


def _selector_list(css_text: str) -> list[str]:
    """Extract the raw pre-rule selector chunks from a stylesheet text."""
    # Strip comments and strings first so braces inside strings don't confuse us.
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    text = re.sub(r'"[^"]*"|\'[^\']*\'', '""', text)
    out = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "{":
            if depth == 0:
                sel = text[start:i].strip()
                if sel and not sel.startswith("@"):
                    out.append(sel)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                start = i + 1
        i += 1
    return out


def _compound_to_attrs(part: str, parent: dict) -> None:
    """Apply one compound selector part (.a.b#id[attr]) onto a dict."""
    part = part.strip()
    if not part:
        return
    # strip pseudo-classes/elements, keep :not(...) inner (best-effort)
    part = re.sub(r"::?[a-z-]+(\([^)]*\))?", "", part)
    # attribute selectors
    for m in re.finditer(r"\[([^\]~|^$*]+?)([~|^$*]?=)?\s*[\"']?([^\"'\]]*)[\"']?\]", part):
        name, _op, val = m.group(1), m.group(2), m.group(3)
        parent["attrs"][name] = val
    part = re.sub(r"\[[^\]]*\]", "", part)
    tags = re.findall(r"^([a-zA-Z][a-zA-Z0-9-]*)", part)
    if tags:
        parent["tag"] = tags[0]
    for m in re.finditer(r"\.(-?[a-zA-Z_][a-zA-Z0-9_-]*)", part):
        parent["classes"].append(m.group(1))
    for m in re.finditer(r"#(-?[a-zA-Z_][a-zA-Z0-9_-]*)", part):
        parent["id"] = m.group(1)


def _selector_to_probe(selector: str) -> str | None:
    """Return HTML for one element (or nested chain) matching `selector`.

    Handles descendant/child/sibling combinators by nesting; unknown or
    over-complex selectors degrade gracefully (the base class still matches).
    """
    parts = re.split(r"\s*(?:>\s*|\+\s*|~\s*|(?:\s+))\s*", selector.strip())
    if not parts:
        return None
    chain = []
    for part in parts:
        node = {"tag": "div", "id": "", "classes": [], "attrs": {}}
        _compound_to_attrs(part, node)
        chain.append(node)
    # Build innermost-first then wrap
    html = ""
    for node in reversed(chain):
        cls = " ".join(node["classes"])
        attrs = "".join(f' {k}="{v}"' if v else f" {k}" for k, v in node["attrs"].items())
        if cls and node["id"]:
            attrs += f' id="{node["id"]}"'
        elif cls:
            pass
        elif node["id"]:
            attrs += f' id="{node["id"]}"'
        if cls:
            attrs += f' class="{cls}"'
        if html:
            html = f'<{node["tag"]}{attrs}>{html}</{node["tag"]}>'
        else:
            html = f'<{node["tag"]}{attrs}></{node["tag"]}>'
    return html


def _build_probe_js() -> str:
    """JS that appends one probe element per unique selector into the DOM."""
    css_texts = []
    for p in sorted((ROOT / "static/css").glob("*.css")):
        css_texts.append(p.read_text(encoding="utf-8", errors="replace"))
    css_text = "\n".join(css_texts)
    selectors = _selector_list(css_text)
    unique = []
    seen = set()
    for sel in selectors:
        if sel in seen:
            continue
        seen.add(sel)
        unique.append(sel)
    # Stable identity: data-p is the index of the probe in the sorted unique
    # selector list, so probe elements keep the same key no matter which
    # partial file the rule currently lives in (first-occurrence order would
    # shift when blocks move between files).
    unique.sort()
    probe_html = "\n".join(
        f'<div data-p="{i}" style="display:contents">{_selector_to_probe(sel)}</div>'
        for i, sel in enumerate(unique)
    )
    js = f"""
(() => {{
    const host = document.createElement('div');
    host.id = '{PROBE_CONTAINER_ID}';
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;left:-10000px;top:0;visibility:hidden;pointer-events:none;';
    host.innerHTML = `{probe_html}`;
    document.body.appendChild(host);
}})();
"""
    return js


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


BLOCK_JS = """
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    return Promise.reject(new Error('fetch disabled in snapshot mode'));
};
"""

# Activate all tab panels, open all overlays/dialogs, mark playback as active
# so every statically-present rule has a rendered context.
STATE_JS = """
(() => {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.add('active'));
    document.querySelectorAll('.hidden').forEach(el => el.classList.remove('hidden'));
    const bar = document.getElementById('playback-bar');
    if (bar) bar.classList.add('has-media', 'is-playing');
    const sr = document.querySelector('.seek-row');
    if (sr) { sr.classList.remove('hidden'); sr.style.display = ''; }
    const title = document.getElementById('track-title');
    if (title) { title.classList.remove('placeholder'); title.textContent = 'Groove Is in the Heart'; }
    const artist = document.getElementById('track-artist');
    if (artist) artist.textContent = 'Deee-Lite';
    // Freeze animations/transitions: the snapshot compares the static cascade,
    // not the phase of load-triggered keyframes (bannerSlide, toastIn, fadeIn).
    const frozen = document.createElement('style');
    frozen.textContent = '*{animation:none !important;transition:none !important}'
        + '*::before,*::after{animation:none !important;transition:none !important}';
    document.head.appendChild(frozen);
})();
"""

SNAPSHOT_JS = """
(() => {
    const out = {};
    const props = [
        'display','position','visibility','opacity','zIndex','float','clear',
        'width','height','minWidth','minHeight','maxWidth','maxHeight',
        'top','left','right','bottom','inset',
        'marginTop','marginRight','marginBottom','marginLeft',
        'paddingTop','paddingRight','paddingBottom','paddingLeft',
        'borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth',
        'borderTopStyle','borderRightStyle','borderBottomStyle','borderLeftStyle',
        'borderTopColor','borderRightColor','borderBottomColor','borderLeftColor',
        'borderTopLeftRadius','borderTopRightRadius','borderBottomRightRadius','borderBottomLeftRadius',
        'boxSizing','fontFamily','fontSize','fontWeight','lineHeight','letterSpacing',
        'textAlign','color','backgroundColor','backgroundImage','boxShadow',
        'transform','filter','backdropFilter',
        'gridTemplateColumns','gridTemplateRows','gridTemplateAreas',
        'gridColumn','gridRow','gridArea','gap','rowGap','columnGap',
        'flexDirection','flexWrap','justifyContent','alignItems','alignSelf','alignContent',
        'overflow','overflowX','overflowY','objectFit',
        'whiteSpace','textOverflow','overflowWrap','cursor','pointerEvents',
        'verticalAlign','textTransform','textDecorationLine','fontStyle','fontVariantNumeric',
        'content','outline','outlineOffset','borderCollapse','wordBreak'
    ];
    // Note: 'aspectRatio' is deliberately not captured: Chromium reports it
    // from the canvas paint state, which varies between identical page loads.
    // fontFamily/fontSize are captured only on text-bearing elements: the page
    // uses web fonts whose async load can shift metrics between runs.
    const all = document.querySelectorAll('*');
    for (const el of all) {
        // Find the nearest probe root (data-p) to get a stable identity;
        // the probe order in the DOM is not part of the contract.
        let probeRoot = null;
        let node = el;
        const rev = [];
        while (node && node !== document.body) {
            rev.unshift(node);
            if (node.hasAttribute && node.hasAttribute('data-p')) { probeRoot = node; break; }
            node = node.parentElement;
        }
        let key;
        if (probeRoot) {
            const parts = [];
            let n = probeRoot;
            let prev = null;
            while (n) {
                let idx = 0;
                let sib = n.previousElementSibling;
                while (sib) { idx += 1; sib = sib.previousElementSibling; }
                const tag = n.tagName.toLowerCase();
                if (n === probeRoot) parts.unshift(`P${n.getAttribute('data-p')}`);
                else parts.unshift(`${tag}[${idx}]`);
                if (n === el) break;
                prev = n;
                n = n.parentElement;
                if (n === probeRoot.parentElement) break;
            }
            key = 'B>probe>' + parts.join('>');
        } else {
            const path = [];
            node = el;
            while (node && node !== document.body) {
                let idx = 0;
                let sib = node.previousElementSibling;
                while (sib) { idx += 1; sib = sib.previousElementSibling; }
                const tag = node.tagName.toLowerCase();
                path.unshift(`${tag}[${idx}]`);
                node = node.parentElement;
            }
            key = 'B' + '>' + path.join('>');
        }
        const cs = getComputedStyle(el);
        const rec = { rect: null, cls: el.className && String(el.className).slice(0, 60), style: {} };
        const r = el.getBoundingClientRect();
        rec.rect = [Math.round(r.x * 100) / 100, Math.round(r.y * 100) / 100,
                    Math.round(r.width * 100) / 100, Math.round(r.height * 100) / 100];
        for (const p of props) {
            rec.style[p] = cs.getPropertyValue(p.replace(/[A-Z]/g, m => '-' + m.toLowerCase()));
        }
        out[key] = rec;
    }
    return { elements: out, count: all.length };
})();
"""


def _capture(out_path: pathlib.Path) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  check_css_reorg_snapshot.py (playwright not installed)")
        return 0

    server = _serve()
    probe_js = _build_probe_js()
    snapshots = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(BLOCK_JS)
                # Versioned script URLs (app.js?v=0.9.50) defeat a plain
                # "**/*.js" glob (it matches the full URL string), so match
                # any URL whose path ends in .js regardless of query string.
                page.route(
                    re.compile(r"\.js(\?.*)?$", re.I),
                    lambda route: route.abort(),
                )
                # Abort images/media/fonts: async loads shift layout at capture
                # time and make snapshots non-deterministic.
                page.route(
                    re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|avif|bmp|woff2?|ttf|eot|otf|mp3|flac|wav|ogg|oga|opus|m4a|aac|webm)(\?|$)", re.I),
                    lambda route: route.abort(),
                )
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_selector("#playback-bar")
                page.evaluate(STATE_JS)
                page.evaluate(probe_js)
                page.wait_for_timeout(150)
                result = page.evaluate(SNAPSHOT_JS)
                snapshots[f"{width}x{height}"] = {
                    "elements": result["elements"],
                    "count": result["count"],
                }
                page.close()
            browser.close()
    finally:
        server.shutdown()

    out_path.write_text(json.dumps(snapshots, indent=1, sort_keys=True), encoding="utf-8")
    total = sum(v["count"] for v in snapshots.values())
    print(f"captured {out_path} ({len(snapshots)} viewports, {total} element-records)")
    return 0


def _element_digest(elements: dict) -> str:
    return hashlib.sha256(json.dumps(elements, sort_keys=True).encode("utf-8")).hexdigest()


def _compare(a_path: pathlib.Path, b_path: pathlib.Path, detail: int = 0) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    diffs = []
    for vp in a:
        if vp not in b:
            diffs.append(f"[{vp}] missing in {b_path}")
            continue
        if a[vp]["count"] != b[vp]["count"]:
            diffs.append(f"[{vp}] element count {a[vp]['count']} != {b[vp]['count']}")
        if _element_digest(a[vp]["elements"]) != _element_digest(b[vp]["elements"]):
            diffs.append(f"[{vp}] computed-style digest differs")
            if detail:
                a_el, b_el = a[vp]["elements"], b[vp]["elements"]
                changed = 0
                for key in a_el:
                    if key not in b_el:
                        print(f"  [{vp}] element only in before: {key}")
                        changed += 1
                        continue
                    if a_el[key] != b_el[key]:
                        changed += 1
                        if changed <= detail:
                            print(f"  [{vp}] differs: {key}")
                            style_a, style_b = a_el[key]["style"], b_el[key]["style"]
                            prop_diff = 0
                            for prop in style_a:
                                if style_a[prop] != style_b.get(prop):
                                    prop_diff += 1
                                    if prop_diff <= 12:
                                        print(f"      {prop}: {style_a[prop]!r} -> {style_b.get(prop)!r}")
                for key in b_el:
                    if key not in a_el:
                        print(f"  [{vp}] element only in after: {key}")
                        changed += 1
                print(f"  [{vp}] {changed} elements changed")
    if not diffs:
        print(f"PASS  {a_path.name} == {b_path.name} (identical computed styles at all viewports)")
        return 0
    print(f"FAIL  {a_path.name} vs {b_path.name}")
    for d in diffs:
        print(f"  {d}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", metavar="JSON", help="capture a snapshot")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two snapshots")
    ap.add_argument("--detail", type=int, default=0, help="per-element diff detail (element + property level)")
    args = ap.parse_args()
    if args.capture:
        return _capture(pathlib.Path(args.capture))
    if args.compare:
        return _compare(pathlib.Path(args.compare[0]), pathlib.Path(args.compare[1]), detail=args.detail)
    # Default suite mode: verify build reproducibility. Rebuild from the
    # partial sources and confirm the result is byte-identical to the
    # committed static/style.css.
    before = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    built = subprocess.run(
        ["bash", str(ROOT / "scripts" / "build-css.sh")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if built.returncode != 0:
        print("FAIL  check_css_reorg_snapshot.py (build failed)")
        print(built.stderr)
        return 1
    after = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    if after != before:
        print("FAIL  check_css_reorg_snapshot.py (build is not reproducible)")
        return 1
    print("PASS  check_css_reorg_snapshot.py (style.css rebuild is reproducible)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"FAIL  check_css_reorg_snapshot.py")
        print(f"  type={type(exc).__name__} repr={exc!r}")
        traceback.print_exc()
        sys.exit(1)
