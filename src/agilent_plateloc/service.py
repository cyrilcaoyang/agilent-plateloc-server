"""Service layer that exposes the PlateLoc driver as a spec-compliant
`EquipmentStatus` source.

Why this exists
---------------
The driver in ``plateloc.py`` is a thin wrapper around the ActiveX COM
control. It is synchronous and single-threaded: only one caller may
talk to the COM object at a time. The dashboard, however, polls
``GET /status`` every 2-3 seconds while operators may concurrently fire
``POST /control/*`` commands.

The service owns:

* a single driver instance (real or in-memory stub),
* an ``asyncio.Lock`` that serialises every call into the driver,
* a small in-memory state machine (``_busy_state``, ``_last_error``)
  used to compute the spec ``equipment_status`` field,
* a ``get_status()`` method that produces a fresh ``EquipmentStatus``
  envelope without ever issuing a write to the device.

If the real driver cannot be loaded (non-Windows host, missing ActiveX,
hardware off) ``dry_run=True`` swaps in a stub so the API surface stays
identical and the dashboard can be developed end-to-end on macOS/Linux.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import config as _config
from .claims import ClaimStore
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
    MetricValue,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# allowed_actions per equipment_status (v1.1)
#
# Mirrors the inverse of the SDK skill catalog's `requires_states` for
# `kind=plate_sealer` (see lab_skills/skill_catalog/plate_sealer.py). Kept
# here so the device is the source of truth: the SDK prefers our
# allowed_actions over its own catalog whenever the field is non-empty.
# ---------------------------------------------------------------------------

_ALL_PLATE_SEALER_SKILLS = [
    "startup",
    "shutdown",
    "seal.start",
    "seal.stop",
    "seal.set_temperature",
    "seal.set_time",
    "stage.in",
    "stage.out",
]

_ALLOWED_ACTIONS_BY_STATE: dict[str, list[str]] = {
    "requires_init": ["startup"],
    "ready": [
        "startup",
        "shutdown",
        "seal.start",
        "seal.set_temperature",
        "seal.set_time",
        "stage.in",
        "stage.out",
    ],
    "busy": ["shutdown", "seal.stop"],
    "degraded": ["shutdown"],
    "error": ["shutdown"],
    "e_stop": [],
    "unknown": [],
    "dry_run": list(_ALL_PLATE_SEALER_SKILLS),
}


def _coerce_float(value: Any) -> float | None:
    """Best-effort cast to ``float`` for ActiveX values that can be ``None``,
    ``int``, ``float``, or a parseable string. Returns ``None`` if the
    value cannot be coerced — callers treat that as "temperature
    unavailable" and fail closed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Stub driver for dry-run / non-Windows development
# ---------------------------------------------------------------------------


class _StubPlateLoc:
    """In-memory mock that mirrors the public ``PlateLoc`` surface.

    Only the methods the service touches are implemented; anything else
    will raise ``AttributeError`` if accidentally used.
    """

    def __init__(self) -> None:
        self.com_port = "DRY-RUN"
        self._connected = False
        self._set_temp = 170
        self._set_time = 1.2
        self._actual_temp = 22  # ambient
        self._cycle_count = 0

    # lifecycle
    def connect(self, profile: str | None = None) -> None:  # noqa: ARG002
        self._connected = True
        self._actual_temp = self._set_temp  # heat up instantly

    def close(self) -> None:
        self._connected = False

    # control
    def set_sealing_temperature(self, t: int) -> int:
        self._set_temp = int(t)
        self._actual_temp = self._set_temp
        return 0

    def set_sealing_time(self, s: float) -> int:
        self._set_time = float(s)
        return 0

    def start_cycle(self) -> int:
        return 0

    def stop_cycle(self) -> int:
        self._cycle_count += 1
        return 0

    def move_stage_in(self) -> int:
        return 0

    def move_stage_out(self) -> int:
        return 0

    # readings
    def get_actual_temperature(self) -> int:
        return self._actual_temp

    def get_sealing_temperature(self) -> int:
        return self._set_temp

    def get_sealing_time(self) -> float:
        return self._set_time

    def get_cycle_count(self) -> int:
        return self._cycle_count

    def get_firmware_version(self) -> str:
        return "DRY-RUN-1.0"

    def get_version(self) -> str:
        return "DRY-RUN-AX"

    def enumerate_profiles(self) -> list[str]:
        return ["dry_run_default"]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


