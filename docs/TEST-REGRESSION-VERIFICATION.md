# Diagnosed test corrections — 2026-10-17

Only tests and test support changed; product code and demo assets are unchanged.

## Focused verification

| Command | Result |
| --- | --- |
| `python3 scripts/test_autosub_candidate_state.py` | 36 tests passed |
| `python3 scripts/test_event_loop_offload_radio_start.py` | 10 tests passed |
| `python3 scripts/test_measurement_all_banks.py` | 13 tests passed |
| `node scripts/test_demo_output_state.js` | Passed |
| `python3 scripts/check_crossover_trim_ui.py` | Passed |
| `python3 scripts/check_output_system_ui.py` | Passed |
| `python3 scripts/check_subwoofer_trim_ui.py` | Passed |
| `python3 scripts/test_demo_server_lifecycle.py` | 3 tests passed |

The browser checks also passed concurrently, each using its own ephemeral
loopback port and the existing live-worktree demo handler. The already-running
demo listener was unchanged. Lifecycle tests cover ready shell/assets,
independent listeners, and cleanup after normal and exceptional exits.

Candidate rollback/persistence checks remain intact. Measurement and demo
checks distinguish a mixed individual A/B selection from a joint B switch,
including equal Neutral slots, and verify that Global is not switched with
the individual banks. Surround Global checks cover independent group
selections, equal A/B assignments, and preservation of an explicitly selected B.

Python syntax checks for all changed Python scripts, Node syntax validation,
and `git diff --check` passed.

Read-only code review found no actionable issues or weakened assertions before
the full-suite run.

## Full-suite verification

`scripts/run_tests.sh` was executed once after focused verification and review.

| Runner result | Files/checks |
| --- | ---: |
| Passed | 632 |
| Failed | 0 |
| Skipped | 21 |

All seven requested scripts and the new lifecycle tests passed in this run.
The 21 skips are the expected 16 native DSP Python suites and five C wrappers:
this development host lacks the `pkg-config` dependencies `libebur128`,
`lilv-0`, `samplerate`, and `speexdsp`. These skips are not counted as passes.
