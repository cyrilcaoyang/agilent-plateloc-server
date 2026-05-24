"""Conformance tests for the lab equipment status spec v1.1.

These tests run with the dry-run stub driver so they require no Windows
/ ActiveX dependencies and can be executed in CI on any platform.

The default ``client`` fixture (see ``conftest.py``) is built with
``enforce_claims=True`` and pre-acquires a claim, so v1.0-era control
tests keep working unchanged. v1.1-specific surface (claim protocol,
``allowed_actions``, ``details.claimed_by``, 423 enforcement) is
covered here and in ``test_claims.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agilent_plateloc.models import PROTOCOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Spec endpoints
# ---------------------------------------------------------------------------


def test_probe(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_id"] == "plateloc"
    assert body["equipment_name"] == "Agilent PlateLoc"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["protocol_version"] == "1.1"


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_openapi_doc(client: TestClient) -> None:
    """FastAPI auto-publishes /openapi.json - the spec requires it.

    We assert that the v1.0 + v1.1 schemas are in the doc so an
    ``openapi-typescript`` consumer (e.g. the dashboard frontend) can
    pull types straight from this device.
    """
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schemas = r.json()["components"]["schemas"]
    for required in [
        # v1.0 envelope
        "EquipmentStatus",
        "ProbeResponse",
        "HealthResponse",
        "ComponentStatus",
        "MetricValue",
        "ErrorInfo",
        # v1.1 claim protocol
        "ClaimRequest",
        "ClaimResponse",
        "ClaimRejection",
        "ClaimedBy",
    ]:
        assert required in schemas, f"OpenAPI doc is missing {required}"


def test_status_envelope(client: TestClient) -> None:
    """Spec-required fields exist and have the correct types/shape."""
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()

    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["equipment_id"] == "plateloc"
    assert body["equipment_kind"] == "plate_sealer"
    assert body["equipment_status"] == "dry_run"
    assert isinstance(body["device_time"], str)
    assert isinstance(body["uptime_seconds"], (int, float))

    # v1.1: allowed_actions is a top-level list of skill names.
    assert isinstance(body["allowed_actions"], list)
    assert "seal.start" in body["allowed_actions"]
    assert "shutdown" in body["allowed_actions"]

    # Metrics are populated from the stub driver.
    metrics = body["metrics"]
    assert metrics["actual_temperature"]["unit"] == "C"
    assert metrics["setpoint_temperature"]["unit"] == "C"
    assert metrics["sealing_time"]["unit"] == "s"
    assert metrics["cycle_count"]["unit"] == "count"

    # Components.
    assert "sealer" in body["components"]
    assert "stage" in body["components"]


def test_status_is_side_effect_free(client: TestClient) -> None:
    """Spec rule #1: GET /status MUST be side-effect-free.

    Polling repeatedly must not increment the cycle counter or otherwise
    mutate state.
    """
    r1 = client.get("/status")
    cc1 = r1.json()["metrics"]["cycle_count"]["value"]
    for _ in range(10):
        client.get("/status")
    r2 = client.get("/status")
    cc2 = r2.json()["metrics"]["cycle_count"]["value"]
    assert cc1 == cc2 == 0


def test_status_always_200_when_disconnected(unclaimed_client: TestClient) -> None:
    """Spec rule #2: /status returns 200 even if hardware isn't ready.

    We force a disconnect by claiming + calling /control/shutdown, then
    verify the response is HTTP 200 with `equipment_status: requires_init`.
    """
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "pytest", "session_id": "shutdown-test", "ttl_s": 30},
    )
    token = r.json()["claim_token"]
    unclaimed_client.post("/control/shutdown", headers={"X-Claim-Token": token})
    r = unclaimed_client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "requires_init"
    assert "startup" in body["required_actions"]
    # In requires_init the only action the device will honour is startup.
    assert body["allowed_actions"] == ["startup"]


# ---------------------------------------------------------------------------
# v1.1 allowed_actions semantics
# ---------------------------------------------------------------------------


def test_allowed_actions_changes_with_state(client: TestClient) -> None:
    """allowed_actions must reflect current equipment_status.

    Walks the dry-run state machine: dry_run starts with the full set
    (because dry_run is by definition able to honour everything), then
    after explicit shutdown we switch to requires_init -> startup-only.

    v1.3.0: the ``client`` fixture homes the stage to ``"in"`` as part
    of setup, so ``stage.in`` is dedup'd (no-op direction).
    ``stage.out`` remains because it's a genuine state change.
    """
    body = client.get("/status").json()
    assert body["equipment_status"] == "dry_run"
    assert "seal.start" in body["allowed_actions"]
    assert "stage.out" in body["allowed_actions"]
    assert "stage.in" not in body["allowed_actions"]  # dedup'd at stage=in

    client.post("/control/shutdown")
    body = client.get("/status").json()
    assert body["equipment_status"] == "requires_init"
    assert body["allowed_actions"] == ["startup"]


def test_allowed_actions_ready_state() -> None:
    """ready (real driver, not dry_run) exposes the full operating set
    minus seal.stop (which only makes sense while busy).

    v1.3.0: after homing the stage to ``"in"``, ``stage.in`` is
    dedup'd out of allowed_actions because it would be a no-op.
    """
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    app = create_app(dry_run=False, enforce_claims=True)
    app.state.service._driver_factory = _StubPlateLoc
    with TestClient(app) as alt:
        r = alt.post(
            "/control/claim",
            json={"owner": "pytest", "session_id": "ready-test", "ttl_s": 60},
        )
        token = r.json()["claim_token"]
        alt.headers["X-Claim-Token"] = token

        alt.post("/control/startup", json={})
        alt.post("/control/stage/in")
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "in"
        actions = set(body["allowed_actions"])
        # Stage is already "in", so stage.in is dedup'd; stage.out
        # remains because it's a genuine state change away from "in".
        assert {"seal.start", "stage.out", "shutdown"} <= actions
        assert "stage.in" not in actions
        assert "seal.stop" not in actions  # nothing to stop yet


def test_allowed_actions_busy_state() -> None:
    """busy advertises seal.stop and shutdown (and nothing that would
    conflict with an in-flight cycle)."""
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    app = create_app(dry_run=False, enforce_claims=True)
    app.state.service._driver_factory = _StubPlateLoc
    with TestClient(app) as alt:
        r = alt.post(
            "/control/claim",
            json={"owner": "pytest", "session_id": "busy-test", "ttl_s": 60},
        )
        alt.headers["X-Claim-Token"] = r.json()["claim_token"]

        alt.post("/control/startup", json={})
        alt.post("/control/stage/in")
        alt.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        body = alt.get("/status").json()
        assert body["equipment_status"] == "busy"
        actions = set(body["allowed_actions"])
        assert "seal.stop" in actions
        assert "shutdown" in actions
        assert "seal.start" not in actions  # already running


# ---------------------------------------------------------------------------
# Control endpoints (existing v1.0 behaviour, now under a held claim)
# ---------------------------------------------------------------------------


def test_set_temperature_validation(client: TestClient) -> None:
    """Out-of-range values are rejected before they reach the driver."""
    r = client.post("/control/seal/temperature", json={"temperature_c": 500})
    assert r.status_code == 422


def test_seal_cycle_round_trip(client: TestClient) -> None:
    """Start a cycle, then stop it, and confirm the cycle counter
    incremented exactly once."""
    before = client.get("/status").json()["metrics"]["cycle_count"]["value"]

    r = client.post(
        "/control/seal/start", json={"temperature_c": 170, "seconds": 3.0}
    )
    assert r.status_code == 200

    r = client.post("/control/seal/stop")
    assert r.status_code == 200

    after = client.get("/status").json()["metrics"]["cycle_count"]["value"]
    assert after == before + 1


def test_temperature_setpoint_persists(client: TestClient) -> None:
    """A temperature set via /control/seal/temperature is visible in
    the next /status response."""
    r = client.post("/control/seal/temperature", json={"temperature_c": 145})
    assert r.status_code == 200
    body = client.get("/status").json()
    assert body["metrics"]["setpoint_temperature"]["value"] == 145


# ---------------------------------------------------------------------------
# Layer-1 temperature interlock (see docs/INTERLOCKS.md in ac-organic-lab)
# ---------------------------------------------------------------------------


def _build_claimed_client(
    *,
    enforce_temp_interlock: bool,
    enforce_stage_interlock: bool = True,
    home_stage: bool = True,
) -> tuple:
    """Spin up a service with the dry-run stub injected (so the
    operational state machine runs, not the dry_run shortcut), acquire
    a claim, and return ``(client, driver)`` so tests can mutate the
    stub's temperatures directly.

    Parameters
    ----------
    enforce_temp_interlock:
        Pass through to ``create_app``.
    enforce_stage_interlock:
        Pass through to ``create_app``. Default ``True``.
    home_stage:
        When True (default) the helper issues ``/control/stage/in``
        after startup so tests of the temperature interlock can issue
        ``/control/seal/start`` without bumping into the stage
        interlock. Tests of the stage interlock itself pass
        ``home_stage=False`` so they can drive the carriage explicitly.
    """
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    app = create_app(
        dry_run=False,
        enforce_claims=True,
        enforce_temp_interlock=enforce_temp_interlock,
        enforce_stage_interlock=enforce_stage_interlock,
    )
    app.state.service._driver_factory = _StubPlateLoc
    c = TestClient(app)
    c.__enter__()
    r = c.post(
        "/control/claim",
        json={"owner": "pytest", "session_id": "interlock-test", "ttl_s": 60},
    )
    c.headers["X-Claim-Token"] = r.json()["claim_token"]
    c.post("/control/startup", json={})
    if home_stage:
        c.post("/control/stage/in")
    driver = app.state.service._driver
    return c, driver


def test_seal_start_in_band_succeeds() -> None:
    """Heater at setpoint -> cycle accepted with HTTP 200."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        # Stub snaps actual to setpoint on set_sealing_temperature, so
        # we are at-setpoint by construction.
        r = c.post(
            "/control/seal/start",
            json={"temperature_c": 170, "seconds": 3.0},
        )
        assert r.status_code == 200, r.text
    finally:
        c.__exit__(None, None, None)


