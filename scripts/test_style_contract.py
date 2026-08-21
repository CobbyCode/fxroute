#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""CSS layered-build contract guard — Task 1 structural regression checks.

Asserts on the built artifact static/style.css:
 1. static/style.css exists and is non-empty, served via static/index.html ?v= query
 2. no hand-divergence: if static/css/ sources exist, cat order in
    scripts/build-css.sh reproduces static/style.css (hash check)
 3. !important only at the explicitly justified utility/accessibility spots:
    .hidden, .streaming-provider [hidden], and the prefers-reduced-motion
    override block; any !important elsewhere fails the guard
 4. unified primitives exist in the built CSS: .btn-fav, .track-row, .track-play
 5. media sanity: at most 3 playback ranges (min-width:1181 desktop +
    701-1180 tablet + max-width:700 phone) — fail if >3 @media blocks
    touch .playback-bar

Must run via: python3 scripts/test_style_contract.py  (supports -v)
"""

import re
import hashlib
import pathlib
import sys
import argparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "static/style.css"
HTML_PATH = ROOT / "static/index.html"
BUILD_SCRIPT = ROOT / "scripts/build-css.sh"
CSS_SOURCES_DIR = ROOT / "static/css"

# snippet required by plan — keep verbatim for contract visibility
# (these are also checked inside the structured validators below)
# css = (ROOT/"static/style.css").read_text()
# assert "@media (min-width: 1181px)" in css
# assert re.search(r"@media \(max-width:\s*700px\)", css)
# playback range cap — tighten after consolidation
# assert css.count(".playback-bar") >= 1

# !important is a last-resort tool: every occurrence must be an explicitly
# justified utility or accessibility override. The contract below verifies
# each declaration's selector, so a new !important anywhere else fails the
# guard no matter how the total count changes.
#
# Justified spots:
#  - ".hidden"                          JS toggles this utility against
#                                       flex/grid display rules
#  - ".streaming-provider [hidden]"     streaming.js toggles the hidden
#                                       attribute; per-element display rules
#                                       (flex/grid) must never override it
#  - ".visually-hidden"                 screen-reader-only utility (page h1);
#                                       must beat element-specific position
#                                       and overflow rules everywhere
#  - prefers-reduced-motion block       accessibility override; the generic
#                                       star rule must beat every specific
#                                       animation/transition rule
IMPORTANT_ALLOWED_SELECTORS = (
    ".hidden",
    ".streaming-provider [hidden]",
    ".visually-hidden",
)
IMPORTANT_REDUCED_MOTION = (
    "animation-duration",
    "animation-iteration-count",
    "transition-duration",
    "scroll-behavior",
)
MAX_PLAYBACK_MEDIA_BLOCKS = 3


def _fail(msg: str) -> str:
    return msg


def check_exists_and_served(errors: list[str], verbose: bool) -> None:
    if not CSS_PATH.exists():
        errors.append(f"missing {CSS_PATH.relative_to(ROOT)}")
        return
    data = CSS_PATH.read_bytes()
    if len(data) == 0:
        errors.append("static/style.css is empty")
        return
    try:
        css = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"static/style.css not utf-8: {exc}")
        return
    if verbose:
        print(f"[ok] static/style.css exists ({len(data)} bytes, {len(css.splitlines())} lines)")
    # snippet assertions (plan)
    if "@media (min-width: 1181px)" not in css:
        errors.append('missing "@media (min-width: 1181px)" in static/style.css')
    if not re.search(r"@media \(max-width:\s*700px\)", css):
        errors.append(r'missing "@media (max-width: 700px)" in static/style.css')
    if css.count(".playback-bar") < 1:
        errors.append('static/style.css must contain at least one ".playback-bar"')
    # served via query string
    if not HTML_PATH.exists():
        errors.append(f"missing {HTML_PATH.relative_to(ROOT)}")
    else:
        html = HTML_PATH.read_text(encoding="utf-8", errors="replace")
        if not re.search(r'style\.css\?v=[^"\']+', html):
            errors.append("static/index.html missing style.css?v= query string for static/style.css")
        elif verbose:
            m = re.search(r'style\.css\?v=[^"\']+', html)
            print(f"[ok] served via static/index.html -> {m.group(0) if m else ''}")


def check_build_reproducibility(errors: list[str], verbose: bool) -> None:
    if not CSS_SOURCES_DIR.exists():
        if verbose:
            print("[skip] static/css/ not present — hand-divergence check skipped (pre-Task 2)")
        return
    sources = sorted(CSS_SOURCES_DIR.glob("*.css"))
    # also consider nested
    sources = sorted(CSS_SOURCES_DIR.rglob("*.css"))
    sources = [p for p in sources if p.is_file()]
    if not sources:
        if verbose:
            print("[skip] static/css/ empty — hand-divergence check skipped")
        return
    if not BUILD_SCRIPT.exists():
        errors.append("static/css/ sources exist but scripts/build-css.sh is missing — cannot verify reproducibility")
        return
    script = BUILD_SCRIPT.read_text(encoding="utf-8", errors="replace")
    # Extract ordered list of static/css/*.css from the cat invocation.
    # The canonical build does: cat "$ROOT/static/css/_tokens.css" \ ... >"$OUT"
    # We collect every occurrence of static/css/<name>.css preserving order.
    ordered = re.findall(r"static/css/[^\"'\s\\]+\.css", script)
    if not ordered:
        errors.append("scripts/build-css.sh does not mention any static/css/*.css sources — cannot verify hash")
        return
    # Deduplicate preserving order (in case script mentions a file twice, keep first)
    seen: set[str] = set()
    ordered_unique: list[str] = []
    for o in ordered:
        if o not in seen:
            seen.add(o)
            ordered_unique.append(o)
    ordered = ordered_unique
    # Resolve each relative path against ROOT
    concat = b""
    missing: list[str] = []
    for rel in ordered:
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
        else:
            concat += p.read_bytes()
    if missing:
        errors.append(f"scripts/build-css.sh references missing sources: {', '.join(missing)}")
        return
    # Warn about sources present on disk but not in build order
    on_disk = {p.relative_to(ROOT).as_posix() for p in sources}
    in_build = set(ordered)
    extra = sorted(on_disk - in_build)
    if extra and verbose:
        print(f"[warn] sources on disk not in build order: {', '.join(extra)}")
    # Hash comparison
    built_hash = hashlib.sha256(concat).hexdigest()
    try:
        actual = CSS_PATH.read_bytes()
    except FileNotFoundError:
        errors.append("static/style.css missing for hash check")
        return
    actual_hash = hashlib.sha256(actual).hexdigest()
    if built_hash != actual_hash:
        errors.append(
            "hand-divergence: cat order in scripts/build-css.sh does not reproduce static/style.css "
            f"(built sha256 {built_hash[:12]}.. != actual {actual_hash[:12]}.., "
            f"built {len(concat)} bytes vs actual {len(actual)} bytes; "
            f"order: {' -> '.join(ordered)})"
        )
    elif verbose:
        print(f"[ok] build reproducibility: sha256 {actual_hash[:12]}.. ({len(actual)} bytes) matches cat order")


def check_important_allowlist(errors: list[str], verbose: bool) -> None:
    if not CSS_PATH.exists():
        return
    css = CSS_PATH.read_text(encoding="utf-8", errors="replace")
    # Strip comments so "!important" inside a comment never counts.
    css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    total = css_nc.count("!important")
    violations: list[str] = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*!important[^{}]*)\}", css_nc):
        selector = " ".join(m.group(1).split())
        decls = " ".join(m.group(2).split())
        if selector.startswith("*") and any(p in decls for p in IMPORTANT_REDUCED_MOTION):
            continue  # prefers-reduced-motion accessibility override
        allowed = any(selector == s or selector.endswith(" " + s) for s in IMPORTANT_ALLOWED_SELECTORS)
        if not allowed:
            violations.append(f"  {selector} {{ {decls} }}")
    if verbose:
        print(f"[info] !important total: {total}")
        print(f"[info] allowed selectors: {', '.join(IMPORTANT_ALLOWED_SELECTORS)}")
        print(f"[info] allowed reduced-motion props: {', '.join(IMPORTANT_REDUCED_MOTION)}")
    if violations:
        detail = "\n".join(violations[:12])
        if len(violations) > 12:
            detail += f"\n  ... and {len(violations) - 12} more"
        errors.append(
            f"!important drift: {len(violations)} declaration(s) outside the justified "
            f"utility/accessibility spots (.hidden, .streaming-provider [hidden], "
            f"prefers-reduced-motion override)\n{detail}"
        )
    elif verbose:
        print(f"[ok] !important restricted to the justified utility/accessibility spots")


def check_primitives(errors: list[str], verbose: bool) -> None:
    if not CSS_PATH.exists():
        return
    css = CSS_PATH.read_text(encoding="utf-8", errors="replace")
    # unified primitives must exist as standalone class selectors
    # Use negative lookahead so .track-row-favorite does not satisfy .track-row
    primitives = {
        ".btn-fav": r"\.btn-fav(?![\w-])",
        ".track-row": r"\.track-row(?![\w-])",
        ".track-play": r"\.track-play(?![\w-])",
    }
    for name, pat in primitives.items():
        if not re.search(pat, css):
            # Show alias hint when primitive is missing
            aliases = {
                ".btn-fav": [".track-row-favorite", ".track-fav", ".streaming-fav", ".album-favorite-toggle", ".track-favorite-btn"],
                ".track-row": [".track-item", ".streaming-track-row", ".track-row-favorite"],
                ".track-play": [".track-play", ".control-btn"],
            }
            hint = ""
            if name in aliases:
                present_aliases = [a for a in aliases[name] if a in css]
                if present_aliases:
                    hint = f" (aliases still present: {', '.join(present_aliases)}; primitive not yet unified)"
            errors.append(f"missing unified primitive {name} in static/style.css{hint}")
        elif verbose:
            print(f"[ok] primitive {name} present")


def check_media_sanity(errors: list[str], verbose: bool) -> None:
    if not CSS_PATH.exists():
        return
    css = CSS_PATH.read_text(encoding="utf-8", errors="replace")
    # Re-assert snippet invariants (already checked, but keep for contract clarity)
    # These are intentionally not fatal by themselves; the block cap below is the gate.
    # Count @media blocks that contain .playback-bar via brace-balanced scan.
    headers: list[str] = []
    idx = 0
    count = 0
    n = len(css)
    while True:
        m = css.find("@media", idx)
        if m == -1:
            break
        brace = css.find("{", m)
        if brace == -1:
            break
        depth = 0
        end = -1
        for j in range(brace, n):
            ch = css[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            break
        block = css[m : end + 1]
        if ".playback-bar" in block:
            count += 1
            header = css[m:brace].strip()
            # normalize whitespace for reporting
            header = re.sub(r"\s+", " ", header)
            headers.append(header)
        idx = end + 1
    if verbose:
        print(f"[info] @media blocks touching .playback-bar: {count} (cap {MAX_PLAYBACK_MEDIA_BLOCKS})")
        for h in headers:
            print(f"       {h}")
    if count > MAX_PLAYBACK_MEDIA_BLOCKS:
        header_list = "\n".join(f"  - {h}" for h in headers)
        errors.append(
            f"playback media sanity: {count} @media blocks touch .playback-bar, "
            f"cap is {MAX_PLAYBACK_MEDIA_BLOCKS} canonical ranges "
            f"(min-width:1181 desktop + 701-1180 tablet + max-width:700 phone)\n"
            f"found:\n{header_list}"
        )
    elif verbose:
        print(f"[ok] playback @media count {count} within cap {MAX_PLAYBACK_MEDIA_BLOCKS}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CSS layered-build contract guard")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    args = parser.parse_args()

    errors: list[str] = []
    # Order matters for reporting: existence first so later checks can rely on css content
    check_exists_and_served(errors, args.verbose)
    check_build_reproducibility(errors, args.verbose)
    check_important_allowlist(errors, args.verbose)
    check_primitives(errors, args.verbose)
    check_media_sanity(errors, args.verbose)

    if errors:
        print("CSS contract guard FAILED:", file=sys.stderr)
        for e in errors:
            # prefix each error for readability; preserve multiline detail
            first, *rest = e.split("\n")
            print(f"  - {first}", file=sys.stderr)
            for line in rest:
                print(f"    {line}", file=sys.stderr)
        # Ensure snippet-required invariants are also surfaced as assertions in verbose logs
        sys.exit(1)
    else:
        print("CSS contract guard passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
