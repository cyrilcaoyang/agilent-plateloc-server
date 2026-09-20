# Agilent PlateLoc sealer — API reference

Base: the service's `http://<host>:8010` (lab deployment; the in-repo
`[service] port` default is 8000). All timestamps UTC ISO-8601.
Request/response schemas: `/openapi.json`. Tags: `spec`, `claim`, `control`,
`documentation`. Fourteen routes, listed below.

## Read (always available, side-effect-free, no claim)

| route | returns |
|---|---|
| `GET /` | `ProbeResponse` — `equipment_id`, `equipment_name`, `protocol_version` (`"1.2"`) |
| `GET /health` | `{"status": "healthy"}` |
| `GET /status` | full `EquipmentStatus` envelope (see below) |
| `GET /openapi.json`, `GET /docs` | OpenAPI document, Swagger UI |
| `GET /agent-docs`, `GET /agent-docs/api-reference`, `GET /llms.txt` | this documentation (`text/markdown`, `text/plain`) |

`GET /status` always answers 200 unless the process itself is broken: every
per-getter failure is folded into `equipment_status: "degraded"` instead of
raising. It never writes to the instrument. While a seal cycle owns the COM
channel the envelope serves the **last** readback rather than queueing behind
the blocking `StartCycle`; `details.readings_as_of` and the metric timestamps
then carry the instant those values were taken.

| `/status` field | content |
|---|---|
| `equipment_status` | `ready` · `busy` · `degraded` · `error` · `requires_init` · `dry_run` |
| `activity`, `activity_since` | `idle` \| `running`, and the start of the current span |
| `allowed_actions` | see *Skill names* |
| `required_actions` | `["startup"]` while `requires_init`, otherwise empty |
| `components.sealer` | `state`: `idle` \| `busy` |
| `components.heater` | `state`: `heating` \| `cooling` \| `stable` \| `unknown`, with a human `message` |
| `components.stage` | `state`: `in` \| `out` \| `unknown` (command-tracked) |
| `metrics` | `actual_temperature` (C), `setpoint_temperature` (C), `temperature_delta_c` (actual − setpoint), `sealing_time` (s), `cycle_count` and `cycles_total` (the same lifetime odometer) |
| `last_error` | `{code, message, severity, timestamp}` — codes per the agent guide |
| `details` | `claimed_by`, `com_port`, `temperature_tolerance_c`, `firmware_version`, `activex_version`, `profile` (when one was used to connect), `cycle_started_at` (while running), `readings_as_of` (when the readback is stale), `dry_run` |

## Claim protocol

| route | body / header | responses |
|---|---|---|
| `POST /control/claim` | `{owner, session_id, ttl_s?}` — `owner`/`session_id` 1..120 chars, `ttl_s` 1..600 (then clamped to 5..300) | 200 `{claim_token, heartbeat_interval_s, expires_at}`; 409 top-level `{detail, claimed_by, retry_after_s}` + `Retry-After` when another session holds it; idempotent for the same `session_id` |
| `POST /control/heartbeat` | header `X-Claim-Token` | 200 `ClaimResponse` with a new `expires_at`; 401 `{detail}` unknown/expired |
| `POST /control/release` | header `X-Claim-Token` | 204, idempotent — a mismatched or missing token is ignored rather than 401'd, so a client that lost its token cannot break someone else's claim |

## Control (header `X-Claim-Token` required → 423 without it)

`enforce_claims=false` turns the gate off; the device still issues tokens and
publishes `details.claimed_by`, it just does not block.

| route | body | gate / notes |
|---|---|---|
| `POST /control/startup` | `{"profile": str \| null}` — **the body is required**; send `{}` for the configured profile | connects and initializes the ActiveX control, then writes the 40 C boot setpoint (best-effort). 503 `{detail}` on failure |
| `POST /control/shutdown` | — | disconnects; resets `stage.state` to `"unknown"`; leaves the device `requires_init` until an explicit `startup` |
| `POST /control/seal/temperature` | `{temperature_c: int}` 20..235 | 409 not connected / cycle in flight; 422 out of range |
| `POST /control/seal/time` | `{seconds: float}` 0.5..12.0 | 409 not connected / cycle in flight; 422 out of range |
| `POST /control/seal/start` | `{temperature_c?: int, seconds?: float}` — same bounds; body required, `{}` is valid | applies the optional settings, then runs one cycle **synchronously**. 412 per the interlock table; 409 not connected or a cycle already in flight |
| `POST /control/seal/stop` | — | abort class; the only control call honored while `activity == "running"`. Idempotent (2xx no-op when nothing runs). 409 only when not connected |
| `POST /control/stage/in` | — | carriage to the loaded position; sets `stage.state` to `"in"` on a clean return, leaves it `"unknown"` on failure. 409 not connected or press is down |
| `POST /control/stage/out` | — | carriage out; same tracking. 409 not connected or press is down |

A POST to the direction the stage is already in is a 200 no-op; that direction
is simply left out of `allowed_actions` so an operator UI does not render a
redundant button.

### `POST /control/seal/start` — the three 412 bodies

Checked in this order; each is identified by its **fields**, not by `detail`
text.

| order | body | `Retry-After` |
|---|---|---|
| 1 | `{detail: "Stage not loaded", stage_state, required: "in"}` | — |
| 2 | `{detail: "Recent operational failure not cleared", last_error_code, last_error_message, retry_after_s}` | yes |
| 3 | `{detail, actual_c, setpoint_c, tolerance_c, retry_after_s}` | when an estimate exists |

All three are emitted as **top-level** JSON (not wrapped in `{"detail": ...}`)
so `response.json()["retry_after_s"]` parses without unwrapping. The 423 and 409
claim bodies use the same convention.

## Refusal codes

| status | meaning |
|---|---|
| 401 | `heartbeat` with an unknown or expired token |
| 409 | claim held by another session, **or** a device-state conflict on a control call: not connected, or a seal cycle already in flight (§6.1 — a conflict, not a precondition) |
| 412 | a layer-1 interlock refused `seal.start`; body shape says which |
| 422 | request body failed validation (missing body, out-of-range `temperature_c` / `seconds` / `ttl_s`) |
| 423 | missing or invalid `X-Claim-Token` on `/control/*`; body carries `claimed_by` |
| 500 | an unexpected driver exception on a control call |
| 503 | `startup` could not connect |

All control responses are `{"ok": true, "message": str}` on 2xx. A 2xx from an
operational control endpoint clears `last_error`; a refusal does not, and the
claim routes never do.

## Skill names (what `allowed_actions` lists)

`startup`, `shutdown`, `seal.start`, `seal.stop`, `seal.set_temperature`,
`seal.set_time`, `stage.in`, `stage.out` — the names the `lab-skills` catalog
uses for `kind: plate_sealer`.

| state | advertised |
|---|---|
| `requires_init` | `["startup"]` |
| idle and healthy | everything except `seal.stop` (nothing to stop), minus `seal.start` when any interlock would refuse it, minus the stage direction it is already in |
| `activity == "running"` | `["shutdown", "seal.stop"]` — checked before the health branches so a mid-cycle fault cannot take the abort away |

`error` and `degraded` deliberately do **not** collapse to `["shutdown"]`: after
a failed cycle the operator's recovery is exactly `stage.out`, retrieving the
plate from a hot chamber. The run itself is withheld by the health interlock
instead.