def test_seal_start_out_of_band_returns_412() -> None:
    """Heater below setpoint -> cycle refused with HTTP 412 and a
    structured body."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        # Set the setpoint, then drag the stub's actual temperature
        # well below the band the stub would otherwise report.
        c.post("/control/seal/temperature", json={"temperature_c": 170})
        driver._actual_temp = 150  # 20 C below setpoint, tolerance is 2

        r = c.post(
            "/control/seal/start",
            json={"seconds": 3.0},  # no temperature_c -> keep setpoint
        )
        assert r.status_code == 412, r.text
        body = r.json()
        assert body["detail"] == "Temperature outside seal band"
        assert body["actual_c"] == 150.0
        assert body["setpoint_c"] == 170.0
        assert body["tolerance_c"] == 2.0
        assert body["retry_after_s"] is not None
        assert body["retry_after_s"] > 0
        # Retry-After header should mirror the body field.
        assert r.headers.get("Retry-After") == str(int(body["retry_after_s"]))
    finally:
        c.__exit__(None, None, None)


def test_seal_start_out_of_band_passes_when_interlock_disabled() -> None:
    """With ``enforce_temp_interlock=False`` the device restores the
    pre-interlock behavior: out-of-band seal cycles are accepted. This
    is the emergency-override path; production stays True."""
    c, driver = _build_claimed_client(enforce_temp_interlock=False)
    try:
        c.post("/control/seal/temperature", json={"temperature_c": 170})
        driver._actual_temp = 150

        r = c.post(
            "/control/seal/start",
            json={"seconds": 3.0},
        )
        assert r.status_code == 200, r.text
    finally:
        c.__exit__(None, None, None)


def test_seal_start_when_temperature_unreadable_returns_412() -> None:
    """If actual or setpoint cannot be read, the interlock refuses
    rather than guessing the device is safe to seal."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        # Force the stub to misreport actual as unreadable. Patching
        # the method is cleaner than subclassing the stub for one test.
        driver.get_actual_temperature = lambda: None  # type: ignore[method-assign]

        r = c.post(
            "/control/seal/start",
            json={"temperature_c": 170, "seconds": 3.0},
        )
        assert r.status_code == 412, r.text
        body = r.json()
        assert "Cannot verify temperature" in body["detail"]
        assert body["actual_c"] is None
        assert body["retry_after_s"] is None
        assert "Retry-After" not in r.headers
    finally:
        c.__exit__(None, None, None)


