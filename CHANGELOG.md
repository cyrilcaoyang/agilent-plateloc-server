# Changelog

## v1.3.2 — classify `no_plate` and `vacuum_error`

Two seal-cycle faults that previously fell through to `com_other` now
get specific `last_error.code`s: `no_plate` ("No Plate In Holder") and
`vacuum_error` ("Hot Plate Vacuum Error"). Both surfaced live on the
bench; each is separately actionable (load a plate vs. check seal film
/ vacuum), so the dashboard can render distinct recovery hints.
Additive and back-compatible — the wire shape (`ErrorInfo.code` is
`str | None`) is unchanged.

## v1.3.1 — structured `last_error.code` taxonomy

`last_error.code` was a free-form string through v1.3.0 (typically
the failing method name). v1.3.1 promotes it to a closed enum, so
the dashboard branches on `code` and renders a targeted recovery
hint instead of regex-matching on the driver's free-form
`message`.

### The taxonomy

`LAST_ERROR_CODES` (in `agilent_plateloc_server.service`):

* `low_air_pressure` — lab air supply dropped below the press
  requirement
* `com_init_failed` — startup couldn't reach the physical sealer
  (powered off, cable disconnected, COM port busy)
* `com_timeout` — a COM call timed out without a specific driver
  error code
* `com_other` — catch-all for driver errors we don't yet classify
* `heater_overtemp` — exceeded safety limit
* `heater_undertemp` — failed to reach setpoint within the ramp
  window
* `profile_not_found` — Initialize() called with a profile name not
  configured in the Diagnostics dialog
* `stage_jam` — stage move failed in a way that wasn't simply
  "stage didn't move" (e.g. driver reports the press is down)
* `process_internal` — a Python type error (KeyError, etc.) bubbled
  up — software bug, not a driver fault

### How classification works

`PlateLocService._classify_error(method_name, exc, detail)` returns
the code. Order is deliberate:

1. Python type errors (`process_internal`) — software bugs are
   distinguished from driver faults so the dashboard files a
   ticket rather than reaching for the Diagnostics dialog.
2. Specific text matches (`low_air_pressure`, `heater_*`,
   `profile_not_found`) — driver text is the most reliable signal.
3. `com_timeout` — TimeoutError type or "timeout"/"timed out"
   substring.
4. Context fallbacks (`stage_jam` for stage moves,
   `com_init_failed` for startup) — fire when the driver text is
   unhelpful.
5. `com_other` — default. Falling here for a repeated failure mode
   is the cue to add a code, not to grow the catch-all.

### Single chokepoint

A new internal helper `PlateLocService.set_last_error(code=...,
message=..., severity=...)` rejects any code outside the enum with
a `ValueError`. The existing `_record_error` was the only mutation
site already, and it now routes through the helper. A developer
who introduces an unclassified code gets an immediate failure, not
a free-form string on the wire.

### Compatibility

* `ErrorInfo.code` is still typed `str | None` in the
  STATUS_SPEC envelope — the wire shape is unchanged. Clients that
  ignore `code` continue to work; clients that branch on it now
  see consistently populated taxonomy values.
* `message` still carries the driver's raw text verbatim.
* The v1.2.1 auto-clear contract is unchanged: the entire
  `last_error` (code + message + severity + timestamp) clears
  together on the first 2xx operational action.

### Source

Prompt: "v1.3.1 — add structured last_error.code taxonomy
mirroring the 412-by-code pattern at the precondition layer."

---

## v1.3.0 — stage-position interlock

The first behaviour change broader than v1.2.x's auto-clear / mirror:
`/status.components.stage.state` is now meaningful (no longer a
hardcoded `"unknown"`) and a layer-1 interlock refuses
`/control/seal/start` when the carriage isn't loaded.

### Stage interlock

| | |
|---|---|
| **Rule** | `components.stage.state == "in"` |
| **412 body** | `{"detail": "Stage not loaded", "stage_state": "out"\|"unknown", "required": "in"}` |
| **`Retry-After`** | not set (recovery is operator-driven, not time-based) |
| **Config flag** | `[service].enforce_stage_interlock` (default `true`); independent of `enforce_temp_interlock` |

Same shape as the v1.2.1 temperature interlock: a single
`PlateLocService.evaluate_stage_interlock(stage_state) ->
(should_block, body)` helper feeds both the 412 path *and* the
`/status.allowed_actions` builder. The two surfaces cannot drift.