_RECENT_ERROR_WINDOW_S = 60.0  # how long an error keeps the device in `error`


class TemperatureOutOfBand(Exception):
    """Raised by ``start_cycle`` when the heater is not at setpoint.

    Layer-1 interlock from ``docs/INTERLOCKS.md``: the device refuses to
    start a seal cycle when ``abs(actual - setpoint) > tolerance``.
    Carries enough structured data for the API layer to emit a clean
    HTTP 412 body without re-querying the driver.
    """

    def __init__(
        self,
        message: str,
        *,
        actual_c: float | None,
        setpoint_c: float | None,
        tolerance_c: float,
        retry_after_s: float | None,
    ) -> None:
        super().__init__(message)
        self.actual_c = actual_c
        self.setpoint_c = setpoint_c
        self.tolerance_c = tolerance_c
        self.retry_after_s = retry_after_s


class StageNotLoaded(Exception):
    """Raised by ``start_cycle`` when the plate stage is not in the
    loaded ("in") position.

    Layer-1 interlock (v1.3.0): the device refuses to start a seal
    cycle unless ``components.stage.state == "in"``. The Agilent COM
    API does not expose a stage-position query, so the position is
    command-tracked and resets to ``"unknown"`` on process restart
    or mid-cycle failure (the plate may have been moved manually
    while the service was down — refusing safely beats guessing).

    Carries the stage state at refusal time so the API layer can
    emit ``{"detail", "stage_state", "required": "in"}`` without
    re-reading service state.
    """

    def __init__(self, message: str, *, stage_state: str) -> None:
        super().__init__(message)
        self.stage_state = stage_state


