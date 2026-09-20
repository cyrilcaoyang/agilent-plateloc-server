# Agilent PlateLoc sealer — agent guide

This service fronts one Agilent PlateLoc Thermal Microplate Sealer through its
VWorks ActiveX COM control (32-bit, hosted in a COM surrogate) and speaks the
AC Organic lab's STATUS_SPEC **v1.2** (`equipment_kind: "plate_sealer"`). Read
this before driving it; the [API reference](/agent-docs/api-reference) lists
every route, and `/openapi.json` carries the exact request and response
schemas.

## What "primary operation" means here

One **seal cycle**: the carriage under the press, the heated platen down on the
film, for `sealing_time` seconds. The ActiveX control is configured in
*blocking* mode, so `StartCycle` returns only when the physical cycle has
finished. `activity == "running"` is exactly the span of that COM call —
command-tracked, because the Agilent control exposes no "cycle in progress"
query, and never derived from the health word.

Two consequences an agent has to plan for:

- `POST /control/seal/start` is **synchronous**. Your own request returns after
  the cycle is over, so you will never observe `running` on your own connection;
  a concurrent `/status` poller will. Budget a request timeout above the seal
  time (≤ 12 s) plus press travel.
- A process restart mid-cycle reports `idle` — the tracking is in memory. The
  window is one cycle.

`metrics.cycles_total` is the instrument's lifetime odometer (the same number as
`metrics.cycle_count`), so it is monotonic by construction. A cycle lasts a few
seconds, far under any sane poll interval, so the poll-to-poll delta of that
counter — not sampled `activity` — is the only honest way to account for cycles
you slept through.

## Health vs. activity (STATUS_SPEC §2.2 / §2.3)

`equipment_status` answers "is it fit for a run"; `activity` answers "is it
running". They are computed independently, health first, so a fault that lands
mid-cycle is not masked by `busy`.

| `equipment_status` | meaning here |
|---|---|
| `ready` | connected, idle, every readback healthy |
| `busy` | connected, a seal cycle in flight, readbacks healthy |
| `degraded` | connected, but at least one instrument readback threw. The failing reads are joined into `message` and surfaced as a `last_error` with `severity: "warning"`. Sealing is **not** withheld for this on its own. |
| `error` | an operational `/control/*` action failed within the last 60 s |
| `requires_init` | the service is up but **not connected to the sealer** — see *Startup and shutdown* |
| `dry_run` | in-memory stub; no hardware. Wins over every other branch. |

`e_stop` and `unknown` are handled defensively in the `allowed_actions` builder
but this device never reports them.

## Claims (STATUS_SPEC §5)

Every `/control/*` request needs a valid `X-Claim-Token` (`enforce_claims`
defaults to true). Acquire with `POST /control/claim` (`{owner, session_id,
ttl_s}`), heartbeat at the returned `heartbeat_interval_s` (= ttl/3, floor 2 s),
release when done. Without a valid token control calls return **423** with a
top-level `{detail, claimed_by, retry_after_s}` body; a claim held by another
session makes `POST /control/claim` return **409** with the same shape plus a
`Retry-After` header. `details.claimed_by` on `/status` shows the current holder.
Re-claiming from the same `session_id` is idempotent and just refreshes the
lease, so calling `claim` at the top of every workflow is safe.

Claims coordinate, they do not authenticate — there is no auth at the device;
access is gated by Tailscale ACLs.

## Startup and shutdown

- `POST /control/startup` (body `{}` or `{"profile": "..."}`) opens the COM
  surrogate, initializes the ActiveX control against the named PlateLoc
  Diagnostics profile, and connects. Failure is **503**.
- **Every successful connect writes a 40 C boot setpoint** (`[instrument]
  boot_setpoint_c`, `0` disables). This fires on the boot auto-connect, on its
  retry loop, and on operator `POST /control/startup`. The reason: the
  instrument reverts to its own front-panel default (160 C on this unit)
  whenever it power-cycles, and without the write a reboot leaves the heater
  driving toward a hot standby nobody asked for. The write is best-effort — if
  it fails the connect still succeeds and the instrument keeps its own setpoint.
  **Plan for it:** straight after `startup` the setpoint is 40 C, so
  `seal.start` is withheld until you set a real sealing temperature and the
  plate reaches it. See *The seal sequence*.
- At process boot the service tries to connect once (timeout
  `startup_connect_timeout_s`, default 15 s). If that fails, a background task
  retries every `startup_retry_interval_s` (default 30 s) until the **first**
  successful connect, then stops for good — the USB serial adapter can
  enumerate after this service does.
- `POST /control/shutdown` disconnects. The service then reports
  `requires_init`, and the retry loop deliberately does **not** fight it: a
  shutdown is an operator decision, and the retry has already stopped itself
  after the first success anyway. The device stays disconnected until someone
  POSTs `/control/startup`.
- Therefore: **do not end a session with the device shut down** unless you were
  asked to. Release your claim and leave it connected.
- `shutdown` also resets `components.stage.state` to `"unknown"`, so the next
  session must re-home the carriage before it can seal.

## Preconditions for a seal cycle (STATUS_SPEC §6)