### Stage state is in-memory, command-tracked

The Agilent COM API exposes no stage-position query
(`MoveStageIn`/`MoveStageOut` only — no `GetStagePosition`). v1.3.0
tracks position by remembering the last commanded direction:

* Fresh process start → `"unknown"` (deliberate; an NSSM restart
  cannot vouch for the carriage)
* `POST /control/stage/in` returns 200 → `"in"`
* `POST /control/stage/out` returns 200 → `"out"`
* `POST /control/seal/start` returns 200 → `"in"` (cycle commits the
  carriage)
* `POST /control/seal/start` returns 412 pre-flight → unchanged
  (refusal had no side effect)
* `POST /control/seal/start` fails mid-cycle (5xx) → `"unknown"`
* `POST /control/stage/*` 4xx/5xx → `"unknown"`
* `POST /control/shutdown` returns 200 → `"unknown"` (next startup
  must re-home)

After any process restart the operator must `POST /control/stage/in`
(or `out`) before the device will accept `seal.start`. The dashboard
tile renders the disabled state by reading
`/status.allowed_actions` — no extra client-side logic needed.

### allowed_actions composition

`/status.allowed_actions` now drops:

* `seal.start` when EITHER the stage OR the temperature interlock
  would refuse (412-path order: stage first, temperature second);
* `stage.in` when `stage.state == "in"` (no-op direction);
* `stage.out` when `stage.state == "out"` (no-op direction).

A redundant stage move is still accepted as a 200 no-op on the wire
— the dedup is purely advisory, to keep an operator UI from
advertising a no-op button.

### Compatibility

* STATUS_SPEC v1.1 conformance preserved.
* `412` body shape for the temperature interlock and the
  `Retry-After` header are unchanged.
* `enforce_temp_interlock` flag unchanged. `enforce_stage_interlock`
  is new (default `true`).
* Workflow code that already calls `/control/stage/in` before
  `seal.start` needs no changes. Code that didn't will start seeing
  412s with the new `Stage not loaded` body; the fix is to home the
  carriage explicitly.

### Source

Prompt: "v1.3.0 — track and publish components.stage.state, gate
seal.start on stage.state == 'in', mirror v1.2.1's
single-helper-extraction pattern."

---

## v1.2.1 — operational follow-ups on top of v1.2.0

Two additive, schema-preserving fixes that close gaps the dashboard
could not patch from where it stood.

### `allowed_actions` now mirrors the temperature interlock

`/status` previously published `seal.start` in `allowed_actions`
whenever `equipment_status` was `ready` or `dry_run`, even while the
heater was heating. Workflow clients that trusted `allowed_actions`
verbatim would still attempt the call and eat a 412 from the layer-1
band check.

A shared `PlateLocService.evaluate_temperature_interlock(actual,
setpoint) -> (should_block, body)` helper now drives both surfaces:

* `/control/seal/start` raises `TemperatureOutOfBand` (HTTP 412) when
  `should_block` is `True`. Unchanged on the wire.
* `/status` drops `seal.start` from `allowed_actions` when the same
  helper says block. The 412 path remains authoritative.

`enforce_temp_interlock=False` short-circuits the helper in both
places — single source of truth for "is the interlock active right
now."

### `last_error` auto-clears on the next successful action

`/status.last_error` was sticky: a transient Low Air Pressure exception
hours ago would still be reported while the device was genuinely idle.

`v1.2.1` clears `last_error` after every 2xx response from an
operational `/control/*` endpoint (startup, shutdown, seal.start,
seal.stop, seal.set_temperature, seal.set_time, stage.in, stage.out).
Claim infrastructure (`/control/claim`, `/control/heartbeat`,
`/control/release`) and read-only endpoints (`/`, `/health`,
`/status`) do not clear. A 412 refusal does not clear either — only
an overall 2xx response does, so a multi-step endpoint that succeeds
mid-call and then refuses does not partially clear.

The clear is wired at the API layer (`service.clear_last_error_on_success()`
called immediately before each 2xx response) so it triggers on the
endpoint outcome, not on individual service calls.

### Compatibility

* STATUS_SPEC v1.1 conformance preserved.
* `412` body shape, `Retry-After` header, and `enforce_temp_interlock`
  flag are unchanged.
* No client-side migration required.

### Source

Prompt: "v1.2.1 — drop seal.start from allowed_actions when band
violated; auto-clear last_error on next successful action."
