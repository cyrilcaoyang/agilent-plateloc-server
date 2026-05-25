"""
Configuration loader for the Agilent PlateLoc driver.

Reads ``config.toml`` from the project root (next to ``pyproject.toml``)
and exposes the values as module-level constants.

Users can also call :func:`load_config` with a custom path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

# Python 3.11+ has tomllib in the stdlib; for 3.10 we fall back to tomli
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


def _find_config_file() -> Path | None:
    """Walk up from this file to find ``config.toml``."""
    # Start from the package source directory and walk up
    here = Path(__file__).resolve().parent
    for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "config.toml"
        if candidate.is_file():
            return candidate
    # Also check CWD (common when running scripts)
    cwd = Path.cwd() / "config.toml"
    if cwd.is_file():
        return cwd
    return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load configuration from a TOML file.

    Parameters
    ----------
    path : str, Path, or None
        Path to the config file.  If ``None``, auto-discovers
        ``config.toml`` by walking up from the package directory
        or checking the current working directory.

    Returns
    -------
    dict
        Parsed TOML as a nested dictionary.

    Raises
    ------
    FileNotFoundError
        If no config file is found.
    RuntimeError
        If neither ``tomllib`` (3.11+) nor ``tomli`` is available.
    """
    if tomllib is None:
        raise RuntimeError(
            "No TOML parser available. "
            "Install tomli (`pip install tomli`) or use Python >= 3.11."
        )

    if path is None:
        found = _find_config_file()
        if found is None:
            raise FileNotFoundError(
                "config.toml not found. "
                "Place it next to pyproject.toml or pass an explicit path."
            )
        path = found

    path = Path(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Convenience: pre-load defaults so other modules can just import them.
# If the config file is missing, fall back to sensible defaults.
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "instrument": {
        "com_port": "COM14",
        "profile": "default",
    },
    "activex": {
        "progid": "PLATELOC.PlateLocCtrl.2",
        "typelib_clsid": "{19D95F7D-D76D-4B5B-B665-68C92511ADCF}",
        "typelib_major": 1,
        "typelib_minor": 0,
    },
    # REST API service (`python -m agilent_plateloc_server`)
    "service": {
        "host": "0.0.0.0",
        "port": 8000,
        # When true, the service uses an in-memory stub instead of touching
        # the ActiveX/COM. Useful for development on macOS/Linux and for CI.
        "dry_run": False,
        # CORS origins allowed by the API. Defaults to wildcard - this is
        # safe in v1 because access is gated by Tailscale ACLs, not auth.
        "cors_origins": ["*"],
        # If auto-connect at startup hangs (COM/ActiveX edge cases), give up
        # after this many seconds and leave the service in `requires_init`.
        "startup_connect_timeout_s": 15.0,
        # Layer-1 temperature interlock (see docs/INTERLOCKS.md). When true,
        # /control/seal/start refuses with HTTP 412 if the heater is outside
        # +/- temperature_tolerance_c of the setpoint. Default true.
        "enforce_temp_interlock": True,
        # Layer-1 stage interlock (v1.3.0). When true, /control/seal/start
        # refuses with HTTP 412 if the stage is not in the loaded position.
        # Independent of enforce_temp_interlock. Default true.
        "enforce_stage_interlock": True,
    },
    # Identity reported in /status; should match the entry in the dashboard's
    # `equipment.yaml`. equipment_kind is fixed at "plate_sealer".
    "dashboard": {
        "equipment_id": "plateloc",
        "equipment_name": "Agilent PlateLoc",
        "equipment_version": None,
    },
}

try:
    _cfg = load_config()
except (FileNotFoundError, RuntimeError):
    _cfg = _DEFAULTS


def get(section: str, key: str, default: Any = None) -> Any:
    """Get a config value with fallback to built-in defaults."""
    return _cfg.get(section, {}).get(key, _DEFAULTS.get(section, {}).get(key, default))


# ---------------------------------------------------------------------------
# Operator seal parameters (parameters.json)
# ---------------------------------------------------------------------------

_PARAMETERS: dict[str, Any] | None = None


def _find_parameters_file() -> Path | None:
    """
    Locate ``parameters.json`` next to ``config.toml`` or in the CWD.
    """
    cfg_path = _find_config_file()
    if cfg_path is not None:
        candidate = cfg_path.parent / "parameters.json"
        if candidate.is_file():
            return candidate
    cwd_candidate = Path.cwd() / "parameters.json"
    if cwd_candidate.is_file():
        return cwd_candidate
    return None