def test_allowed_actions_drops_seal_start_when_out_of_band() -> None:
    """v1.2.1: ``/status`` must drop ``seal.start`` from
    ``allowed_actions`` when the temperature interlock would refuse
    a POST.

    Defends the v0.4 workflow path where SDK clients consume
    ``allowed_actions`` verbatim — without this gate they would still
    attempt the call and eat a 412.
    """
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        c.post("/control/seal/temperature", json={"temperature_c": 170})
        driver._actual_temp = 150  # 20 C below setpoint, tolerance is 2

        body = c.get("/status").json()
        assert "seal.start" not in body["allowed_actions"]
        # Sibling actions still present. _build_claimed_client homed
        # the stage to "in", so v1.3.0 dedups stage.in out; stage.out
        # remains (genuine state change).
        assert "stage.out" in body["allowed_actions"]
        assert "stage.in" not in body["allowed_actions"]
        assert "shutdown" in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_allowed_actions_drops_seal_start_when_heater_heating() -> None:
    """heater.state == 'heating' implies the band check fails.
    The two surfaces (heater state + allowed_actions) must agree."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        c.post("/control/seal/temperature", json={"temperature_c": 170})
        driver._actual_temp = 100  # well below setpoint

        body = c.get("/status").json()
        assert body["components"]["heater"]["state"] == "heating"
        assert "seal.start" not in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_allowed_actions_drops_seal_start_when_unreadable() -> None:
    """Fail-closed: if actual or setpoint cannot be read, seal.start
    is absent from ``allowed_actions`` (matching the 412 path)."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        driver.get_actual_temperature = lambda: None  # type: ignore[method-assign]

        body = c.get("/status").json()
        assert "seal.start" not in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_allowed_actions_keeps_seal_start_when_interlock_disabled() -> None:
    """``enforce_temp_interlock=False`` → seal.start is in
    ``allowed_actions`` whenever the state would otherwise allow it,
    regardless of heater state or band.

    Note: the stage interlock is still active here (the default in
    ``_build_claimed_client`` homes the stage), so seal.start would be
    blocked by stage if we hadn't homed. The point of this test is to
    isolate the *temperature* gate's behaviour with the flag off.
    """
    c, driver = _build_claimed_client(enforce_temp_interlock=False)
    try:
        c.post("/control/seal/temperature", json={"temperature_c": 170})
        driver._actual_temp = 50  # way out of band

        body = c.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert "seal.start" in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_allowed_actions_agrees_with_412_path() -> None:
    """For each blocked scenario, ``/status`` omits ``seal.start`` iff
    a POST to ``/control/seal/start`` returns 412.

    This is the contract that closes the v0.4 gap: the SDK gate
    (advisory) and the device gate (authoritative) must never disagree.
    """
    scenarios = [
        ("heating: actual << setpoint", lambda d: setattr(d, "_actual_temp", 100)),
        ("cooling: actual >> setpoint", lambda d: setattr(d, "_actual_temp", 250)),
        (
            "actual unreadable",
            lambda d: setattr(
                d, "get_actual_temperature", lambda: None  # type: ignore[method-assign]
            ),
        ),
        (
            "setpoint unreadable",
            lambda d: setattr(
                d, "get_sealing_temperature", lambda: None  # type: ignore[method-assign]
            ),
        ),
    ]
    for label, mutate in scenarios:
        c, driver = _build_claimed_client(enforce_temp_interlock=True)
        try:
            c.post("/control/seal/temperature", json={"temperature_c": 170})
            mutate(driver)

            status_omits = "seal.start" not in c.get("/status").json()["allowed_actions"]
            post_refuses = (
                c.post("/control/seal/start", json={"seconds": 3.0}).status_code == 412
            )

            assert status_omits, f"{label}: /status should omit seal.start"
            assert post_refuses, f"{label}: POST should return 412"
            assert status_omits == post_refuses, f"{label}: surfaces disagreed"
        finally:
            c.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# last_error auto-clear on first successful action (v1.2.1)
