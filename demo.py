"""
demo.py — Agilent PlateLoc driver demonstration
=================================================

Run this after you have created a profile in the Diagnostics dialog
(with the correct COM port).  Change PROFILE below to match.

Usage:
    .venv\\Scripts\\python.exe demo.py
"""

import logging
import time

from agilent_plateloc_server import PlateLoc
from agilent_plateloc_server.plateloc import PlateLocError
from agilent_plateloc_server.config import (
    get as cfg,
    get_seal_parameters,
    load_parameters,
)

# ── Configuration (loaded from config.toml) ────────────────────────
PROFILE   = cfg("instrument", "profile",  "default")
COM_PORT  = cfg("instrument", "com_port", "COM4")
TEMP_TOL_C  = int(cfg("film", "temperature_tolerance_c", 2))
HEAT_TIMEOUT = int(cfg("film", "heat_timeout_s", 120))
# ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


def _choose_seal_and_plate() -> tuple[str, str]:
    """Let the user pick a seal type and exact plate type from parameters.json."""
    data = load_parameters()
    seal_types = data.get("seal_types") or []
    if not isinstance(seal_types, list) or not seal_types:
        raise SystemExit("No seal_types defined in parameters.json")

    print()
    print("=" * 50)
    print("Available seal types:")
    for idx, seal_type in enumerate(seal_types, start=1):
        name = seal_type.get("name", "<unnamed>")
        print(f"  [{idx}] {name}")
    print("=" * 50)

    raw = input(f"Select seal type [1-{len(seal_types)}] (default 1): ").strip()
    if not raw:
        seal_idx = 1
    else:
        seal_idx = int(raw)
        if seal_idx < 1 or seal_idx > len(seal_types):
            raise SystemExit("Invalid seal type selection")

    seal_type = seal_types[seal_idx - 1]
    seal_name = seal_type.get("name")
    if not isinstance(seal_name, str) or not seal_name:
        raise SystemExit("Selected seal type is missing a name")

    plates = seal_type.get("plates") or []
    if not isinstance(plates, list) or not plates:
        raise SystemExit(f"No plates defined for seal type {seal_name!r}")

    print()
    print("Available plate types for this seal:")
    for idx, plate in enumerate(plates, start=1):
        name = plate.get("name", "<unnamed>")
        temp = plate.get("temperature_c", "?")
        time_s = plate.get("time_s", "?")
        print(f"  [{idx}] {name} ({temp} C, {time_s} s)")

    raw = input(f"Select plate type [1-{len(plates)}] (default 1): ").strip()
    if not raw:
        plate_idx = 1
    else:
        plate_idx = int(raw)
        if plate_idx < 1 or plate_idx > len(plates):
            raise SystemExit("Invalid plate type selection")

    plate = plates[plate_idx - 1]
    plate_name = plate.get("name")
    if not isinstance(plate_name, str) or not plate_name:
        raise SystemExit("Selected plate type is missing a name")
    return seal_name, plate_name


def _prompt_int_setting(label: str, default: int, min_value: int, max_value: int) -> int:
    """Prompt for an integer setting, using default on blank input."""
    while True:
        raw = input(
            f"{label} [{min_value}-{max_value}] (default {default}): "
        ).strip()
        if not raw:
            return default

        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue

        if min_value <= value <= max_value:
            return value
        print(f"Please enter a value from {min_value} to {max_value}.")


