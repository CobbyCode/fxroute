#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Shared test runner for FXRoute.
#
# Discovers every test_*.py / test_*.js in scripts/ and runs each file
# directly (unittest-style files exit non-zero on failure, script-style
# files print their own pass line).  Tests that need native helper
# binaries, PipeWire graph state or the real audio hardware on .104 are
# skipped locally with an explicit reason; use --all to force-run them
# (they will fail fast when the host lacks the prerequisites).
#
# Browser/UI tests use Python Playwright and the Playwright-managed
# Chromium binary.  On a fresh dev host install them once:
#   pip install playwright
#   python3 -m playwright install chromium
# The runner checks for the browser up front and skips the browser tests
# with a setup hint when it is missing, instead of failing every test.
#
# Usage:
#   scripts/run_tests.sh            # run everything runnable locally
#   scripts/run_tests.sh --list     # print categorized test inventory
#   scripts/run_tests.sh --all      # also attempt .104-only tests
#   scripts/run_tests.sh --verbose  # show per-test output

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------------------
# Hermetic XDG sandbox
# ---------------------------------------------------------------------------
# No test may ever write the invoking user's real product configuration.
# Persistence resolves $XDG_CONFIG_HOME/fxroute (config),
# $XDG_STATE_HOME (measurement/autosub state) and $XDG_DATA_HOME with a
# $HOME fallback, so point the three XDG roots at a fresh throwaway dir for
# the whole run. Tests that sandbox themselves (TemporaryDirectory + XDG
# override, or path patches) are unaffected; everything else lands in the
# sandbox, which is removed afterwards. NOTE: HOME itself is deliberately
# untouched — project dependencies resolve through the user site-packages
# under $HOME/.local, so overriding HOME would break every test import.
# See also scripts/check_test_xdg_isolation.py, which audits every test
# file for product-config writes so future tests cannot regress this.
FXROUTE_TEST_SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/fxroute-tests-XXXXXXXX")"
export XDG_CONFIG_HOME="$FXROUTE_TEST_SANDBOX/config"
export XDG_STATE_HOME="$FXROUTE_TEST_SANDBOX/state"
export XDG_DATA_HOME="$FXROUTE_TEST_SANDBOX/data"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_DATA_HOME"
cleanup_test_sandbox() { rm -rf "$FXROUTE_TEST_SANDBOX"; }
trap cleanup_test_sandbox EXIT

MODE="local"
VERBOSE=0
for arg in "$@"; do
    case "$arg" in
        --list) MODE="list" ;;
        --all) MODE="all" ;;
        --verbose) VERBOSE=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

PYTHON="python3"
NODE="node"
# Node-based tests (test_*.js / check_*.js) only run if node is
# installed; otherwise skip them cleanly.
NODE_SKIP_REASON=""
if ! command -v node >/dev/null 2>&1; then
    NODE_SKIP_REASON="node binary not installed on this host"
fi

# ---------------------------------------------------------------------------
# Test discovery
# ---------------------------------------------------------------------------

PY_TESTS=()
JS_TESTS=()
CHECK_TESTS=()
for f in scripts/test_*.py; do
    [ -e "$f" ] && PY_TESTS+=("$f")
done
for f in scripts/test_*.js; do
    [ -e "$f" ] && JS_TESTS+=("$f")
done
for f in scripts/check_*.py scripts/check_*.js; do
    [ -e "$f" ] && CHECK_TESTS+=("$f")
done

NATIVE_HELPER_BIN=""
NATIVE_HELPER_TESTS=(
    "scripts/test_native_dsp_control.py"
    "scripts/test_native_dsp_convolver_sr_level.py"
    "scripts/test_native_dsp_effects.py"
    "scripts/test_native_dsp_filters.py"
    "scripts/test_native_dsp_ir_limiter.py"
    "scripts/test_native_dsp_matrix.py"
    "scripts/test_native_dsp_stages.py"
)
NATIVE_C_TESTS=(
    "native_dsp/test_lv2_host.sh"
    "native_dsp/test_master_gain.sh"
    "native_dsp/test_loudness_strength.sh"
)