# ---------------------------------------------------------------------------


def _provoke_error(driver) -> None:  # type: ignore[no-untyped-def]
    """Drive the stub into a recorded ``last_error`` by replacing
    ``move_stage_in`` with a method that raises a non-``RuntimeError``
    exception (mirrors real ActiveX COM faults, which surface as
    ``pywintypes.com_error``, not ``RuntimeError``). The API layer maps
    those to HTTP 500; ``RuntimeError`` would map to 409 instead.
    Returns nothing; the caller asserts ``last_error`` is populated."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("Low Air Pressure (simulated COM fault)")

    driver.move_stage_in = _boom


def test_last_error_clears_on_next_successful_action() -> None:
    """After a control failure, the first successful action that
    follows clears ``last_error`` before the response is built."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        _provoke_error(driver)
        r = c.post("/control/stage/in")
        assert r.status_code == 500
        assert c.get("/status").json()["last_error"] is not None

        # stage.out does not need the failing path; first 2xx → cleared.
        r = c.post("/control/stage/out")
        assert r.status_code == 200, r.text
        assert c.get("/status").json()["last_error"] is None
    finally:
        c.__exit__(None, None, None)


def test_last_error_preserved_on_412_refusal() -> None:
    """A 412 refusal is not a recovery — ``last_error`` must persist.

    Drives the band check fail by mutating the stub directly; avoiding
    a successful ``/control/seal/temperature`` call which would itself
    clear ``last_error`` and mask the bug under test.
    """
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        _provoke_error(driver)
        c.post("/control/stage/in")
        body = c.get("/status").json()
        assert body["last_error"] is not None
        original_message = body["last_error"]["message"]

        # _build_claimed_client startup() already snaps _actual_temp to
        # _set_temp=170; drag actual below band without a 2xx round trip.
        driver._actual_temp = 150
        r = c.post("/control/seal/start", json={"seconds": 3.0})
        assert r.status_code == 412

        body = c.get("/status").json()
        assert body["last_error"] is not None
        assert body["last_error"]["message"] == original_message
    finally:
        c.__exit__(None, None, None)


