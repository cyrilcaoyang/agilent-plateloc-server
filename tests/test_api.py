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
    """
    body = client.get("/status").json()
    assert body["equipment_status"] == "dry_run"
    assert "seal.start" in body["allowed_actions"]
    assert "stage.in" in body["allowed_actions"]

    client.post("/control/shutdown")
    body = client.get("/status").json()
    assert body["equipment_status"] == "requires_init"
    assert body["allowed_actions"] == ["startup"]


def test_allowed_actions_ready_state() -> None:
    """ready (real driver, not dry_run) exposes the full operating set
    minus seal.stop (which only makes sense while busy)."""
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
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        actions = set(body["allowed_actions"])
        assert {"seal.start", "stage.in", "stage.out", "shutdown"} <= actions
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


def _build_claimed_client(*, enforce_temp_interlock: bool) -> tuple:
    """Spin up a service with the dry-run stub injected (so the
    operational state machine runs, not the dry_run shortcut), acquire
    a claim, and return ``(client, driver)`` so tests can mutate the
    stub's temperatures directly."""
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    app = create_app(
        dry_run=False,
        enforce_claims=True,
        enforce_temp_interlock=enforce_temp_interlock,
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
        # Sibling actions still present.
        assert "stage.in" in body["allowed_actions"]
        assert "stage.out" in body["allowed_actions"]
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
    regardless of heater state or band."""
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
    return body


def test_save_status_fixtures(unclaimed_client: TestClient) -> None:
    """Re-generate ``tests/fixtures/status_*.json``.

    Fixtures are checked into git so reviewers can eyeball schema
    changes. After intentional schema changes, re-run pytest and commit
    the diffs as part of the PR.

    Coverage:
      - status_requires_init.json   - hardware not connected (spec example)
      - status_ready.json           - connected & idle, seal.start present
      - status_ready_claimed.json   - same, but with details.claimed_by
      - status_ready_heating.json   - heater below band, seal.start ABSENT
      - status_busy.json            - cycle in progress (uses stub driver)
      - status_dry_run.json         - dry-run mode advertised in /status
    """
    from agilent_plateloc.api import create_app
    from agilent_plateloc.service import _StubPlateLoc

    FIXTURES.mkdir(exist_ok=True)

    # dry_run snapshot (no claim active).
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
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
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
        (FIXTURES / "status_ready.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # heating snapshot: setpoint above actual by more than tolerance.
        # Re-acquire the claim because the previous block released it.
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
        assert "seal.start" not in body["allowed_actions"]
        (FIXTURES / "status_ready_heating.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # Re-acquire for the busy snapshot.
        r = alt.post(
            "/control/claim",
            json={
                "owner": "fixture",
                "session_id": "fixture-session",
                "ttl_s": 60,
            },
        )
        alt.headers["X-Claim-Token"] = r.json()["claim_token"]

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
