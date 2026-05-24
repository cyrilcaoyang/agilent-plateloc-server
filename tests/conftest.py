"""Shared pytest fixtures.

The API tests run the FastAPI app with ``dry_run=True`` so no Windows /
ActiveX dependencies are required. ``conftest.py`` keeps that switch
out of every individual test.

v1.1 note: ``enforce_claims=True`` is the production default and is
exercised here. The default ``client`` fixture pre-acquires a claim and
attaches ``X-Claim-Token`` to every subsequent request, so individual
tests behave as if claims didn't exist. Tests that need to exercise the
claim protocol itself (acquire/heartbeat/release, 423 enforcement,
advisory mode) use the more explicit fixtures below.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agilent_plateloc.api import create_app


@pytest.fixture
def unclaimed_client() -> Iterator[TestClient]:
    """A `TestClient` whose lifespan auto-connects the dry-run stub but
    does not pre-acquire a claim. Use for tests of the public spec
    surface (``/``, ``/health``, ``/status``, ``/openapi.json``) and
    for tests that explicitly assert ``/control/*`` returns 423 when
    no token is provided."""
    app = create_app(dry_run=True, enforce_claims=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(unclaimed_client: TestClient) -> TestClient:
    """Default `TestClient` for /control/* tests. Pre-acquires a claim
    and attaches ``X-Claim-Token`` to every request so existing v1.0-era
    test bodies continue to work unchanged.

    v1.3.0 addition: this fixture also homes the stage to ``"in"`` so
    legacy tests (seal-cycle round-trip, temperature interlock, etc.)
    can issue ``/control/seal/start`` without bumping into the new
    stage interlock. Tests that exercise the stage interlock itself
    construct their own clients (see ``_build_claimed_client`` in
    ``test_api.py``).

    Each test gets a fresh app/service - no shared state across tests.
    """
    r = unclaimed_client.post(
        "/control/claim",
        json={"owner": "pytest", "session_id": "pytest-default", "ttl_s": 60.0},
    )
    assert r.status_code == 200, r.text
    unclaimed_client.headers["X-Claim-Token"] = r.json()["claim_token"]
    unclaimed_client.post("/control/stage/in")
    return unclaimed_client


@pytest.fixture
def advisory_client() -> Iterator[TestClient]:
    """A `TestClient` built with ``enforce_claims=False``. The device
    still publishes ``allowed_actions`` and ``details.claimed_by`` but
    does not block ``/control/*`` calls that omit ``X-Claim-Token``.
    Used to verify the v1.1 *advisory* mode."""
    app = create_app(dry_run=True, enforce_claims=False)
    with TestClient(app) as c:
        yield c