def test_last_error_preserved_on_heartbeat() -> None:
    """Claim infrastructure must not clear operational error state."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        _provoke_error(driver)
        c.post("/control/stage/in")
        assert c.get("/status").json()["last_error"] is not None

        # heartbeat returns 200 with a fresh ClaimResponse body.
        r = c.post("/control/heartbeat")
        assert r.status_code == 200, r.text

        assert c.get("/status").json()["last_error"] is not None
    finally:
        c.__exit__(None, None, None)


def test_last_error_not_cleared_by_status_get() -> None:
    """Read-only endpoints must not mutate ``last_error``."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        _provoke_error(driver)
        c.post("/control/stage/in")
        assert c.get("/status").json()["last_error"] is not None

        for _ in range(5):
            c.get("/status")
        assert c.get("/status").json()["last_error"] is not None
    finally:
        c.__exit__(None, None, None)


def test_last_error_clears_on_successful_shutdown() -> None:
    """``/control/shutdown`` is an operational endpoint; a 2xx response
    drives the device into a quiescent state and clears the error."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        _provoke_error(driver)
        c.post("/control/stage/in")
        assert c.get("/status").json()["last_error"] is not None

        r = c.post("/control/shutdown")
        assert r.status_code == 200, r.text
        assert c.get("/status").json()["last_error"] is None
    finally:
        c.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# v1.3.0 stage interlock — transition table + 412 path + agreement property
# ---------------------------------------------------------------------------


def test_stage_state_unknown_on_fresh_startup() -> None:
    """AC #1: after startup, before any stage move, state is "unknown"
    and seal.start is absent from allowed_actions."""
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "unknown"
        assert "seal.start" not in body["allowed_actions"]
        # Both stage moves are advertised — operator must home.
        assert "stage.in" in body["allowed_actions"]
        assert "stage.out" in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_stage_out_transition() -> None:
    """AC #2: POST /control/stage/out → 200, state becomes "out"."""
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        r = c.post("/control/stage/out")
        assert r.status_code == 200, r.text
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "out"
        # stage.out dedup'd; stage.in is the operator's next step.
        assert "stage.out" not in body["allowed_actions"]
        assert "stage.in" in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_seal_start_412_when_stage_out() -> None:
    """AC #3: seal.start while stage=out returns 412 with the
    stage-specific body. No Retry-After header. allowed_actions omits
    seal.start. State remains "out" — the pre-flight refusal has no
    side effect."""
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        c.post("/control/stage/out")
        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 412, r.text
        assert r.json() == {
            "detail": "Stage not loaded",
            "stage_state": "out",
            "required": "in",
        }
        assert "Retry-After" not in r.headers
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "out"  # unchanged
        assert "seal.start" not in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_seal_start_412_when_stage_unknown() -> None:
    """Stage interlock fail-closes on "unknown" same as on "out"."""
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 412, r.text
        assert r.json()["stage_state"] == "unknown"
        assert r.json()["required"] == "in"
    finally:
        c.__exit__(None, None, None)


def test_stage_in_then_seal_start_succeeds() -> None:
    """AC #4 + #5: stage/in → 200 → seal.start succeeds → stage stays "in"."""
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        r = c.post("/control/stage/in")
        assert r.status_code == 200
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "in"
        assert "seal.start" in body["allowed_actions"]

        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 200, r.text

        # Cycle leaves the carriage IN — operator must explicitly
        # stage/out afterwards.
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "in"
    finally:
        c.__exit__(None, None, None)


