# SPDX-License-Identifier: AGPL-3.0-only
"""Internal, single-job transport for full-resolution capture evidence.

This is acquisition data, not Speaker Align authorization. Input/link provenance
does not prove a physical upstream tap or a fixed microphone position. The future
acquisition owner must attest those separately and apply the analysis gates.
"""

from __future__ import annotations

from copy import deepcopy
import threading

import numpy as np


class CaptureEvidence:
    """Opt in via ``MeasurementStore.start_measurement(capture_evidence=owner)``.

    One owner binds once, retains only the current attempt, and transfers the
    final evidence once via ``take()`` after the worker task finishes cleanup.
    Failed/cancelled jobs discard their buffers. Nothing enters job/result JSON.
    A successful capture may still carry rejected/fallback reference metadata;
    transport deliberately preserves that verdict rather than certifying timing.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "unbound"
        self._job_id = None
        self._ir = None
        self._analysis = None
        self._evidence = None
        self._completion = None

    def _bind(self, job_id: str) -> None:
        with self._lock:
            if self._state != "unbound":
                raise ValueError("Capture evidence owner is already bound to a job")
            self._job_id = job_id
            self._state = "pending"

    def _begin_attempt(self) -> None:
        with self._lock:
            if self._state != "pending":
                raise RuntimeError("Capture evidence owner is not accepting attempts")
            self._ir = self._analysis = self._evidence = None

    def _receive_ir(self, ir: np.ndarray, analysis: dict) -> None:
        with self._lock:
            if self._state != "pending":
                raise RuntimeError("Capture evidence owner is not accepting an IR")
            self._ir = np.array(ir, dtype=np.float64, copy=True)
            self._ir.setflags(write=False)
            # The policy mutates the same analysis with its final ER verdict.
            # Identity prevents pairing one retry's samples with another result.
            self._analysis = analysis

    def _select(self, analysis: dict, *, job: dict, capture: dict,
                calibration_curve: tuple | None = None) -> None:
        """Pin the final attempt's IR, analysis and microphone calibration.

        ``calibration_curve`` is the ``(frequencies_hz, offsets_db)`` pair the
        analysis applied to its response points, or ``None`` when none was
        applied: a consumer judging levels from the raw IR needs the same one.
        """
        curve = None
        if calibration_curve is not None:
            frequencies, offsets = calibration_curve
            curve = {"frequencies_hz": [float(value) for value in frequencies],
                     "offsets_db": [float(value) for value in offsets]}
        with self._lock:
            if (self._state != "pending" or job["id"] != self._job_id
                    or self._ir is None or self._analysis is not analysis):
                raise RuntimeError("Final capture analysis has no matching full-resolution IR")
            self._evidence = {
                "impulse_response": self._ir,
                "time_reference": "deconvolved-sweep-origin",
                "analysis": deepcopy({key: value for key, value in analysis.items()
                                      if not key.startswith("_")}),
                "measurement_target": deepcopy(job.get("measurement_target")),
                "capture": deepcopy(capture),
                "calibration_curve": curve,
            }

    def _discard(self) -> None:
        with self._lock:
            self._ir = self._analysis = self._evidence = None
            self._completion = None
            self._state = "unavailable"

    def _attach(self, task, job: dict) -> None:
        with self._lock:
            self._completion = (task, job)
        task.add_done_callback(lambda completed: self._finish(completed, job))

    def _finish(self, task, job: dict) -> None:
        with self._lock:
            # take() may reconcile completion before the queued callback runs.
            # Never let that late callback republish already consumed evidence.
            if self._state != "pending":
                return
            successful = (not task.cancelled() and task.exception() is None
                          and job.get("status") == "completed" and self._evidence is not None)
            self._ir = self._analysis = None
            self._completion = None
            if successful:
                self._state = "ready"
            else:
                self._evidence = None
                self._state = "unavailable"

    def take(self) -> dict:
        """Transfer final evidence after draining/awaiting the successful job.

        The returned IR is full-rate and unshifted, with the same origin as the
        analyzer's absolute arrival/reference indices. Preview data is unrelated.
        """
        with self._lock:
            completion = self._completion
        # Awaiting an already-finished Task does not yield to its queued done
        # callbacks. Reconcile here as well so await/drain is a sufficient fence.
        if completion is not None and completion[0].done():
            self._finish(*completion)
        with self._lock:
            if self._state != "ready":
                raise RuntimeError("Capture evidence is not ready or is unavailable")
            evidence = self._evidence
            self._evidence = None
            self._state = "consumed"
            return evidence
