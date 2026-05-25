"""Lab equipment status spec v1.1.

This module is a verbatim copy of the unified status contract from the
ac-organic-lab monorepo (``docs/STATUS_SPEC.md`` for the v1.0 base and
``docs/STATUS_SPEC_v1_1.md`` for the v1.1 additions). It MUST stay in
sync with those documents until a shared ``lab-status-contract`` Python
package is published; once it is, replace this file with::

    from lab_status_contract import (
        EquipmentStatus, ProbeResponse, HealthResponse,
        ClaimRequest, ClaimResponse, ClaimRejection, ClaimedBy, ...
    )

Conformance: agilent_plateloc_server REST API conforms to lab status spec v1.1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.1"


EquipmentKind = Literal[
    "solid_doser",
    "liquid_handler",
    "press",
    "fume_hood",
    "robot_arm",
    "environmental_sensor",
    "hplc",
    "plate_reader",
    "plate_sealer",
    "plate_stacker",
    "other",
]

EquipmentState = Literal[
    "ready",          # initialized, idle, can accept commands
    "busy",           # performing an operation
    "requires_init",  # service up but hardware not initialized (e.g. needs POST /control/startup)
    "degraded",       # running but a sub-component is unhealthy
    "dry_run",        # simulation mode, no hardware connected
    "error",          # hardware reported an error
    "e_stop",         # emergency stopped
    "unknown",        # state cannot be determined
]


class ComponentStatus(BaseModel):
    connected: bool
    state: str  # equipment-defined string; pick a small enum per equipment kind
    message: str | None = None
    last_event_at: datetime | None = None


class MetricValue(BaseModel):
    value: float | int | str | bool
    unit: str | None = None
    timestamp: datetime | None = None


class ErrorInfo(BaseModel):
    code: str | None = None
    message: str
    severity: Literal["info", "warning", "error", "critical"]
    timestamp: datetime


class EquipmentStatus(BaseModel):
    """Unified equipment status envelope (spec v1.1).

    The ``allowed_actions`` field is the v1.1 addition: a flat list of
    ``Skill.name`` values the device will currently honor on
    ``/control/*``. Empty list (or absent on a v1.0 device) means the
    SDK should fall back to ``requires_states`` from the catalog.
    ``details.claimed_by`` is the other v1.1 addition; it is published
    as a free-form value under ``details`` (not a top-level field) so
    v1.0 readers parse it transparently.
    """

    protocol_version: str = PROTOCOL_VERSION

    # Identity
    equipment_id: str
    equipment_name: str
    equipment_kind: EquipmentKind
    equipment_version: str | None = None
    host: str | None = None  # local hostname only (output of `hostname`)

    # Operational state
    equipment_status: EquipmentState
    message: str | None = None
    required_actions: list[str] = Field(default_factory=list)

    # NEW in v1.1: skill names the device will currently honor on /control/*.
    # Authoritative; the SDK prefers this over its own catalog `requires_states`
    # whenever the field is present and non-empty.
    allowed_actions: list[str] = Field(default_factory=list)

    # Timing
    device_time: datetime
    uptime_seconds: float | None = None

    # Sub-equipment / measurements
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    last_error: ErrorInfo | None = None

    # Free-form per-equipment data; safe to display in a debug/details panel.
    # When a claim is held, ``details["claimed_by"]`` is a serialised
    # ``ClaimedBy`` (the v1.1 spec keeps the field nested under details so
    # v1.0 readers parse it without changes).
    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResponse(BaseModel):
    """Body of `GET /` - the cheapest possible identity probe."""

    equipment_id: str
    equipment_name: str
    protocol_version: str = PROTOCOL_VERSION


class HealthResponse(BaseModel):
    """Body of `GET /health` - service liveness."""

    status: Literal["healthy"] = "healthy"


# ---------------------------------------------------------------------------
# v1.1 claim protocol shapes (per docs/STATUS_SPEC_v1_1.md)
# ---------------------------------------------------------------------------


class ClaimedBy(BaseModel):
    """Identity of the holder of the active claim.

    Surfaced under ``EquipmentStatus.details["claimed_by"]`` so any reader
    of ``/status`` sees who currently controls the device without a
    separate request. ``expires_at`` is the heartbeat-extended absolute
    UTC timestamp.
    """

    session_id: str
    owner: str
    expires_at: datetime


class ClaimRequest(BaseModel):
    """Body of ``POST /control/claim``.

    ``ttl_s`` is a request; the device may clamp to its own min/max and
    the actual TTL is read off the returned ``expires_at``.
    """

    owner: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    ttl_s: float = Field(default=30.0, ge=1.0, le=600.0)


class ClaimResponse(BaseModel):
    """Body of a successful ``POST /control/claim`` (HTTP 200) and of a
    successful ``POST /control/heartbeat`` (HTTP 200; an empty 204 is
    also valid).
    """

    claim_token: str
    heartbeat_interval_s: float
    expires_at: datetime


class ClaimRejection(BaseModel):
    """Body of ``POST /control/claim`` when the device refuses the claim
    (HTTP 409 / 423).

    ``retry_after_s`` is the device's hint for how long to wait; clients
    should also honor the standard ``Retry-After`` header. ``claimed_by``
    is best-effort: it is populated when the device chose to publish who
    currently holds the lock.
    """

    detail: str
    claimed_by: ClaimedBy | None = None
    retry_after_s: float | None = None