def _prompt_float_setting(
    label: str,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    """Prompt for a floating-point setting, using default on blank input."""
    while True:
        raw = input(
            f"{label} [{min_value:.1f}-{max_value:.1f}] "
            f"(default {default:.1f}): "
        ).strip()
        if not raw:
            return default

        try:
            value = float(raw)
        except ValueError:
            print("Please enter a number.")
            continue

        if min_value <= value <= max_value:
            return value
        print(f"Please enter a value from {min_value:.1f} to {max_value:.1f}.")


def _customize_seal_params(default_temp_c: int, default_time_s: float) -> tuple[int, float]:
    """Allow operator override of film-derived sealing parameters."""
    print()
    print("=" * 50)
    print("Seal parameters")
    print("=" * 50)
    print(f"Recommended temperature : {default_temp_c} C")
    print(f"Recommended seal time   : {default_time_s:.1f} s")
    print("Press ENTER to use each recommended value, or type a custom value.")
    print()

    seal_temp_c = _prompt_int_setting("Sealing temperature (C)", default_temp_c, 20, 235)
    seal_time_s = _prompt_float_setting("Sealing time (s)", default_time_s, 0.5, 12.0)
    return seal_temp_c, seal_time_s


def _wait_for_temperature_ready(sealer: PlateLoc, target_c: int) -> None:
    """Block until the plate is within tolerance, or abort before cycling."""
    log.info(
        "Waiting for plate to reach %d C (+/- %d C). Press Ctrl+C to abort ...",
        target_c,
        TEMP_TOL_C,
    )
    try:
        for _ in range(HEAT_TIMEOUT):
            act = sealer.get_actual_temperature()
            delta = act - target_c
            if abs(delta) <= TEMP_TOL_C:
                log.info("Plate ready at %d C", act)
                return

            direction = "cooling" if delta > 0 else "heating"
            log.info("  %s ... %d C", direction, act)
            time.sleep(1)
    except KeyboardInterrupt:
        log.warning("Temperature wait aborted by operator; seal cycle will not start")
        sealer.close()
        raise SystemExit(1) from None

    actual_c = sealer.get_actual_temperature()
    log.error(
        "Timed out waiting for plate temperature; seal cycle will not start "
        "(actual=%d C, target=%d C, tolerance=%d C)",
        actual_c,
        target_c,
        TEMP_TOL_C,
    )
    sealer.close()
    raise SystemExit(1)


def main() -> None:
    sealer = PlateLoc(com_port=COM_PORT)

    # ── 1. Connect ──────────────────────────────────────────────────
    log.info("Connecting to PlateLoc (profile=%r) ...", PROFILE)
    sealer.connect(profile=PROFILE)
    log.info("Connected!")

    # ── 2. Device info ──────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  Agilent PlateLoc -- Device Info")
    print("=" * 50)
    print(f"  ActiveX version  : {sealer.get_version()}")
    print(f"  Firmware version : {sealer.get_firmware_version()}")
    print(f"  Cycle count      : {sealer.get_cycle_count()}")
    print(f"  Profiles         : {sealer.enumerate_profiles()}")
    print("=" * 50)
    print()

    # ── 3. Let user choose seal type / plate type ───────────────────
    seal_name, plate_name = _choose_seal_and_plate()
    params = get_seal_parameters(seal_name, plate_name)
    recommended_temp_c = int(params["temperature_c"])
    recommended_time_s = float(params["time_s"])
    seal_temp_c, seal_time_s = _customize_seal_params(
        recommended_temp_c,
        recommended_time_s,
    )

    log.info(
        "Using seal=%r, plate=%r -> temp=%d C, time=%.2f s",
        seal_name,
        plate_name,
        seal_temp_c,
        seal_time_s,
    )

    # ── 4. Read current settings ────────────────────────────────────
    cur_temp = sealer.get_sealing_temperature()
    cur_time = sealer.get_sealing_time()
    act_temp = sealer.get_actual_temperature()
    log.info("Current settings -> temp=%d C, time=%.1f s", cur_temp, cur_time)
    log.info("Actual plate temperature: %d C", act_temp)

    # ── 5. Configure seal parameters ────────────────────────────────
    log.info("Setting temperature to %d C ...", seal_temp_c)
    sealer.set_sealing_temperature(seal_temp_c)

    log.info("Setting seal time to %.1f s ...", seal_time_s)
    sealer.set_sealing_time(seal_time_s)

    # Confirm
    log.info(
        "Confirmed -> temp=%d C, time=%.1f s",
        sealer.get_sealing_temperature(),
        sealer.get_sealing_time(),
    )

    # ── 6. Wait for temperature to stabilise ────────────────────────
    _wait_for_temperature_ready(sealer, seal_temp_c)

    # ── 7. Run a seal cycle ─────────────────────────────────────────
    input("\n>>> Press ENTER to start a seal cycle (load plate first!) <<<\n")

    log.info("Starting seal cycle ...")
    try:
        sealer.start_cycle()
        log.info("Seal cycle complete!")
    except PlateLocError:
        err_msg = sealer.get_last_error()
        log.error("Seal cycle FAILED: %s", err_msg)
        log.info("Aborting current operation ...")
        try:
            sealer.abort()
            log.info("Abort sent successfully")
        except PlateLocError:
            pass  # abort may also fail if already cleared

    # ── 8. Read post-cycle info ─────────────────────────────────────
    log.info("Post-cycle temperature: %d C", sealer.get_actual_temperature())
    log.info("Total cycle count: %d", sealer.get_cycle_count())
    last_err = sealer.get_last_error()
    if last_err:
        log.warning("Last error: %s", last_err)

    # ── 9. Disconnect ───────────────────────────────────────────────
    sealer.close()
    log.info("Done -- PlateLoc disconnected.")


if __name__ == "__main__":
    main()
