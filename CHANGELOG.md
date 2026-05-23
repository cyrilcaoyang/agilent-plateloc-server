# Changelog

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