def test_mid_cycle_failure_leaves_stage_unknown() -> None:
    """AC #6: mid-cycle COM fault → last_error populated AND
    stage.state == "unknown" (carriage position no longer tracked)."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        # Replace start_cycle with one that raises mid-call. Both pre-
        # flights (stage in, temp in band) pass, so _stage_state gets
        # pessimized to "unknown" BEFORE the fault, and stays there.
        def _boom(*_a: object, **_kw: object) -> None:
            raise OSError("Simulated mid-cycle fault")

        driver.start_cycle = _boom
        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 500, r.text

        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "unknown"
        assert body["last_error"] is not None
        assert "seal.start" not in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_shutdown_resets_stage_state() -> None:
    """AC #7: /control/shutdown → 200 → stage state resets to "unknown".

    After shutdown the device reports ``requires_init`` with no
    components dict (existing v1.1 contract: with the driver gone we
    don't fabricate component statuses). The operator-visible effect
    is that after the next startup the carriage is "unknown" again
    and must be re-homed — exactly the contract the README documents
    for the "in-memory state, defaults to unknown on restart"
    callout.
    """
    c, _ = _build_claimed_client(enforce_temp_interlock=True)
    try:
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "in"

        r = c.post("/control/shutdown")
        assert r.status_code == 200, r.text

        body = c.get("/status").json()
        assert body["equipment_status"] == "requires_init"
        # components is empty in requires_init by the v1.1 contract;
        # the stage state is preserved in-memory and surfaces after
        # the next startup.
        assert body["components"] == {}

        # Re-startup and observe: state was reset on shutdown, so the
        # carriage is now "unknown" exactly as on a process restart.
        r = c.post("/control/startup", json={})
        assert r.status_code == 200, r.text
        body = c.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "unknown"
        assert "seal.start" not in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_stage_interlock_disabled_allows_seal_regardless_of_stage() -> None:
    """AC #8: enforce_stage_interlock=False → seal.start succeeds and
    is in allowed_actions regardless of stage.state."""
    c, _ = _build_claimed_client(
        enforce_temp_interlock=True,
        enforce_stage_interlock=False,
        home_stage=False,  # stage stays "unknown"
    )
    try:
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "unknown"
        assert "seal.start" in body["allowed_actions"]

        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 200, r.text
    finally:
        c.__exit__(None, None, None)


def test_stage_dedup_redundant_move() -> None:
    """Stage move dedup: when state == "in", stage.in is omitted from
    allowed_actions; same for stage.out. The 200 no-op redundancy is
    preserved on the wire (a POST still succeeds), but the advisory
    list doesn't advertise the no-op direction.

    Asymmetry vs seal.start is deliberate: a redundant stage move is
    harmless; sealing without a plate would waste hot air.
    """
    c, _ = _build_claimed_client(enforce_temp_interlock=True)
    try:
        # Homed to "in" — stage.in is the no-op direction.
        body = c.get("/status").json()
        assert "stage.in" not in body["allowed_actions"]
        assert "stage.out" in body["allowed_actions"]
        # But a POST is still accepted (the device honors the no-op).
        r = c.post("/control/stage/in")
        assert r.status_code == 200

        # Move to "out" — stage.out is now the no-op.
        c.post("/control/stage/out")
        body = c.get("/status").json()
        assert "stage.out" not in body["allowed_actions"]
        assert "stage.in" in body["allowed_actions"]
    finally:
        c.__exit__(None, None, None)


def test_pre_flight_412_does_not_change_stage_state() -> None:
    """A 412 refusal MUST NOT mutate stage state — the carriage never
    moved. Covers both the stage interlock refusal and the
    temperature interlock refusal."""
    # Stage interlock refusal: stage stays "out" after the 412.
    c, _ = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        c.post("/control/stage/out")
        c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "out"
    finally:
        c.__exit__(None, None, None)

    # Temperature interlock refusal: stage stays "in" after the 412.
    c, driver = _build_claimed_client(enforce_temp_interlock=True)
    try:
        driver._actual_temp = 50  # well out of band
        r = c.post("/control/seal/start", json={"seconds": 3.0})
        assert r.status_code == 412
        assert r.json()["detail"] == "Temperature outside seal band"
        body = c.get("/status").json()
        assert body["components"]["stage"]["state"] == "in"
    finally:
        c.__exit__(None, None, None)


def test_seal_start_412_stage_wins_over_temperature() -> None:
    """When BOTH interlocks would block, the stage refusal lands first
    (documented order: discrete state change is faster to fix than a
    temperature ramp). The temperature body is not surfaced."""
    c, driver = _build_claimed_client(enforce_temp_interlock=True, home_stage=False)
    try:
        driver._actual_temp = 50  # temp also out of band
        r = c.post("/control/seal/start", json={"temperature_c": 170, "seconds": 3.0})
        assert r.status_code == 412
        # Stage body, not temp body.
        body = r.json()
        assert body["detail"] == "Stage not loaded"
        assert "stage_state" in body
        assert "actual_c" not in body  # temp body would have this
    finally:
        c.__exit__(None, None, None)


def test_two_surface_agreement_property() -> None:
    """AC #9: across every (stage_state, enforce_stage_interlock,
    temp_in_band) combination, /status.allowed_actions contains
    seal.start IFF a POST /control/seal/start would NOT return 412.

    This is the load-bearing invariant — the SDK and the device must
    never disagree on whether seal.start is currently honoured.
    """
    # Driver mutations parameterised so each combination starts clean.
    def _setup_stage(c: TestClient, target: str) -> None:
        if target == "in":
            c.post("/control/stage/in")
        elif target == "out":
            c.post("/control/stage/out")
        # "unknown" = post-startup default; nothing to do.

    def _setup_temp(driver, in_band: bool) -> None:
        if in_band:
            driver._actual_temp = driver._set_temp
        else:
            driver._actual_temp = driver._set_temp - 90  # 90 C below

    combos = [
        (stage, stage_enforce, temp_in_band)
        for stage in ("in", "out", "unknown")
        for stage_enforce in (True, False)
        for temp_in_band in (True, False)
    ]

    for stage, stage_enforce, temp_in_band in combos:
        c, driver = _build_claimed_client(
            enforce_temp_interlock=True,
            enforce_stage_interlock=stage_enforce,
            home_stage=False,
        )
        try:
            _setup_stage(c, stage)
            _setup_temp(driver, temp_in_band)

            status_advertises = (
                "seal.start" in c.get("/status").json()["allowed_actions"]
            )
            post = c.post(
                "/control/seal/start", json={"seconds": 3.0}
            )
            post_accepts = post.status_code == 200

            label = (
                f"stage={stage}, enforce_stage={stage_enforce}, "
                f"temp_in_band={temp_in_band}"
            )
            assert status_advertises == post_accepts, (
                f"{label}: allowed_actions advertises={status_advertises} "
                f"but POST status={post.status_code}"
            )
        finally:
            c.__exit__(None, None, None)


def test_shutdown_then_control_returns_409(client: TestClient) -> None:
    """Spec-friendly behaviour: control endpoints fail with 409 (not 500)
    when the driver isn't connected, so the operator UI can render a
    clear "click Connect first" message.

    Note: the claim is acquired by the fixture, so the 423 path is *not*
    hit here; this test exists to assert the post-shutdown 409 path,
    which is independent of v1.1 enforcement.
    """
    client.post("/control/shutdown")
    r = client.post("/control/seal/start", json={})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Snapshot fixtures (saved for regression review)
# ---------------------------------------------------------------------------


def _scrub_for_diff(body: dict) -> dict:
    """Replace runtime-volatile fields with stable placeholders so the
    saved fixtures only diff when the schema or value semantics change."""
    body["device_time"] = "2026-04-29T22:50:01Z"
    body["uptime_seconds"] = 0.0
    body["host"] = "plateloc-pc"
    for metric in body.get("metrics", {}).values():
        if metric.get("timestamp"):
            metric["timestamp"] = "2026-04-29T22:50:01Z"
    # Claim expiry is wall-clock; scrub the same way as device_time.
    if isinstance(body.get("details"), dict) and "claimed_by" in body["details"]:
        body["details"]["claimed_by"]["expires_at"] = "2026-04-29T22:51:01Z"
    # last_error.timestamp is wall-clock (set at the moment of failure)
    # — pin it so a re-run of the fixture writer doesn't churn the file.
    if isinstance(body.get("last_error"), dict) and body["last_error"].get("timestamp"):
        body["last_error"]["timestamp"] = "2026-04-29T22:50:01Z"
    return body


def test_save_status_fixtures(unclaimed_client: TestClient) -> None:
    """Re-generate ``tests/fixtures/status_*.json``.

    Fixtures are checked into git so reviewers can eyeball schema
    changes. After intentional schema changes, re-run pytest and commit
    the diffs as part of the PR.

    Coverage:
      - status_requires_init.json          - hardware not connected (spec example)
      - status_ready.json                  - ready, stage homed in, seal.start present
      - status_ready_claimed.json          - same, but with details.claimed_by
      - status_ready_stage_unknown.json    - ready after startup, stage not homed,
                                             seal.start ABSENT (v1.3.0)
      - status_ready_stage_out.json        - ready with stage out, seal.start ABSENT (v1.3.0)
      - status_ready_heating.json          - heater below band, seal.start ABSENT
      - status_ready_mid_cycle_failure.json - last_error populated, stage unknown (v1.3.0)
      - status_busy.json                   - cycle in progress (uses stub driver)
      - status_dry_run.json                - dry-run mode advertised in /status
    """
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    FIXTURES.mkdir(exist_ok=True)

    # dry_run snapshot. Home the stage so the dry-run example matches
    # the "ready to seal" shape an operator hits during normal CI.
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "fixture", "session_id": "fixture-dry-run", "ttl_s": 60},
    )
    unclaimed_client.headers["X-Claim-Token"] = r.json()["claim_token"]
    unclaimed_client.post("/control/stage/in")
    unclaimed_client.post(
        "/control/release",
        headers={"X-Claim-Token": unclaimed_client.headers["X-Claim-Token"]},
    )
    del unclaimed_client.headers["X-Claim-Token"]
    body = unclaimed_client.get("/status").json()
    (FIXTURES / "status_dry_run.json").write_text(
        json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
    )

    # ready/busy: spin up a fresh service with the stub injected via
    # driver_factory but `dry_run=False`, so equipment_status reflects
    # the real operational state machine.
    app = create_app(dry_run=False, enforce_claims=True)
    app.state.service._driver_factory = _StubPlateLoc
    with TestClient(app) as alt:
        # Acquire the claim under a stable session_id so the fixture is
        # reproducible (apart from expires_at, which the scrubber pins).
        r = alt.post(
            "/control/claim",
            json={"owner": "fixture", "session_id": "fixture-session", "ttl_s": 60},
        )
        token = r.json()["claim_token"]
        alt.headers["X-Claim-Token"] = token

        alt.post("/control/startup", json={})

        # status_ready_stage_unknown: after startup but BEFORE the
        # operator homes the carriage. v1.3.0 says seal.start MUST be
        # absent from allowed_actions in this state.
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "unknown"
        assert "seal.start" not in body["allowed_actions"]
        (FIXTURES / "status_ready_stage_unknown.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # status_ready_stage_out: explicitly extended carriage. Stage
        # interlock blocks seal.start; stage.in is the operator-visible
        # next step.
        alt.post("/control/stage/out")
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "out"
        assert "seal.start" not in body["allowed_actions"]
        assert "stage.in" in body["allowed_actions"]
        assert "stage.out" not in body["allowed_actions"]
        (FIXTURES / "status_ready_stage_out.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # Home the stage for the runnable snapshots.
        alt.post("/control/stage/in")
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "in"
        # Snapshot WITH the claim metadata so reviewers see the v1.1 shape.
        (FIXTURES / "status_ready_claimed.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # Snapshot WITHOUT claim metadata (back-compat with v1.0 readers).
        # Release the claim, re-poll, snapshot.
        alt.post("/control/release", headers={"X-Claim-Token": token})
        del alt.headers["X-Claim-Token"]
        body = alt.get("/status").json()
        assert "claimed_by" not in body["details"]
        assert body["components"]["stage"]["state"] == "in"
        assert "seal.start" in body["allowed_actions"]
        (FIXTURES / "status_ready.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # heating snapshot: stage in (so only the temperature gate
        # blocks), setpoint above actual by more than tolerance.
        r = alt.post(
            "/control/claim",
            json={
                "owner": "fixture",
                "session_id": "fixture-session",
                "ttl_s": 60,
            },
        )
        alt.headers["X-Claim-Token"] = r.json()["claim_token"]
        # Bump the setpoint without changing actual: the stub's
        # set_sealing_temperature normally snaps actual to setpoint, so
        # we mutate _set_temp directly to keep the band check failing.
        heating_driver = app.state.service._driver
        heating_driver._set_temp = 170
        heating_driver._actual_temp = 80  # 90 C below setpoint (tol=2)
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        assert body["components"]["stage"]["state"] == "in"  # stage OK
        assert "seal.start" not in body["allowed_actions"]   # temp blocks
        (FIXTURES / "status_ready_heating.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # status_ready_mid_cycle_failure: stage was homed, but a fake
        # mid-cycle COM fault on start_cycle leaves last_error set AND
        # stage.state == "unknown" (per the v1.3.0 transition table).
        heating_driver._actual_temp = heating_driver._set_temp  # heal temp gate

        def _boom(*_a: object, **_kw: object) -> None:
            raise OSError("Simulated mid-cycle COM fault")

        original_start_cycle = heating_driver.start_cycle
        heating_driver.start_cycle = _boom
        r = alt.post(
            "/control/seal/start", json={"temperature_c": 170, "seconds": 3.0}
        )
        assert r.status_code == 500, r.text
        heating_driver.start_cycle = original_start_cycle  # restore
        body = alt.get("/status").json()
        assert body["equipment_status"] == "error"
        assert body["components"]["stage"]["state"] == "unknown"
        assert body["last_error"] is not None
        (FIXTURES / "status_ready_mid_cycle_failure.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # busy snapshot: home stage again (mid-cycle failure left it
        # unknown), heal temperature, restart cleanly.
        alt.post("/control/stage/in")
        heating_driver._set_temp = 170
        heating_driver._actual_temp = 170
        alt.post(
            "/control/seal/start", json={"temperature_c": 170, "seconds": 3.0}
        )
        body = alt.get("/status").json()
        assert body["equipment_status"] == "busy"
        (FIXTURES / "status_busy.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

    # requires_init: shut the dry-run driver down explicitly.
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "fixture", "session_id": "fixture-shutdown", "ttl_s": 60},
    )
    unclaimed_client.headers["X-Claim-Token"] = r.json()["claim_token"]
    unclaimed_client.post("/control/shutdown")
    unclaimed_client.post(
        "/control/release",
        headers={"X-Claim-Token": unclaimed_client.headers["X-Claim-Token"]},
    )
    del unclaimed_client.headers["X-Claim-Token"]
    body = unclaimed_client.get("/status").json()
    assert body["equipment_status"] == "requires_init"
    (FIXTURES / "status_requires_init.json").write_text(
        json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
    )