def load_parameters(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load operator-facing sealing parameters from ``parameters.json``.

    The JSON file should contain seal types with exact plate entries, e.g.::

        {
          "seal_types": [
            {
              "name": "Agilent Thin Clear Pierceable Film",
              "plates": [
                {
                  "name": "8R/12C PP Round Well Spherical Bottom (14mm)",
                  "temperature_c": 130,
                  "time_s": 3.0
                }
              ]
            }
          ]
        }
    """
    if path is None:
        found = _find_parameters_file()
        if found is None:
            return {}
        path = found

    path = Path(path)
    if not path.is_file():
        return {}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("parameters.json must contain a JSON object at the top level")
    return data


def get_seal_parameters(seal_name: str, plate_name: str) -> dict[str, float]:
    """
    Resolve sealing temperature/time for a seal type and exact plate type.
    """
    global _PARAMETERS

    if _PARAMETERS is None:
        _PARAMETERS = load_parameters()

    seal_types = _PARAMETERS.get("seal_types")
    if not isinstance(seal_types, list):
        raise ValueError("parameters.json must contain a 'seal_types' list")

    seal_match: dict[str, Any] | None = None
    for seal_type in seal_types:
        if not isinstance(seal_type, dict):
            continue
        if seal_type.get("name") == seal_name:
            seal_match = seal_type
            break

    if seal_match is None:
        raise ValueError(f"Seal type {seal_name!r} not found in parameters.json")

    plates = seal_match.get("plates")
    if not isinstance(plates, list):
        raise ValueError(f"Seal type {seal_name!r} must contain a 'plates' list")

    plate_match: dict[str, Any] | None = None
    for plate in plates:
        if not isinstance(plate, dict):
            continue
        if plate.get("name") == plate_name:
            plate_match = plate
            break

    if plate_match is None:
        raise ValueError(
            f"Plate type {plate_name!r} not found for seal type {seal_name!r}"
        )

    try:
        temperature_c = float(plate_match["temperature_c"])
        time_s = float(plate_match["time_s"])
    except KeyError as exc:
        raise ValueError(
            f"Missing {exc.args[0]!r} for seal={seal_name!r}, plate={plate_name!r}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid temperature/time for seal={seal_name!r}, plate={plate_name!r}"
        ) from exc

    return {
        "temperature_c": temperature_c,
        "time_s": time_s,
    }


# ---------------------------------------------------------------------------
# Film-specific sealing settings (film_settings.json)
# ---------------------------------------------------------------------------

_FILM_SETTINGS: dict[str, Any] | None = None


def _find_film_settings_file() -> Path | None:
    """
    Locate ``film_settings.json`` next to ``config.toml`` or in the CWD.
    """
    cfg_path = _find_config_file()
    if cfg_path is not None:
        candidate = cfg_path.parent / "film_settings.json"
        if candidate.is_file():
            return candidate
    cwd_candidate = Path.cwd() / "film_settings.json"
    if cwd_candidate.is_file():
        return cwd_candidate
    return None


def load_film_settings(path: str | Path | None = None) -> dict[str, Any]:
    """
    Load film-specific sealing settings from ``film_settings.json``.

    The JSON file should map a film name to its settings, e.g.::

        {
          "default": {
            "temperature_c": 170,
            "time_s": 1.2,
            "temperature_tolerance_c": 2,
            "heat_timeout_s": 120
          }
        }
    """
    if path is None:
        found = _find_film_settings_file()
        if found is None:
            return {}
        path = found

    path = Path(path)
    if not path.is_file():
        return {}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("film_settings.json must contain a JSON object at the top level")
    return data


def get_film_settings(name: str | None = None) -> dict[str, Any]:
    """
    Get sealing settings for a specific film.

    If *name* is None, the film name is read from ``config.toml``::

        [film]
        name = "default"
    """
    global _FILM_SETTINGS

    if _FILM_SETTINGS is None:
        _FILM_SETTINGS = load_film_settings()

    if not _FILM_SETTINGS:
        return {}

    if name is None:
        name = get("film", "name", "default")

    settings = _FILM_SETTINGS.get(name, {})
    if not isinstance(settings, dict):
        raise ValueError(f"Film settings for {name!r} must be a JSON object")
    return settings


_NUM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")


def _parse_first_number(value: str) -> float:
    """Extract the first numeric value from a string like '170 °C' or '1–1.2 sec'."""
    m = _NUM_RE.search(value)
    if not m:
        raise ValueError(f"Could not parse numeric value from {value!r}")
    return float(m.group(1))


def get_seal_params(film_name: str, plate_material: str) -> dict[str, float]:
    """
    Resolve sealing temperature/time for a given film + plate material.

    Expects ``film_settings.json`` with a structure like::

        {
          "seal_films": [
            {
              "name": "Peelable Aluminum",
              "product_number": "24210-001",
              "microplate_compatibility": {
                "polypropylene": { "temperature": "170 °C", "time": "1.2 sec" }
              }
            }
          ]
        }
    """
    data = load_film_settings()
    films = data.get("seal_films")
    if not isinstance(films, list):
        raise ValueError("film_settings.json must contain a 'seal_films' list")

    match: dict[str, Any] | None = None
    for film in films:
        if not isinstance(film, dict):
            continue
        if film.get("name") == film_name or film.get("product_number") == film_name:
            match = film
            break

    if match is None:
        raise ValueError(f"Film {film_name!r} not found in film_settings.json")

    compat = match.get("microplate_compatibility") or {}
    if not isinstance(compat, dict):
        raise ValueError(f"Film {film_name!r} has invalid microplate_compatibility block")

    mat = compat.get(plate_material)
    if mat is None or not isinstance(mat, dict):
        raise ValueError(f"{film_name!r} is not compatible with plate material {plate_material!r}")

    temp_str = str(mat.get("temperature", "")).strip()
    time_str = str(mat.get("time", "")).strip()

    if not temp_str or not time_str:
        raise ValueError(
            f"Missing temperature/time for film={film_name!r}, plate={plate_material!r}"
        )

    temperature_c = _parse_first_number(temp_str)
    time_s = _parse_first_number(time_str)

    return {
        "temperature_c": temperature_c,
        "time_s": time_s,
    }