class PlateLocService:
    """Wraps a ``PlateLoc`` (or ``_StubPlateLoc``) driver and produces
    spec-compliant ``EquipmentStatus`` snapshots.

    Concurrency: all driver I/O happens inside ``self._lock``. Status
    reads share the same lock so a poll cannot interleave with a write.
    """

    def __init__(
        self,
        dry_run: bool = False,
        *,
        driver_factory: Callable[[], Any] | None = None,
        enforce_claims: bool = True,
        enforce_temp_interlock: bool = True,
        enforce_stage_interlock: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        dry_run:
            When True the service uses ``_StubPlateLoc`` and reports
            ``equipment_status: dry_run`` regardless of operation.
        driver_factory:
            Optional override that returns a driver instance. Tests use
            this to inject a stub while keeping ``dry_run=False`` so
            the operational state machine (ready/busy/error) is exercised.
        enforce_claims:
            STATUS_SPEC v1.1 strictness switch. When True (default), the
            API layer rejects ``/control/*`` requests with HTTP 423 unless
            they carry a valid ``X-Claim-Token``. Set False for the
            handful of legacy / single-operator deployments that want
            v1.1 *advisory* claims (the device still publishes
            ``allowed_actions`` and ``details.claimed_by`` but does not
            block writes from clients without a token).
        enforce_temp_interlock:
            Layer-1 interlock (see ``docs/INTERLOCKS.md``). When True
            (default), ``start_cycle`` raises ``TemperatureOutOfBand`` if
            the heater is not within ``temperature_tolerance_c`` of the
            setpoint, which the API surfaces as HTTP 412. Set False only
            for emergency overrides (e.g. calibration at room
            temperature) — running with this off restores the failure
            mode where sealing below setpoint produces an underspec'd
            seal and downstream pneumatic faults.
        enforce_stage_interlock:
            Layer-1 interlock (v1.3.0). When True (default),
            ``start_cycle`` raises ``StageNotLoaded`` if the stage is
            not in the loaded position, which the API surfaces as
            HTTP 412 with ``{"detail":"Stage not loaded","stage_state":
            "out"|"unknown","required":"in"}``. Independent of
            ``enforce_temp_interlock``. Set False only for emergency
            overrides — running with this off restores the failure
            mode where seal cycles run with the carriage extended,
            wasting hot air and risking film damage.
        """
        self.dry_run = dry_run
        self._driver_factory = driver_factory
        self._driver: Any | None = None
        self._lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_error: ErrorInfo | None = None
        self._busy_state: bool = False
        self._connect_profile: str | None = None
        # Stage position is command-tracked (the COM API has no
        # GetStagePosition equivalent). Defaults to "unknown" at process
        # start; the operator homes it via /control/stage/{in,out}. See
        # README "Stage interlock" for the full transition table.
        self._stage_state: str = "unknown"
        self.enforce_claims = enforce_claims
        self.enforce_temp_interlock = enforce_temp_interlock
        self.enforce_stage_interlock = enforce_stage_interlock
        self.claims = ClaimStore()

        # Tolerance (in C) inside which `actual_temperature` is considered
        # to have reached `setpoint_temperature`. Mirrors what `demo.py`
        # uses for its temperature-wait loop, so the device speaks the
        # same language as the operator-facing demo. The ActiveX control
        # does not expose a native "temperature stable" signal; we
        # synthesize it by comparing the two metrics.
        self._temp_tolerance_c: float = float(
            _config.get("film", "temperature_tolerance_c", 2)
        )

        # Identity (configurable so a deployment can override).
        self.equipment_id: str = _config.get("dashboard", "equipment_id", "plateloc")
        self.equipment_name: str = _config.get(
            "dashboard", "equipment_name", "Agilent PlateLoc"
        )
        self.equipment_kind = "plate_sealer"
        self.equipment_version: str | None = _config.get(
            "dashboard", "equipment_version", None
        )

    # ---- lifecycle ---------------------------------------------------------

    def _create_driver(self) -> Any:
        if self._driver_factory is not None:
            return self._driver_factory()
        if self.dry_run:
            return _StubPlateLoc()
        # Imported lazily so non-Windows hosts can run the dry-run service
        # without pywin32 installed.
        from .plateloc import PlateLoc

        return PlateLoc()

    async def startup(self, profile: str | None = None) -> None:
        """Create (or reuse) the driver and connect.

        On failure, leaves the service in `requires_init` and re-raises
        so callers (lifespan / `/control/startup`) can decide whether to
        log-and-continue or surface a 503.

        Does NOT clear ``self._last_error`` on success: the API layer
        owns that policy and clears only when the overall endpoint
        response is 2xx (see :meth:`clear_last_error_on_success`).
        """
        async with self._lock:
            if self._driver is not None and self._driver_connected():
                return
            self._driver = self._create_driver()
            self._connect_profile = profile
            try:
                await asyncio.to_thread(self._driver.connect, profile)
            except Exception as exc:
                # `connect()` already calls get_last_error() and folds the
                # detail into the exception text on the Initialize-failed
                # path, but other startup failures (driver-create, ATL
                # hosting, surrogate exit) won't carry that string. Best-
                # effort enrich here too.
                detail = await self._read_driver_last_error()
                self._record_error(exc, "startup", detail=detail)
                # keep self._driver around so retries reuse the same instance
                raise

    async def shutdown(self) -> None:
        """Best-effort disconnect. Never raises.

        Resets ``_stage_state`` to ``"unknown"`` — the next operator
        cycle must re-home the carriage. Does NOT clear
        ``self._last_error`` (the API layer owns that policy; see
        :meth:`clear_last_error_on_success`).
        """
        async with self._lock:
            if self._driver is None:
                # Even on the no-op path, stage state should reflect
                # "we cannot vouch for the carriage position" — same
                # rationale as a fresh process start.
                self._stage_state = "unknown"
                return
            try:
                await asyncio.to_thread(self._driver.close)
            except Exception:
                logger.exception("Error while closing driver")
            finally:
                self._driver = None
                self._busy_state = False
                self._stage_state = "unknown"

    # ---- control -----------------------------------------------------------

    async def set_sealing_temperature(self, t: int) -> None:
        await self._do(
            "set_sealing_temperature",
            lambda d: d.set_sealing_temperature(int(t)),
        )

    async def set_sealing_time(self, s: float) -> None:
        await self._do(
            "set_sealing_time",
            lambda d: d.set_sealing_time(float(s)),
        )

    async def start_cycle(self) -> None:
        """Start a seal cycle.

        Two layer-1 interlocks fire before the hardware moves:

        1. **Stage** — raises :class:`StageNotLoaded` unless
           ``components.stage.state == "in"``. Checked first because
           a wrong-stage refusal is faster for the operator to fix
           (a single click) than a temperature ramp.
        2. **Temperature** — raises :class:`TemperatureOutOfBand` if
           the heater is outside ``temperature_tolerance_c`` of the
           setpoint.

        Both pre-flight checks leave hardware state untouched, so
        ``_stage_state`` is unchanged on either refusal path.

        On the COM path: the cycle physically commits the stage to
        ``"in"`` (the carriage is under the press at the start and
        stays there at the end). We pessimize to ``"unknown"`` on
        entry so a mid-cycle failure (driver fault after the
        physical commit started) leaves a truthful state instead of
        a stale ``"in"``.
        """
        # Inlined (not via ``_do``) so the precondition checks and the
        # ``StartCycle`` COM call sit inside a single critical section.
        async with self._lock:
            if self._driver is None or not self._driver_connected():
                raise RuntimeError(
                    "PlateLoc is not connected. POST /control/startup first."
                )
            # Stage gate first (cheap; in-memory). Temperature gate
            # second (requires async COM reads). allowed_actions is
            # built from the same two helpers, so the surfaces agree.
            self._assert_stage_loaded()
            await self._assert_temperature_in_band()
            # Both pre-flights passed; from here on the COM call may
            # mutate physical stage position. Pessimize.
            self._stage_state = "unknown"
            try:
                await asyncio.to_thread(self._driver.start_cycle)
            except Exception as exc:
                detail = await self._read_driver_last_error()
                self._record_error(exc, "start_cycle", detail=detail)
                # Leave _stage_state as "unknown" — the cycle aborted
                # mid-motion and the carriage position is no longer
                # tracked.
                raise
            self._stage_state = "in"
        self._busy_state = True

    def evaluate_temperature_interlock(
        self,
        actual: float | None,
        setpoint: float | None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Single source of truth for the layer-1 temperature interlock.

        Returns ``(should_block, body_for_412)``:

        * ``should_block`` is ``False`` when the heater is at setpoint
          within ``self._temp_tolerance_c`` (the seal cycle would be
          honoured) **or** when ``enforce_temp_interlock`` is disabled.
          In both cases ``body_for_412`` is ``None``.
        * ``should_block`` is ``True`` when the band check fails or
          when the temperatures cannot be read (fail-closed). The
          returned ``body_for_412`` is the structured JSON body that
          ``/control/seal/start`` returns with HTTP 412 — building it
          here keeps the ``/status`` ``allowed_actions`` gate and the
          412 refusal path on the same single answer.
        """
        if not self.enforce_temp_interlock:
            return False, None

        tolerance = self._temp_tolerance_c

        if actual is None or setpoint is None:
            return True, {
                "detail": "Cannot verify temperature: actual or setpoint unavailable",
                "actual_c": actual,
                "setpoint_c": setpoint,
                "tolerance_c": tolerance,
                "retry_after_s": None,
            }

        delta = actual - setpoint
        if abs(delta) <= tolerance:
            return False, None

        # Best-effort retry estimate. PlateLoc heat-up is faster than
        # cool-down on the hot-plate; the constants below are
        # conservative averages, not measured ramps, and intentionally
        # round up so callers don't hot-poll. None is also a valid
        # answer; we always provide a number here so the dashboard can
        # render "try again in ~N s".
        excess = abs(delta) - tolerance
        ramp_c_per_s = 1.0 if delta < 0 else 0.3
        retry_after_s = max(1.0, round(excess / ramp_c_per_s + 0.5))

        return True, {
            "detail": "Temperature outside seal band",
            "actual_c": actual,
            "setpoint_c": setpoint,
            "tolerance_c": tolerance,
            "retry_after_s": retry_after_s,
        }

    def evaluate_stage_interlock(
        self,
        stage_state: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Single source of truth for the stage-position interlock.

        Returns ``(should_block, body_for_412)``:

        * ``should_block`` is ``False`` when ``stage_state == "in"`` —
          the stage is loaded and a seal cycle can proceed — or when
          ``enforce_stage_interlock`` is disabled. In both cases
          ``body_for_412`` is ``None``.
        * ``should_block`` is ``True`` when ``stage_state`` is
          ``"out"`` or ``"unknown"``. The returned ``body_for_412``
          is the structured JSON body that ``/control/seal/start``
          returns with HTTP 412. No ``Retry-After`` — recovery is
          operator-driven (``POST /control/stage/in``), not
          time-based.

        Mirrors :meth:`evaluate_temperature_interlock` so the two
        interlocks compose uniformly at the seal.start endpoint and
        the /status allowed_actions builder.
        """
        if not self.enforce_stage_interlock:
            return False, None
        if stage_state == "in":
            return False, None
        return True, {
            "detail": "Stage not loaded",
            "stage_state": stage_state,
            "required": "in",
        }

    def _assert_stage_loaded(self) -> None:
        """Raise :class:`StageNotLoaded` if the stage interlock would
        block a seal cycle right now.

        Caller MUST already hold ``self._lock``. Synchronous because
        stage state is in-memory — no driver I/O needed.
        """
        blocks, body = self.evaluate_stage_interlock(self._stage_state)
        if not blocks:
            return
        assert body is not None  # invariant: blocks=True implies a body
        raise StageNotLoaded(body["detail"], stage_state=body["stage_state"])

    async def _assert_temperature_in_band(self) -> None:
        """Raise :class:`TemperatureOutOfBand` if the temperature
        interlock would block a seal cycle right now.

        Caller MUST already hold ``self._lock``. Uses ``asyncio.to_thread``
        for the (blocking) COM reads so the event loop is not pinned
        while the surrogate replies. The decision itself is delegated
        to :meth:`evaluate_temperature_interlock` so this path stays in
        lockstep with the ``/status`` ``allowed_actions`` gate.
        """
        actual_raw = await asyncio.to_thread(self._driver.get_actual_temperature)
        setpoint_raw = await asyncio.to_thread(self._driver.get_sealing_temperature)

        blocks, body = self.evaluate_temperature_interlock(
            _coerce_float(actual_raw), _coerce_float(setpoint_raw)
        )
        if not blocks:
            return
        assert body is not None  # invariant: blocks=True implies a body
        raise TemperatureOutOfBand(
            body["detail"],
            actual_c=body["actual_c"],
            setpoint_c=body["setpoint_c"],
            tolerance_c=body["tolerance_c"],
            retry_after_s=body["retry_after_s"],
        )

    async def stop_cycle(self) -> None:
        await self._do("stop_cycle", lambda d: d.stop_cycle())
        self._busy_state = False

    async def move_stage_in(self) -> None:
        """Move the plate carriage to the loaded position.

        Tracks ``_stage_state`` per the v1.3.0 transition table.
        Inlined (not via :meth:`_do`) so the state mutations and the
        COM call sit inside the same critical section.
        """
        await self._move_stage("move_stage_in", "in")

    async def move_stage_out(self) -> None:
        """Move the plate carriage to the unloaded position.

        Tracks ``_stage_state`` per the v1.3.0 transition table.
        """
        await self._move_stage("move_stage_out", "out")

    async def _move_stage(self, com_method: str, target: str) -> None:
        """Shared implementation for ``move_stage_in`` and
        ``move_stage_out``. Pessimizes ``_stage_state`` on entry and
        commits to ``target`` only on a clean COM return.

        A POST /control/stage/{in,out} to the position the stage is
        already in is handled as a no-op 200 by the COM driver. The
        net state remains ``target``; a /status poll mid-call cannot
        observe the "unknown" flicker because /status takes the same
        lock.
        """
        async with self._lock:
            if self._driver is None or not self._driver_connected():
                raise RuntimeError(
                    "PlateLoc is not connected. POST /control/startup first."
                )
            self._stage_state = "unknown"
            try:
                await asyncio.to_thread(getattr(self._driver, com_method))
            except Exception as exc:
                detail = await self._read_driver_last_error()
                self._record_error(exc, com_method, detail=detail)
                # Leave _stage_state as "unknown" — the move failed
                # mid-motion and we no longer know where the carriage is.
                raise
            self._stage_state = target

    async def _do(self, name: str, fn: Callable[[Any], Any]) -> None:
        async with self._lock:
            if self._driver is None or not self._driver_connected():
                raise RuntimeError(
                    "PlateLoc is not connected. POST /control/startup first."
                )
            try:
                await asyncio.to_thread(fn, self._driver)
            except Exception as exc:
                # Best-effort: pull the human-readable message out of the
                # ActiveX control via GetLastError so operators see what
                # the instrument actually reported instead of just the
                # generic Agilent HRESULT (e.g. -2147221503 = 0x80040201).
                detail = await self._read_driver_last_error()
                self._record_error(exc, name, detail=detail)
                raise

    async def _read_driver_last_error(self) -> str | None:
        """Return ``driver.get_last_error()`` if the driver has it, else None.

        Wrapped so the error-handling path can never itself crash on a
        broken / stub driver. Called from inside ``self._lock``.
        """
        driver = self._driver
        if driver is None:
            return None
        getter = getattr(driver, "get_last_error", None)
        if getter is None:
            return None
        try:
            result = await asyncio.to_thread(getter)
        except Exception:
            return None
        if result is None:
            return None
        text = str(result).strip()
        return text or None

    # ---- status (side-effect-free) ----------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot. MUST NOT mutate hardware state.

        The spec requires this endpoint to be safe to call every 2-3
        seconds and to always return HTTP 200 unless the process itself
        is broken. We therefore catch every per-getter failure and fold
        it into ``equipment_status: degraded`` rather than raising.

        v1.1 fields (``allowed_actions``, ``details.claimed_by``) are
        attached *after* the COM lock is released; the claim store has
        its own (cheap) async lock and we want polling status to never
        block behind long-running control operations.
        """
        async with self._lock:
            status = self._build_status()
        # NB: ``self.claims.current()`` is its own async coroutine that
        # takes the claim store's internal lock. Calling it outside the
        # COM lock means a slow seal cycle does not stall /status polling.
        claimed_by = await self.claims.current()
        if claimed_by is not None:
            status.details["claimed_by"] = claimed_by.model_dump(mode="json")
        return status

    def _build_status(self) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = socket.gethostname()

        # ---- not connected: requires_init --------------------------------
        if self._driver is None or not self._driver_connected():
            return EquipmentStatus(
                protocol_version=PROTOCOL_VERSION,
                equipment_id=self.equipment_id,
                equipment_name=self.equipment_name,
                equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
                equipment_version=self.equipment_version,
                host=host,
                equipment_status="requires_init",
                message="Driver not connected. POST /control/startup to initialize.",
                required_actions=["startup"],
                allowed_actions=list(_ALLOWED_ACTIONS_BY_STATE["requires_init"]),
                device_time=now,
                uptime_seconds=uptime,
                last_error=self._last_error,
            )

        # ---- read what we can; never let a single getter fail status -----
        metrics: dict[str, MetricValue] = {}
        details: dict[str, Any] = {}
        readback_errors: list[str] = []

        def _read(label: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:
                readback_errors.append(f"{label}: {exc}")
                return None

        actual_temp = _read("actual_temperature", self._driver.get_actual_temperature)
        if actual_temp is not None:
            metrics["actual_temperature"] = MetricValue(
                value=actual_temp, unit="C", timestamp=now
            )
        setpoint = _read("setpoint_temperature", self._driver.get_sealing_temperature)
        if setpoint is not None:
            metrics["setpoint_temperature"] = MetricValue(
                value=setpoint, unit="C", timestamp=now
            )

        # Synthesized: signed delta and heater state. The ActiveX has no
        # native "ready to seal" signal so the device computes it from
        # the two raw metrics using the operator-facing tolerance. delta
        # is `actual - setpoint`, so a negative value means "still heating
        # up", positive means "above setpoint / cooling".
        actual_f = _coerce_float(actual_temp)
        setpoint_f = _coerce_float(setpoint)
        heater_temp_delta: float | None
        if actual_f is not None and setpoint_f is not None:
            heater_temp_delta = actual_f - setpoint_f
        else:
            heater_temp_delta = None
        if heater_temp_delta is not None:
            metrics["temperature_delta_c"] = MetricValue(
                value=round(heater_temp_delta, 1), unit="C", timestamp=now
            )
        seal_time = _read("sealing_time", self._driver.get_sealing_time)
        if seal_time is not None:
            metrics["sealing_time"] = MetricValue(
                value=seal_time, unit="s", timestamp=now
            )
        cycle_count = _read("cycle_count", self._driver.get_cycle_count)
        if cycle_count is not None:
            metrics["cycle_count"] = MetricValue(value=cycle_count, unit="count")

        firmware = _read("firmware_version", self._driver.get_firmware_version)
        if firmware:
            details["firmware_version"] = firmware
        ax_version = _read("activex_version", self._driver.get_version)
        if ax_version:
            details["activex_version"] = ax_version
        if self._connect_profile:
            details["profile"] = self._connect_profile
        com_port = getattr(self._driver, "com_port", None)
        if com_port:
            details["com_port"] = com_port

        # ---- components --------------------------------------------------
        connected = self._driver_connected()
        sealer_state = (
            "busy" if self._busy_state else ("idle" if connected else "disconnected")
        )

        # Heater state is derived from the temperature delta against the
        # configured tolerance. "stable" means the plate is at setpoint
        # within +/- temperature_tolerance_c and a seal cycle would seal
        # at the requested temperature. "heating"/"cooling" mean it is
        # not yet there. "unknown" covers the case where one of the
        # readings could not be obtained.
        if not connected:
            heater_state = "disconnected"
            heater_message: str | None = None
        elif heater_temp_delta is None:
            heater_state = "unknown"
            heater_message = None
        elif abs(heater_temp_delta) <= self._temp_tolerance_c:
            heater_state = "stable"
            heater_message = f"At setpoint ({actual_temp} C)"
        elif heater_temp_delta < 0:
            heater_state = "heating"
            heater_message = f"Heating {actual_temp} -> {setpoint} C"
        else:
            heater_state = "cooling"
            heater_message = f"Cooling {actual_temp} -> {setpoint} C"

        # Stage state is command-tracked (v1.3.0). On a disconnected
        # driver we cannot vouch for the carriage so we override to
        # "unknown" regardless of the last commanded position — the
        # next /control/startup leaves it "unknown" until the operator
        # explicitly homes.
        stage_component_state = self._stage_state if connected else "unknown"

        components: dict[str, ComponentStatus] = {
            "sealer": ComponentStatus(
                connected=connected,
                state=sealer_state,
            ),
            "heater": ComponentStatus(
                connected=connected,
                state=heater_state,
                message=heater_message,
                # last_event_at is intentionally None: it should be the
                # time of the last *transition* (e.g. heating -> stable),
                # not the poll timestamp. Wire that up when we have a
                # transition tracker.
            ),
            "stage": ComponentStatus(
                connected=connected, state=stage_component_state
            ),
        }

        # Tell readers what tolerance defines "stable" so a dashboard or
        # workflow can render the delta meaningfully without guessing.
        details["temperature_tolerance_c"] = self._temp_tolerance_c

        # ---- top-level equipment_status ----------------------------------
        if self.dry_run:
            state: str = "dry_run"
            message: str | None = "Dry-run mode - no hardware connected"
            details["dry_run"] = True
        elif self._busy_state:
            state = "busy"
            message = "Seal cycle in progress"
        elif self._last_error is not None and (
            (now - self._last_error.timestamp).total_seconds()
            < _RECENT_ERROR_WINDOW_S
        ):
            state = "error"
            message = self._last_error.message
        elif readback_errors:
            state = "degraded"
            message = "; ".join(readback_errors)
        else:
            state = "ready"
            message = "Idle, ready to seal"

        # ---- allowed_actions ---------------------------------------------
        # Start from the state-derived defaults, then layer the v1.2.1
        # temperature interlock and the v1.3.0 stage interlock on top.
        # Both gates consult the SAME helpers the /control/seal/start
        # 412 path uses, so a workflow client trusting allowed_actions
        # verbatim cannot round-trip into a 412 the device would have
        # refused.
        allowed_actions = list(_ALLOWED_ACTIONS_BY_STATE.get(state, []))
        if state in ("ready", "dry_run"):
            if "seal.start" in allowed_actions:
                stage_blocks, _ = self.evaluate_stage_interlock(
                    stage_component_state
                )
                temp_blocks, _ = self.evaluate_temperature_interlock(
                    actual_f, setpoint_f
                )
                if stage_blocks or temp_blocks:
                    allowed_actions.remove("seal.start")
            # Stage move dedup: don't advertise the no-op direction.
            # A POST to the "already there" direction is still accepted
            # (the device treats it as a 200 no-op); we just leave it
            # out of the advertised list so an operator UI doesn't show
            # a redundant button.
            if stage_component_state == "in" and "stage.in" in allowed_actions:
                allowed_actions.remove("stage.in")
            if stage_component_state == "out" and "stage.out" in allowed_actions:
                allowed_actions.remove("stage.out")

        return EquipmentStatus(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=self.equipment_id,
            equipment_name=self.equipment_name,
            equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
            equipment_version=self.equipment_version,
            host=host,
            equipment_status=state,  # type: ignore[arg-type]
            message=message,
            allowed_actions=allowed_actions,
            device_time=now,
            uptime_seconds=uptime,
            components=components,
            metrics=metrics,
            last_error=self._last_error,
            details=details,
        )

    # ---- helpers -----------------------------------------------------------

    def _driver_connected(self) -> bool:
        """Driver is connected if either flag is set. The real PlateLoc
        uses the private `_connected` attribute; the stub also exposes it
        for parity. Wrapped in getattr so an unexpected driver type
        cannot crash the status endpoint."""
        if self._driver is None:
            return False
        return bool(getattr(self._driver, "_connected", False))

    def clear_last_error_on_success(self) -> None:
        """Drop ``self._last_error`` after a 2xx operational response.

        Policy (single source of truth — see README "Safety interlocks"):

        * Called by every operational ``/control/*`` endpoint right
          before it returns a 2xx response (startup, shutdown,
          seal.start, seal.stop, seal.set_temperature, seal.set_time,
          stage.in, stage.out). Doing this at the API layer — not
          inside the service methods — means a refusal mid-endpoint
          (e.g. ``set_sealing_time`` succeeds then ``start_cycle``
          raises 412) does NOT clear: only an *overall* 2xx clears.
        * NOT called on 4xx / 5xx responses. A 412 from the temperature
          interlock is a refusal, not a recovery; ``last_error`` keeps
          its relevance.
        * NOT called by ``/control/claim``, ``/control/heartbeat``, or
          ``/control/release``: those are claim infrastructure, not
          operational progress, and clearing on them would hide
          ``last_error`` during a heartbeat-only retry loop.
        * NOT called by ``/status``, ``/``, or ``/health`` — those are
          read-only and must not mutate state.

        Concurrency: attribute assignment is atomic in CPython, so
        this method does NOT take ``self._lock``. A concurrent
        ``/status`` poll between the lock release and this clear sees
        the old value, which is the same staleness already inherent in
        polling.
        """
        self._last_error = None

    def _record_error(
        self, exc: Exception, code: str, *, detail: str | None = None
    ) -> None:
        message = str(exc)
        if detail and detail not in message:
            # The driver-reported text is what makes 0x80040201 actionable
            # ("Could not initialize - No response from PlateLoc",
            # "Stage cannot move - press is down", etc.). Append it once.
            message = f"{message} (driver: {detail})"
        self._last_error = ErrorInfo(
            code=code,
            message=message,
            severity="error",
            timestamp=datetime.now(timezone.utc),
        )
        if detail:
            logger.exception("PlateLoc error in %s (driver: %s)", code, detail)
        else:
            logger.exception("PlateLoc error in %s", code)


__all__ = [
    "PlateLocService",
    "StageNotLoaded",
    "TemperatureOutOfBand",
    "_StubPlateLoc",
]