# Native DSP suites build and run the C engine; they need the dev packages
# for the LV2 host and DSP libraries.  Without them the tests must be
# skipped with an explicit reason, never counted as passed.
NATIVE_DEPS_PKGS="libebur128 lilv-0 samplerate speexdsp"
NATIVE_DEPS_SKIP_REASON=""
if ! pkg-config --exists $NATIVE_DEPS_PKGS; then
    NATIVE_DEPS_SKIP_REASON="missing native DSP build dependencies (pkg-config: $NATIVE_DEPS_PKGS); build on .104"
fi

# Browser tests run through Python Playwright and need the
# Playwright-managed Chromium binary.  When it is missing, each browser
# test would otherwise fail with a launch error; detect it once here and
# skip the whole group with a setup hint instead.  Never auto-install in
# the suite run (see the header comment for the one-time host setup).
BROWSER_TESTS=(
    scripts/check_css_reorg_snapshot.py
    scripts/check_select_popup_theme.py
    scripts/check_ui_walkthrough.py
    scripts/check_viewports.py
    scripts/test_brand_geometry.py
    scripts/test_favorite_button_geometry.py
    scripts/test_favorite_primitives_geometry.py
    scripts/test_footer_badge_geometry.py
    scripts/test_library_folder_back.py
    scripts/test_measurement_ui_responsive.py
    scripts/test_reduced_motion.py
    scripts/test_subwoofer_controls_geometry.py
    scripts/test_tidal_direct_search.py
    scripts/test_tidal_header_geometry.py
    scripts/test_tidal_subbar_overflow.py
    scripts/test_view_tab_geometry.py
)

BROWSER_SKIP_REASON=""
if ! "$PYTHON" -c "import playwright" >/dev/null 2>&1; then
    BROWSER_SKIP_REASON="playwright python package not installed (run: pip install playwright)"
else
    # Probe with a real launch: verifies the managed Chromium binary and
    # its system dependencies in one cheap check.
    if ! "$PYTHON" - <<'PYEOF' >/dev/null 2>&1
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    browser.close()
PYEOF
    then
        BROWSER_SKIP_REASON="playwright chromium browser not installed (run: python3 -m playwright install chromium)"
    fi
fi

# Tests that need live PipeWire graph / audio hardware on .104.
# (Kept as a category for future hardware-bound suites; currently empty
# because test_measurement_sr_session.py mocks the rate plumbing and runs
# locally with 16/16 passing.)
HARDWARE_TESTS=(
)

# ---------------------------------------------------------------------------
# Inventory listing
# ---------------------------------------------------------------------------

if [ "$MODE" = "list" ]; then
    echo "== Python tests (local) =="
    for f in "${PY_TESTS[@]}"; do
        case " ${NATIVE_HELPER_TESTS[*]} ${HARDWARE_TESTS[*]} " in
            *" $f "*) echo "  [.104]  $f" ;;
            *) echo "  [local] $f" ;;
        esac
    done
    echo "== JavaScript tests (local) =="
    for f in "${JS_TESTS[@]}"; do
        echo "  [local] $f"
    done
    echo "== C tests (native DSP) =="
    for f in "${NATIVE_C_TESTS[@]}"; do
        echo "  [.104]  $f"
    done
    echo "== Regression checks (local) =="
    for f in "${CHECK_TESTS[@]}"; do
        echo "  [local] $f"
    done
    echo
    echo "Legend: [local] läuft lokal mit installierten Projektabhängigkeiten (z. B. uvicorn, requests), [.104] benötigt native Helper-Binary (Build-Deps: $NATIVE_DEPS_PKGS) oder Live-PipeWire/Hardware."
    echo "Browser tests (Playwright/Chromium) additionally need the Playwright-managed browser: pip install playwright && python3 -m playwright install chromium"
    exit 0
