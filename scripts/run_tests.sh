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
# Usage:
#   scripts/run_tests.sh            # run everything runnable locally
#   scripts/run_tests.sh --list     # print categorized test inventory
#   scripts/run_tests.sh --all      # also attempt .104-only tests
#   scripts/run_tests.sh --verbose  # show per-test output

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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
# Node-basierte Tests (test_*.js / check_*.js) sind nur ausführbar, wenn
# ein node-Binary auf dem Host vorhanden ist; sonst sauber überspringen.
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

# Tests that require the native 2.1 helper binary built from
# pipewire_stage1/ (built on .104; not available in this repo checkout).
NATIVE_HELPER_BIN="pipewire_stage1/build/fxroute_21_passthrough"
NATIVE_HELPER_TESTS=(
    "scripts/test_native_helper_alignment.py"
    "scripts/test_native_helper_bass_routing.py"
    "scripts/test_native_helper_sub_gain.py"
)

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
    echo "== Regression checks (local) =="
    for f in "${CHECK_TESTS[@]}"; do
        echo "  [local] $f"
    done
    echo
    echo "Legend: [local] läuft lokal mit installierten Projektabhängigkeiten (z. B. uvicorn, requests), [.104] benötigt native Helper-Binary oder Live-PipeWire/Hardware."
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
            echo "FAIL  $name"
            cat /tmp/fxroute-test-$$.log
            FAILED_TESTS+=("$name")
            FAIL=$((FAIL + 1))
        fi
        rm -f /tmp/fxroute-test-$$.log
    else
        local out
        out=$("$runner" "$name" 2>&1)
        if [ $? -eq 0 ]; then
            echo "PASS  $name"
            PASS=$((PASS + 1))
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
echo

for f in "${PY_TESTS[@]}"; do
    reason=""
    if [ "$MODE" != "all" ]; then
        case " ${NATIVE_HELPER_TESTS[*]} " in
            *" $f "*) reason="needs native helper binary $NATIVE_HELPER_BIN (build on .104)" ;;
        esac
        case " ${HARDWARE_TESTS[*]} " in
            *" $f "*) reason="needs live PipeWire/measurement host on .104" ;;
        esac
    fi
    run_one "$f" "$PYTHON" "$reason"
done

for f in "${JS_TESTS[@]}"; do
    run_one "$f" "$NODE" "$NODE_SKIP_REASON"
done

for f in "${CHECK_TESTS[@]}"; do
    if [[ "$f" == *.js ]]; then
        run_one "$f" "$NODE" "$NODE_SKIP_REASON"
    else
        run_one "$f" "$PYTHON" ""
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
