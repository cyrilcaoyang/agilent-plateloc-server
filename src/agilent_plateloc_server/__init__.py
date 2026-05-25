"""
Agilent PlateLoc Thermal Microplate Sealer - Python Driver
==========================================================

Control the PlateLoc Sealer via its VWorks ActiveX COM interface from Python.

The ActiveX DLL is 32-bit, so this package uses a 32-bit COM surrogate
subprocess to bridge the gap when running under 64-bit Python.

All instrument-specific values (COM port, profile name, ActiveX ProgID,
type library CLSID, sealing defaults) are read from ``config.toml``.

Usage::

    from agilent_plateloc_server import PlateLoc

    sealer = PlateLoc()                   # reads com_port from config.toml
    sealer.connect()                      # reads profile from config.toml
    sealer.set_sealing_temperature(170)
    sealer.set_sealing_time(3.0)
    sealer.start_cycle()
    temp = sealer.get_actual_temperature()
    sealer.close()
"""

from .plateloc import PlateLoc

__all__ = ["PlateLoc"]
__version__ = "1.3.1"

# The service / FastAPI app live in `agilent_plateloc_server.service` and
# `agilent_plateloc_server.api` and are imported on demand to avoid pulling in
# FastAPI/uvicorn for callers that only want the driver. Import them
# explicitly when needed::
#
#     from agilent_plateloc_server.api import create_app
#     from agilent_plateloc_server.service import PlateLocService