fi

# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------

PASS=0
FAIL=0
SKIP=0
FAILED_TESTS=()

run_one() {
    local name="$1" runner="$2" reason="$3"
    if [ -n "$reason" ]; then
        echo "SKIP  $name ($reason)"
        SKIP=$((SKIP + 1))
        return 0
    fi
    if [ "$VERBOSE" = "1" ]; then
        if "$runner" "$name" >/tmp/fxroute-test-$$.log 2>&1; then
            echo "PASS  $name"
            PASS=$((PASS + 1))
        else
            if [ $? -eq 77 ]; then
                echo "SKIP  $name (prerequisite unavailable on this host)"
                SKIP=$((SKIP + 1))
            else
                echo "FAIL  $name"
                cat /tmp/fxroute-test-$$.log
                FAILED_TESTS+=("$name")
                FAIL=$((FAIL + 1))
            fi
        fi
        rm -f /tmp/fxroute-test-$$.log
    else
        local out
        out=$("$runner" "$name" 2>&1)
        local status=$?
        if [ "$status" -eq 0 ]; then
            echo "PASS  $name"
            PASS=$((PASS + 1))
        elif [ "$status" -eq 77 ]; then
            echo "SKIP  $name (prerequisite unavailable on this host)"
            SKIP=$((SKIP + 1))
        else
            echo "FAIL  $name"
            echo "$out" | tail -20 | sed 's/^/      /'
            FAILED_TESTS+=("$name")
            FAIL=$((FAIL + 1))
        fi
    fi
}

# ---------------------------------------------------------------------------
# Run everything
# ---------------------------------------------------------------------------

echo "== FXRoute test runner (mode: $MODE) =="
if [ "$MODE" != "all" ] && [ -n "$BROWSER_SKIP_REASON" ]; then
    echo "NOTE: browser tests will be skipped: $BROWSER_SKIP_REASON"
    echo "      (the other tests still run; use --all to force the browser tests)"
fi
echo

for f in "${PY_TESTS[@]}"; do
    reason=""
    if [ "$MODE" != "all" ]; then
        case " ${NATIVE_HELPER_TESTS[*]} " in
            *" $f "*) reason="$NATIVE_DEPS_SKIP_REASON" ;;
        esac
        case " ${HARDWARE_TESTS[*]} " in
            *" $f "*) reason="needs live PipeWire/measurement host on .104" ;;
        esac
        case " ${BROWSER_TESTS[*]} " in
            *" $f "*) reason="$BROWSER_SKIP_REASON" ;;
        esac
    fi
    run_one "$f" "$PYTHON" "$reason"
done

for f in "${NATIVE_C_TESTS[@]}"; do
    run_one "$f" "sh" "$NATIVE_DEPS_SKIP_REASON"
done

for f in "${JS_TESTS[@]}"; do
    run_one "$f" "$NODE" "$NODE_SKIP_REASON"
done

for f in "${CHECK_TESTS[@]}"; do
    if [[ "$f" == *.js ]]; then
        run_one "$f" "$NODE" "$NODE_SKIP_REASON"
    else
        reason=""
        if [ "$MODE" != "all" ]; then
            case " ${BROWSER_TESTS[*]} " in
                *" $f "*) reason="$BROWSER_SKIP_REASON" ;;
            esac
        fi
        run_one "$f" "$PYTHON" "$reason"
    fi
done

echo
echo "== Summary =="
echo "  passed: $PASS"
echo "  failed: $FAIL"
echo "  skipped: $SKIP"
if [ ${#FAILED_TESTS[@]} -gt 0 ]; then
    printf '  failed tests: %s\n' "${FAILED_TESTS[*]}"
fi
echo

[ "$FAIL" -eq 0 ]
