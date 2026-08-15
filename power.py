"""systemd-logind capability detection and power actions.

FXRoute exposes only two system actions through this module:

* ``suspend``     -> ``org.freedesktop.login1.Manager.Suspend``
* ``power_off``   -> ``org.freedesktop.login1.Manager.PowerOff``

Both calls go through the D-Bus system bus to ``org.freedesktop.login1``.
The polkit rule installed by ``install.sh`` (see ``assets/polkit/``)
restricts authority to these two action IDs and to the install user, so
nothing else can be triggered from this module.

The implementation prefers the ``dbus-send`` binary (provided by the
``dbus`` package on every supported distro and shipped in the installer
dependency list) over the in-process python ``dbus`` bindings.  This keeps
FXRoute compatible with the project venv that does not pull
``python3-dbus`` as a pip dependency and removes the exposure to a
process-global gin trap that an in-process D-Bus connection would create
on top of the existing FastAPI asyncio loop.

The exposed surface is intentionally narrow:

* :func:`get_capabilities` queries ``Manager.CanSuspend`` and
  ``Manager.CanPowerOff`` via D-Bus, maps the textual response to a
  boolean-ish status and reports dbus-send / login1 reachability.
* :func:`request_suspend` / :func:`request_power_off` invoke the matching
  ``Manager`` method.  Permission failures and login1 unavailability are
  returned as ``ok=False`` instead of raising; HTTP callers translate them
  into proper HTTP error codes.

The :class:`PowerBackend` class exposes an injectable ``runner`` hook so
tests can drive the module without touching the real system bus.  The
helper :func:`set_backend_for_tests` swaps the module-level default
backend (used by main.py) for a fake during unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


_LOGIND_OBJECT_PATH = "/org/freedesktop/login1"
_LOGIND_DESTINATION = "org.freedesktop.login1"
_LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"
_DBUS_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# suspend/power_off are the only two logind actions FXRoute ever invokes
# over D-Bus.  Keeping the allow-list here makes audit grep trivial and
# forbids accidentally branching into reboot/hibernate from HTTP callers.
_ALLOWED_LOGIND_ACTIONS = frozenset({"suspend", "power_off"})
_ALLOWED_LOGIND_CALLS = {
    "suspend": "Suspend",
    "power_off": "PowerOff",
}

# Seconds the helper waits for any single dbus-send child.  Anything above
# this is treated as "login1 hung" / "dbus-send panic" and reported as
# unreachable instead of leaving the asyncio loop blocked.
_DBUS_SEND_TIMEOUT_SECONDS = 8.0


@dataclass
class PowerCallResult:
    """The result of a suspend / power-off invocation.

    Callers (HTTP layer) translate the ``status`` into HTTP responses:

    * ``"ok"``         -> 202 Accepted; the system will suspend/shut down
      asynchronously.  The frontend should switch its badge to the
      "Suspending…" / "Shutting down…" state.
    * ``"denied"``     -> 403; polkit refused the request even with the
      FXRoute-installed rule.  Either the rule is missing/broken or the
      caller is not the configured user.
    * ``"unavailable"`` -> 503; dbus-send or login1 is missing on the host,
      so the requested action cannot even be dispatched.
    """

    ok: bool
    status: str
    action: str
    error: Optional[str] = None


@dataclass
class PowerCapabilities:
    """Capability payload returned by :func:`get_capabilities`.

    The frontend uses the three textual fields to decide which menu
    entries are offered:

    * ``"yes"``       -> the action is directly available for the user.
    * ``"challenge"`` -> the action is technically possible but requires
      authentication (resolved when the polkit rule authorises the action
      for the FXRoute install user).
    * ``"no"``        -> logind reports the action as impossible on this
      host (e.g. ``CanSuspend=no`` on a server without PM).  The frontend
      hides the menu entry.
    * ``"unavailable"`` -> dbus / logind cannot be reached; both menu
      entries stay hidden.
    """

    available: bool
    suspend: str
    power_off: str
    unavailable_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# dbus-send subprocess helpers
# ---------------------------------------------------------------------------


_run_subprocess = Callable[..., Awaitable["_SubprocessResult"]]


@dataclass
class _SubprocessResult:
    """Bounded result of a dbus-send child process."""

    returncode: int
    stdout: str
    stderr: str


async def _default_runner(
    *args: str,
    timeout: float = _DBUS_SEND_TIMEOUT_SECONDS,
) -> _SubprocessResult:
    """Run ``args`` as a fresh subprocess and capture both streams.

    The child is started without a shell (``shell=False``) so the argument
    list cannot be reinterpreted as a shell command, and so there is no
    quoting context to manipulate.  A TIMEOUT/SIGKILL terminates the
    entire process group so a runaway dbus-send cannot leak past the
    configured bound.
    """

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=2.0
            )
        except Exception:
            stdout_b, stderr_b = b"", b""
        return _SubprocessResult(
            returncode=-1,
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace")
            + f"\ndbus-send timed out after {timeout:.0f}s",
        )
    return _SubprocessResult(
        returncode=proc.returncode,
        stdout=stdout_b.decode(errors="replace"),
        stderr=stderr_b.decode(errors="replace"),
    )


# ---------------------------------------------------------------------------
# PowerBackend
# ---------------------------------------------------------------------------


class PowerBackend:
    """Stateless capability + action backend for system power controls.

    The backend is safe to construct once at module import time and to
    reuse across all HTTP requests because every call opens its own
    short-lived ``dbus-send`` child and never holds a persistent
    connection.
    """

    def __init__(
        self,
        *,
        dbus_send_path: str = "dbus-send",
        runner: _run_subprocess = _default_runner,
    ) -> None:
        self._dbus_send_path = dbus_send_path
        self._runner = runner

    @property
    def uses_real_dbus(self) -> bool:
        """True when this backend actually shells out to ``dbus-send``.

        Tests inject a fake runner with no ``dbus-send`` invocation; UI
        layers can use this flag to skip the capability probe entirely
        when running against the test backend.
        """

        return self._runner is _default_runner

    async def get_capabilities(self) -> PowerCapabilities:
        """Return ``CanSuspend`` / ``CanPowerOff`` mapped to UI status."""

        suspend = await self._query_logind_property("CanSuspend")
        power_off = await self._query_logind_property("CanPowerOff")

        unavailable_reason: Optional[str] = None
        available = True
        if suspend is None and power_off is None:
            # Neither query returned an answer: dbus-send or login1 is
            # unreachable, which collapses the whole capability surface
            # into "unavailable".
            available = False
            unavailable_reason = "systemd-logind not reachable via dbus"

        return PowerCapabilities(
            available=available,
            suspend=suspend or "unavailable",
            power_off=power_off or "unavailable",
            unavailable_reason=unavailable_reason,
        )

    async def request_suspend(self) -> PowerCallResult:
        return await self._invoke_logind_action("suspend")

    async def request_power_off(self) -> PowerCallResult:
        return await self._invoke_logind_action("power_off")

    # -- internal helpers -------------------------------------------------

    # Names that, in modern logind (systemd >= 256), were demoted from
    # ``Manager`` properties to ``Manager`` methods returning ``out s``.
    # FXRoute always probes via the *method* form first and only falls
    # back to ``Properties.Get`` when logind reports ``UnknownMethod``;
    # on older systemd the property form succeeds, on modern logind the
    # method form succeeds - either path lands on the same parser.
    _LOGIND_METHOD_CAPABILITY_NAMES = frozenset({"CanSuspend", "CanPowerOff"})

    async def _query_logind_property(self, name: str) -> Optional[str]:
        """Probe a textual logind capability (``yes``/``no``/``challenge``).

        Logind's ``CanX`` capability surface changed shape across
        systemd versions:

        * systemd < 256 -- ``CanSuspend`` / ``CanPowerOff`` are
          ``Manager`` *properties*; reading them via
          ``org.freedesktop.DBus.Properties.Get`` returns ``yes/no/etc``.
        * systemd >= 256 -- the same names became ``Manager`` *methods*
          with an ``out s result`` signature.  Reading them via
          ``Properties.Get`` errors out with ``UnknownProperty``.

        FXRoute always tries the method form first (works on modern
        logind; errors with ``UnknownMethod`` on older ones) and falls
        back to the property form so the capability surface stays
        consistent on every supported distro.

        Returns:
            * the textual value returned by login1 (``"yes"``, ``"no"``,
              ``"challenge"``, ``"inhibited"``, ...) when reachable.
            * ``None`` when both calls and the Caller service are
              unreachable, so the capability endpoint can collapse to
              "unavailable".
        """

        method_args = (
            self._dbus_send_path,
            "--system",
            "--print-reply",
            "--dest=" + _LOGIND_DESTINATION,
            _LOGIND_OBJECT_PATH,
            f"{_LOGIND_MANAGER_IFACE}.{name}",
        )
        try:
            method_result = await self._runner(
                *method_args, timeout=_DBUS_SEND_TIMEOUT_SECONDS
            )
        except FileNotFoundError:
            return None
        if method_result.returncode == 0:
            return _parse_dbus_string_variant(method_result.stdout)

        method_error, _ = _parse_dbus_error(
            method_result.stderr or method_result.stdout
        )
        # Modern logind: success via method.  Older logind: method
        # call returns ``UnknownMethod`` and we have to read the
        # property via ``Properties.Get`` instead.  Both UnknownProperty
        # (modern) and UnknownMethod (older) plus runtime errors
        # collapse into a single ``None`` so the frontend stays tidy.
        if method_error not in (
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.freedesktop.DBus.Error.UnknownProperty",
        ) and name in self._LOGIND_METHOD_CAPABILITY_NAMES:
            return None

        property_args = (
            self._dbus_send_path,
            "--system",
            "--print-reply",
            "--dest=" + _LOGIND_DESTINATION,
            _LOGIND_OBJECT_PATH,
            _DBUS_PROPERTIES_IFACE + ".Get",
            f"string:{_LOGIND_MANAGER_IFACE}",
            f"string:{name}",
        )
        try:
            property_result = await self._runner(
                *property_args, timeout=_DBUS_SEND_TIMEOUT_SECONDS
            )
        except FileNotFoundError:
            return None
        if property_result.returncode != 0:
            return None
        return _parse_dbus_string_variant(property_result.stdout)

    async def _invoke_logind_action(self, action: str) -> PowerCallResult:
        """Trigger a single logind action through ``dbus-send``.

        ``action`` MUST belong to :data:`_ALLOWED_LOGIND_ACTIONS`.  Any
        other value is rejected without dispatching the call.
        """

        if action not in _ALLOWED_LOGIND_ACTIONS:
            return PowerCallResult(
                ok=False,
                status="denied",
                action=action,
                error=f"unsupported action: {action}",
            )

        method = _ALLOWED_LOGIND_CALLS[action]
        args = (
            self._dbus_send_path,
            "--system",
            "--print-reply",
            "--dest=" + _LOGIND_DESTINATION,
            _LOGIND_OBJECT_PATH,
            f"{_LOGIND_MANAGER_IFACE}.{method}",
            "boolean:false",
        )
        try:
            result = await self._runner(*args, timeout=_DBUS_SEND_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            return PowerCallResult(
                ok=False,
                status="unavailable",
                action=action,
                error=f"dbus-send not available: {exc}",
            )

        if result.returncode == 0:
            return PowerCallResult(ok=True, status="ok", action=action)

        error_name, message = _parse_dbus_error(result.stderr or result.stdout)
        if error_name in {
            "org.freedesktop.login1.PermissionDenied",
            "org.freedesktop.DBus.Error.AccessDenied",
            "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired",
        }:
            # Polkit refused the request: the FXRoute polkit rule is
            # either missing, broken for this user, or intentionally
            # tighter than what was requested.  The frontend must surface
            # this to the operator (permission denied != system failure).
            status = "denied"
        elif error_name in {
            "org.freedesktop.DBus.Error.ServiceUnknown",
            "org.freedesktop.DBus.Error.UnknownObject",
            "org.freedesktop.DBus.Error.NoReply",
        }:
            status = "unavailable"
        else:
            status = "denied"

        return PowerCallResult(
            ok=False,
            status=status,
            action=action,
            error=message or error_name or f"dbus-send exited {result.returncode}",
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_DBUS_STRING_VARIANT_RE = re.compile(
    r"^\s*(?:variant\s+)?string\s+\"([^\"\\]*(?:\\.[^\"\\]*)*)\"\s*$",
    re.MULTILINE,
)
_DBUS_ERROR_NAME_RE = re.compile(r"Error\s+([A-Za-z0-9_.]+)\s*:\s*(.*)$")
_DBUS_ERROR_NAME_BARE_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*:\s*(.*)$")


def _parse_dbus_string_variant(payload: str) -> Optional[str]:
    """Extract the value of a single ``string "..."`` from a dbus-send reply.

    Covers both the bare ``string "yes"`` shape returned by direct
    method calls (``Manager.CanSuspend``) and the prefixed
    ``variant string "yes"`` shape returned by ``Properties.Get`` on
    older systemd.  Login1 never reports anything else for these
    reads, so a single match is enough.
    """

    match = _DBUS_STRING_VARIANT_RE.search(payload)
    if not match:
        return None
    return match.group(1)


def _parse_dbus_error(payload: str) -> tuple[str, str]:
    """Extract the error name and message from a dbus-send error reply.

    dbus-send prints errors in two slightly different shapes depending
    on the protocol version (``Error <name>: <msg>`` vs. ``<name>:
    <msg>``).  Both share a name : message structure, so the same
    regex order works for both.
    """

    match = _DBUS_ERROR_NAME_RE.search(payload) or _DBUS_ERROR_NAME_BARE_RE.search(payload)
    if not match:
        return "", payload.strip().splitlines()[0] if payload.strip() else ""
    name, message = match.group(1), match.group(2).strip()
    return name, message


# ---------------------------------------------------------------------------
# Public vocabulary + capability helpers consumed by main.py
# ---------------------------------------------------------------------------

# Outer bound for any single capability probe.  ``_DBUS_SEND_TIMEOUT_SECONDS``
# already prevents an individual dbus-send child from running forever; this
# second bound keeps a hung login1 from pinning the FastAPI event loop when
# the child somehow does not honour the inner timeout (for example when the
# kernel / dbus daemon itself is stuck).
_SYSTEM_POWER_CAPABILITIES_TIMEOUT_SECONDS = 10.0


def is_logind_call_executable(value: str) -> bool:
    """Strict-Yes gate for a textual logind ``CanX`` answer.

    systemd-logind's ``Manager.CanSuspend`` / ``Manager.CanPowerOff``
    properties return one of the small enum the task enumerates:

    ``yes``                                -> directly executable
    ``no``                                 -> denied
    ``na``                                 -> not applicable on this host
    ``challenge``                          -> needs interactive polkit auth
    ``inhibited``                          -> locked by an inhibitor lock
    ``inhibitor-blocked``                  -> locked by an inhibitor lock alias
    ``challenge-inhibitor-blocked``        -> combined auth + inhibitor block

    Only the exact text ``"yes"`` maps to ``True``.  Every other answer,
    including the ``challenge`` / inhibitor variants, maps to ``False``.
    Treating "challenge" as supported would imply privilege axes the
    polkit rule does not grant (see
    ``assets/polkit/50-fxroute-power.rules``), and the rule deliberately
    does not extend with ``*-multiple-sessions`` or ``*-ignore-inhibit``.
    """

    return value == "yes"


async def is_now_supported(action: str) -> tuple[bool, str]:
    """Re-probe logind right before an action so a stale UI snapshot,
    a freshly-engaged inhibitor lock, or a direct CLI POST cannot slip
    past the menu gate.

    Returns ``(executable_bool, raw_logind_value)``.  When the probe
    surfaces a degraded state the function falls through to
    ``(False, "unavailable")`` so the caller (a FastAPI handler) can
    surface a controlled 409 without ever dispatching a dbus-send
    command the operator's UI had every reason not to advertise.
    """

    try:
        caps = await asyncio.wait_for(
            get_capabilities(),
            timeout=_SYSTEM_POWER_CAPABILITIES_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return False, "unavailable"
    value = caps.suspend if action == "suspend" else caps.power_off
    return is_logind_call_executable(value), value


# ---------------------------------------------------------------------------
# Module-level singleton helpers used by main.py
# ---------------------------------------------------------------------------


_power_backend: Optional[PowerBackend] = None


def get_power_backend() -> PowerBackend:
    """Return the lazily constructed module-level backend.

    A single instance is shared by every HTTP request because each
    operation is bounded and stateless; recreating it on every call
    would only re-resolve the ``dbus-send`` path for no benefit.
    """

    global _power_backend
    if _power_backend is None:
        _power_backend = PowerBackend()
    return _power_backend


def set_backend(backend: Optional[PowerBackend]) -> Optional[PowerBackend]:
    """Override the module-level backend, returning the previous one.

    Tests call this with a fake :class:`PowerBackend` (or ``None`` to
    restore the default) so they can drive the HTTP layer without
    touching the real D-Bus.
    """

    global _power_backend
    previous = _power_backend
    _power_backend = backend
    return previous


async def get_capabilities() -> PowerCapabilities:
    """Return current logind capabilities, bounded by the subsystem timeout.

    The per-subprocess timeout in ``_DBUS_SEND_TIMEOUT_SECONDS`` is layered
    by ``_SYSTEM_POWER_CAPABILITIES_TIMEOUT_SECONDS`` here so the caller
    does not need to repeat ``asyncio.wait_for`` for every probe and so a
    single hung login1 cannot pin the entire FXRoute event loop.
    """

    return await asyncio.wait_for(
        get_power_backend().get_capabilities(),
        timeout=_SYSTEM_POWER_CAPABILITIES_TIMEOUT_SECONDS,
    )


async def request_suspend() -> PowerCallResult:
    return await get_power_backend().request_suspend()


async def request_power_off() -> PowerCallResult:
    return await get_power_backend().request_power_off()