Three layer-1 interlocks gate `seal.start`, and they are evaluated by the same
three `evaluate_*_interlock` helpers that build `/status.allowed_actions` — so
the advertised list and the 412 refusals cannot drift. Read `allowed_actions`
first; if `seal.start` is missing, a POST would 412 with a body you branch on
by **shape**, not by `detail` text.

| precondition | when it blocks | 412 body | `Retry-After` |
|---|---|---|---|
| stage loaded | `components.stage.state != "in"` | `{detail: "Stage not loaded", stage_state, required: "in"}` | none — recovery is a `POST /control/stage/in`, not a wait |
| no uncleared failure | `last_error` is younger than the 60 s window | `{detail: "Recent operational failure not cleared", last_error_code, last_error_message, retry_after_s}` | yes |
| temperature in band | `abs(actual − setpoint) > [film] temperature_tolerance_c` (default 2 C), **or** either reading is unavailable (fail closed) | `{detail: "Temperature outside seal band" \| "Cannot verify temperature: actual or setpoint unavailable", actual_c, setpoint_c, tolerance_c, retry_after_s}` | when an estimate exists |

Checked in that order — the two in-memory gates before the one that needs COM
reads, and a wrong-stage refusal is one click for the operator to fix while a
temperature ramp is minutes.

`retry_after_s` for the temperature gate is a conservative estimate, not a
measured ramp: 1.0 C/s heating, 0.3 C/s cooling, rounded up so callers do not
hot-poll.

Two things that are **not** preconditions, despite sounding like them:

- **Plate presence.** The COM API has no plate sensor and no stage-position
  query. The stage interlock checks a *command-tracked* position that starts at
  `"unknown"` on process start, on shutdown, and after any failed or aborted
  move. Refusing safely beats guessing where the carriage is. A genuinely empty
  chamber is only discovered by the instrument during the cycle, and comes back
  as `last_error.code: "no_plate"`.
- **`degraded`.** A failing readback does not by itself withhold the run — only
  the fail-closed temperature branch does, and only because it cannot verify the
  band.

While `activity == "running"` the device advertises `["shutdown", "seal.stop"]`
only. Anything that would start a second cycle or move the carriage under a
lowered press is refused with **409** — a state conflict, not a precondition
(§6.1). The instrument itself says "Stage cannot move - press is down".

## The seal sequence

```
claim → startup → stage.in → seal.set_temperature → poll /status
      → seal.set_time → seal.start → stage.out → release
```

Between `set_temperature` and `start`, poll `/status` until
`components.heater.state == "stable"` (the service synthesizes that from
`metrics.temperature_delta_c` against `details.temperature_tolerance_c`; the
ActiveX has no native "ready to seal" signal) or equivalently until
`seal.start` appears in `allowed_actions`. `heater.state` is one of `heating`,
`cooling`, `stable`, `unknown`; `temperature_delta_c` is `actual − setpoint`, so
negative means still heating up.

`POST /control/seal/start` accepts optional `temperature_c` / `seconds` and
applies them before starting — but it does **not** wait for the ramp. Passing a
cold-to-hot `temperature_c` there sets the setpoint and then immediately 412s on
the temperature gate. Use it for the values you are already at; use
`seal/temperature` plus a poll when you need to move the setpoint.

`POST /control/seal/stop` is the abort class and the one control call honored
mid-cycle. It is idempotent — stopping when nothing runs is a 2xx no-op. Because
the in-flight `StartCycle` owns the COM channel, a stop issued mid-cycle is
serialised behind it and lands when that call returns.

## `last_error` (branch on the code, never the message)

`code` is always one of eleven values; `message` carries the driver's raw text,
with the ActiveX `GetLastError()` detail appended, because a bare HRESULT like
`0x80040201` is not actionable on its own.

| code | raised by |
|---|---|
| `low_air_pressure` | driver text "air pressure" |
| `no_plate` | driver text "no plate" |
| `vacuum_error` | driver text "vacuum" |
| `heater_overtemp` / `heater_undertemp` | driver text, incl. "did not reach setpoint" |
| `profile_not_found` | a `startup` whose text names the profile / available profiles |
| `com_timeout` | a `TimeoutError`, or "timeout"/"timed out" in the text |
| `stage_jam` | a failed `stage.in`/`stage.out` whose text is otherwise unhelpful |
| `com_init_failed` | a failed `startup` whose text is otherwise unhelpful |
| `com_other` | the catch-all |
| `process_internal` | a Python `KeyError`/`AttributeError`/`TypeError`/`NameError` — a bug in this service, file a ticket rather than reaching for the diagnostics dialog |

Severity is `error` for operational failures. A *readback* fault seen while
composing `/status` is classified through the same text taxonomy but reported
with `severity: "warning"`, because §2.2 already carries the safety consequence
by putting the top-level state at `degraded`, and a useful subset of capability
remains. An operational error always wins over a readback warning.

**Clearing (§6.4):** an operational `/control/*` endpoint clears `last_error`
only when its *overall* response is 2xx. A 412 is a refusal, not a recovery, and
does not clear. `claim`/`heartbeat`/`release` never clear — otherwise a
heartbeat-only retry loop would hide the fault. Reads never mutate anything.
Independently, the error ages out of the 60 s window on its own.

## Discovery

`/llms.txt` (this index), `/agent-docs` (this guide),
`/agent-docs/api-reference`, `/openapi.json`, `/docs` (Swagger UI).
