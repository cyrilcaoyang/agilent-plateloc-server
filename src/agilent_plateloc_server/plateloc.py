"""
PlateLoc Thermal Microplate Sealer - Python driver.

Provides a high-level interface to the PlateLoc Sealer through
the Agilent VWorks ActiveX COM control.

Since the ActiveX DLL (AgilentPlateLoc.dll) is 32-bit only,
this driver supports two modes:

1. **Direct mode** — when running under 32-bit Python, the COM
   object is created in-process.
2. **Surrogate mode** — when running under 64-bit Python, a
   32-bit Python subprocess is launched to host the COM object,
   and commands are sent via JSON-over-pipe.

If neither works (e.g. no 32-bit Python available), the driver
falls back to attempting direct COM instantiation, which will
raise a clear error.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from . import config as _config

# Default ProgID for the PlateLoc ActiveX control
_PROGID = _config.get("activex", "progid", "PLATELOC.PlateLocCtrl.2")

# Type library identifiers (used for early-bound COM wrappers)
_TYPELIB_CLSID = _config.get("activex", "typelib_clsid", "{19D95F7D-D76D-4B5B-B665-68C92511ADCF}")
_TYPELIB_MAJOR = _config.get("activex", "typelib_major", 1)
_TYPELIB_MINOR = _config.get("activex", "typelib_minor", 0)

# Common 32-bit Python locations on Windows
_PYTHON32_CANDIDATES = [
    r"C:\Python310-32\python.exe",
    r"C:\Python311-32\python.exe",
    r"C:\Python312-32\python.exe",
    r"C:\Python39-32\python.exe",
    r"C:\Python38-32\python.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311-32\python.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe"),
]


def _is_64bit() -> bool:
    """Return True if running under 64-bit Python."""
    return struct.calcsize("P") * 8 == 64


def _find_python32() -> str | None:
    """Try to locate a 32-bit Python interpreter."""
    for path in _PYTHON32_CANDIDATES:
        if os.path.isfile(path):
            return path
    # Try 'py -3-32' launcher
    try:
        result = subprocess.run(
            ["py", "-3-32", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            exe = result.stdout.strip()
            if os.path.isfile(exe):
                return exe
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


class PlateLocError(Exception):
    """Raised when the PlateLoc returns an error."""


class PlateLoc:
    """
    High-level driver for the Agilent PlateLoc Thermal Microplate Sealer.

    Parameters
    ----------
    com_port : str
        The COM port the PlateLoc is connected to (e.g. ``"COM14"``).
        This is used to identify / create the correct profile.
    progid : str, optional
        The ActiveX ProgID. Defaults to ``"PLATELOC.PlateLocCtrl.2"``.
    python32_path : str or None, optional
        Path to a 32-bit Python interpreter. If ``None``, the driver
        will auto-detect one when needed.
    blocking : bool
        If ``True`` (default), all methods block until the operation
        completes. If ``False``, methods return immediately and you
        must handle events / poll status.

    Examples
    --------
    >>> sealer = PlateLoc(com_port="COM14")
    >>> sealer.connect()
    >>> sealer.set_sealing_temperature(170)
    >>> sealer.set_sealing_time(3.0)
    >>> sealer.start_cycle()
    >>> print(sealer.get_actual_temperature())
    >>> sealer.close()
    """

    def __init__(
        self,
        com_port: str | None = None,
        progid: str = _PROGID,
        python32_path: str | None = None,
        blocking: bool = True,
    ):
        self.com_port = com_port or _config.get("instrument", "com_port", "COM14")
        self.progid = progid
        self.python32_path = python32_path
        self.blocking = blocking

        # Internal state
        self._proc: subprocess.Popen | None = None  # surrogate process
        self._com_obj: Any = None  # direct COM object
        self._mode: str | None = None  # "direct" or "surrogate"
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, profile: str | None = None) -> None:
        """
        Connect to the PlateLoc Sealer.

        This creates the ActiveX COM object, configures blocking mode,
        and calls ``Initialize`` with the given profile name.

        Parameters
        ----------
        profile : str or None
            The profile name configured in the PlateLoc Diagnostics
            dialog. The profile contains the COM port setting.
            If ``None``, reads from ``config.toml`` (``instrument.profile``).
            Use :meth:`enumerate_profiles` or :meth:`show_diags_dialog`
            to configure profiles.
        """
        if profile is None:
            profile = _config.get("instrument", "profile", "default")
        self._create_com_object()

        # Set blocking mode
        self._send("set_blocking", [self.blocking])

        # Initialize with the profile
        result = self._send("initialize", [profile])
        if isinstance(result, (int, float)) and result != 0:
            # Fetch the human-readable reason before raising
            try:
                detail = self._send("get_last_error")
            except Exception:
                detail = "(could not retrieve)"
            profiles = []
            try:
                profiles = self._send("enumerate_profiles")
            except Exception:
                pass
            raise PlateLocError(
                f"Initialize('{profile}') failed (code {result}).\n"
                f"  Last error : {detail or '(none)'}\n"
                f"  Available profiles: {profiles}\n"
                f"  Hint: open the Diagnostics dialog to create / fix the profile:\n"
                f"    sealer._create_com_object()\n"
                f"    sealer.show_diags_dialog(modal=True, security_level=0)"
            )
        self._connected = True
        logger.info("Connected to PlateLoc on %s (profile=%r)", self.com_port, profile)

    def close(self) -> None:
        """Disconnect from the PlateLoc and release resources."""
        if self._mode == "surrogate" and self._proc:
            try:
                self._send("close")
                self._send("quit")
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        elif self._mode == "direct" and self._com_obj:
            try:
                self._com_obj.Close()
            except Exception:
                pass
            self._com_obj = None
            # Destroy AtlAxWin host windows
            import win32gui

            for attr in ("_hwnd_ax", "_hwnd_parent"):
                hwnd = getattr(self, attr, None)
                if hwnd:
                    try:
                        win32gui.DestroyWindow(hwnd)
                    except Exception:
                        pass
                    setattr(self, attr, None)
        self._connected = False
        self._mode = None
        logger.info("Disconnected from PlateLoc")

    # ------------------------------------------------------------------
    # Sealing operations
    # ------------------------------------------------------------------

    def set_sealing_temperature(self, temperature: int) -> int:
        """
        Set the sealing temperature in °C.

        Parameters
        ----------
        temperature : int
            Desired temperature, 20–235 °C.

        Returns
        -------
        int
            0 if successful.
        """
        if not 20 <= temperature <= 235:
            raise ValueError(f"Temperature must be 20–235 °C, got {temperature}")
        result = self._send("set_sealing_temperature", [temperature])
        self._check_result(result, "SetSealingTemperature")
        logger.info("Sealing temperature set to %d °C", temperature)
        return result

    def set_sealing_time(self, seconds: float) -> int:
        """
        Set the seal-cycle duration in seconds.

        Parameters
        ----------
        seconds : float
            Desired duration, 0.5–12.0 s.

        Returns
        -------
        int
            0 if successful.
        """
        if not 0.5 <= seconds <= 12.0:
            raise ValueError(f"Sealing time must be 0.5–12.0 s, got {seconds}")
        result = self._send("set_sealing_time", [seconds])
        self._check_result(result, "SetSealingTime")
        logger.info("Sealing time set to %.1f s", seconds)
        return result

    def start_cycle(self) -> int:
        """
        Start a seal cycle.

        Returns
        -------
        int
            0 if successful.
        """
        result = self._send("start_cycle")
        self._check_result(result, "StartCycle")
        logger.info("Seal cycle started")
        return result

    def stop_cycle(self) -> int:
        """
        Stop the currently running seal cycle.

        Returns
        -------
        int
            0 if successful.
        """
        result = self._send("stop_cycle")
        self._check_result(result, "StopCycle")
        logger.info("Seal cycle stopped")
        return result

    def apply_seal(self) -> int:
        """
        Apply the seal to the microplate and keep the door closed.

        Returns
        -------
        int
            0 if successful.
        """
        result = self._send("apply_seal")
        self._check_result(result, "ApplySeal")
        logger.info("Seal applied")
        return result

    # ------------------------------------------------------------------
    # Stage control
    # ------------------------------------------------------------------

    def move_stage_in(self) -> int:
        """
        Move the plate stage into the sealing chamber.

        Returns
        -------
        int
            0 if successful.
        """
        result = self._send("move_stage_in")
        self._check_result(result, "MoveStageIn")
        return result

    def move_stage_out(self) -> int:
        """
        Move the plate stage out of the sealing chamber.

        Returns
        -------
        int
            0 if successful.
        """
        result = self._send("move_stage_out")
        self._check_result(result, "MoveStageOut")
        return result

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_actual_temperature(self) -> int:
        """
        Get the current hot plate temperature in °C.

        Returns
        -------
        int
            Current temperature in °C.
        """
        return self._send("get_actual_temperature")

    def get_sealing_temperature(self) -> int:
        """
        Get the configured sealing temperature in °C.

        Returns
        -------
        int
            Configured sealing temperature in °C.
        """
        return self._send("get_sealing_temperature")

    def get_sealing_time(self) -> float:
        """
        Get the configured seal-cycle duration in seconds.

        Returns
        -------
        float
            Configured sealing time in seconds.
        """
        return self._send("get_sealing_time")

    def get_cycle_count(self) -> int:
        """
        Get the total number of seal cycles performed (odometer).

        Returns
        -------
        int
            Number of seal cycles.
        """
        return self._send("get_cycle_count")

    def get_firmware_version(self) -> str:
        """
        Get the PlateLoc firmware version.

        Returns
        -------
        str
            Firmware version string.
        """
        return self._send("get_firmware_version")

    def get_version(self) -> str:
        """
        Get the ActiveX control version.

        Returns
        -------
        str
            ActiveX control version string.
        """
        return self._send("get_version")

    def get_last_error(self) -> str:
        """
        Get the last error message.

        Returns
        -------
        str
            Last error description.
        """
        return self._send("get_last_error")

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def enumerate_profiles(self) -> list[str]:
        """
        List all configured profiles.

        Returns
        -------
        list of str
            Available profile names.
        """
        return self._send("enumerate_profiles")

    def show_diags_dialog(self, modal: bool = True, security_level: int = 0) -> int:
        """
        Show the PlateLoc Diagnostics dialog.

        Use this to create/edit profiles and configure COM port settings.

        Parameters
        ----------
        modal : bool
            If ``True``, the dialog blocks until closed.
        security_level : int
            Access level: 0=Admin, 1=Technician, 2=Operator, 3=Guest, -1=None.

        Returns
        -------
        int
            0 if successful.
        """
        return self._send("show_diags_dialog", [modal, security_level])

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def abort(self) -> int:
        """Abort the current task in error state and clear the error."""
        result = self._send("abort")
        self._check_result(result, "Abort")
        return result

    def retry(self) -> int:
        """Retry the last action after an error."""
        result = self._send("retry")
        self._check_result(result, "Retry")
        return result

    def ignore_error(self) -> int:
        """Ignore the last error and proceed to the next step."""
        result = self._send("ignore")
        self._check_result(result, "Ignore")
        return result

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> PlateLoc:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"PlateLoc(com_port={self.com_port!r}, status={status!r})"

    # ------------------------------------------------------------------
    # Internal: COM object creation & communication
    # ------------------------------------------------------------------

    def _create_com_object(self) -> None:
        """Create the ActiveX COM object (direct or via surrogate)."""
        if _is_64bit():
            # Try surrogate mode (32-bit subprocess)
            python32 = self.python32_path or _find_python32()
            if python32:
                logger.info(
                    "64-bit Python detected. Using 32-bit surrogate: %s", python32
                )
                self._start_surrogate(python32)
                return

            # No 32-bit Python found — try direct anyway (may fail)
            logger.warning(
                "64-bit Python detected but no 32-bit Python found. "
                "Attempting direct COM instantiation (may fail for 32-bit DLL)."
            )

        # Direct mode
        self._create_direct()

    def _create_direct(self) -> None:
        """Create the COM object directly in-process via AtlAxWin hosting.

        The PlateLoc ActiveX control is a *visual* control and must be
        hosted inside an AtlAxWin window container — a plain
        ``Dispatch()`` will create the object but all method calls
        return E_UNEXPECTED.
        """
        import ctypes
        import ctypes.wintypes

        import pythoncom
        import win32api
        import win32com.client
        import win32con
        import win32gui

        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)

        # Load ATL for AtlAxWin hosting
        atl = None
        for lib_name in ("atl", "atl110", "atl100", "atl90", "atl80", "atl71"):
            try:
                atl = ctypes.windll.LoadLibrary(lib_name)
                atl.AtlAxWinInit()
                break
            except (OSError, AttributeError):
                atl = None

        if atl is None:
            raise PlateLocError(
                "Could not load ATL library — "
                "AtlAxWin hosting is required for the PlateLoc ActiveX control."
            )

        # Hidden parent window
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "PlateLocDirectHost"
        wc.lpfnWndProc = {
            win32con.WM_DESTROY: lambda *a: win32gui.PostQuitMessage(0),
        }
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass

        self._hwnd_parent = win32gui.CreateWindow(
            "PlateLocDirectHost", "PlateLoc Host",
            0, 0, 0, 100, 100,
            0, 0, win32api.GetModuleHandle(None), None,
        )

        try:
            self._hwnd_ax = win32gui.CreateWindow(
                "AtlAxWin", self.progid,
                win32con.WS_CHILD,
                0, 0, 100, 100,
                self._hwnd_parent, 0,
                win32api.GetModuleHandle(None), None,
            )

            punk = ctypes.c_void_p()
            hr = atl.AtlAxGetControl(self._hwnd_ax, ctypes.byref(punk))
            if hr != 0 or not punk.value:
                raise PlateLocError(
                    f"AtlAxGetControl failed (hr=0x{hr:08X}). "
                    f"Is '{self.progid}' registered?"
                )

            idisp = pythoncom.ObjectFromAddress(
                punk.value, pythoncom.IID_IDispatch,
            )
            # Use early-bound wrapper for correct ByRef param handling
            try:
                import win32com.client.gencache
                win32com.client.gencache.EnsureModule(
                    _TYPELIB_CLSID, 0, _TYPELIB_MAJOR, _TYPELIB_MINOR,
                )
                self._com_obj = win32com.client.CastTo(
                    win32com.client.Dispatch(idisp), '_DPlateLoc',
                )
            except Exception:
                self._com_obj = win32com.client.Dispatch(idisp)

        except PlateLocError:
            raise
        except Exception as e:
            raise PlateLocError(
                f"Failed to create COM object '{self.progid}'. "
                f"Error: {e}"
            ) from e

        self._mode = "direct"
        logger.info("COM object created (direct/AtlAxWin mode): %s", self.progid)

    def _start_surrogate(self, python32: str) -> None:
        """Launch the 32-bit COM surrogate subprocess."""
        server_script = str(Path(__file__).parent / "_com_server.py")

        self._proc = subprocess.Popen(
            [python32, server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Wait for ready signal
        ready_line = self._proc.stdout.readline()
        if not ready_line:
            stderr = self._proc.stderr.read()
            raise PlateLocError(
                f"32-bit surrogate failed to start. "
                f"Python: {python32}\nStderr: {stderr}"
            )

        ready = json.loads(ready_line)
        if not ready.get("ok"):
            raise PlateLocError(f"Surrogate startup error: {ready.get('error')}")

        self._mode = "surrogate"
        logger.info("32-bit surrogate started (pid=%d)", self._proc.pid)

        # Create the COM object in the surrogate, passing typelib info for early binding
        resp = self._send_surrogate({
            "cmd": "create",
            "args": [self.progid, _TYPELIB_CLSID, _TYPELIB_MAJOR, _TYPELIB_MINOR],
        })
        if not resp.get("ok"):
            raise PlateLocError(f"Failed to create COM object: {resp.get('error')}")

    def _send(self, cmd: str, args: list | None = None) -> Any:
        """Send a command and return the result."""
        if self._mode == "surrogate":
            resp = self._send_surrogate({"cmd": cmd, "args": args or []})
            if not resp.get("ok"):
                raise PlateLocError(f"{cmd} failed: {resp.get('error')}")
            return resp.get("result")
        elif self._mode == "direct":
            return self._send_direct(cmd, args or [])
        else:
            raise PlateLocError("Not connected. Call connect() first.")

    def _send_surrogate(self, request: dict) -> dict:
        """Send a JSON command to the 32-bit surrogate process."""
        if not self._proc or self._proc.poll() is not None:
            raise PlateLocError("Surrogate process is not running")

        line = json.dumps(request) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        response_line = self._proc.stdout.readline()
        if not response_line:
            stderr = self._proc.stderr.read()
            raise PlateLocError(f"Surrogate returned no response. Stderr: {stderr}")

        return json.loads(response_line)

    def _send_direct(self, cmd: str, args: list) -> Any:
        """Execute a command directly on the COM object."""
        obj = self._com_obj
        if obj is None:
            raise PlateLocError("COM object not created")

        method_map = {
            "set_blocking": lambda: setattr(obj, "Blocking", bool(args[0])),
            "initialize": lambda: obj.Initialize(args[0] if args else "default"),
            "close": lambda: obj.Close(),
            "enumerate_profiles": lambda: list(obj.EnumerateProfiles() or []),
            "get_actual_temperature": lambda: obj.GetActualTemperature(),
            "get_sealing_temperature": lambda: obj.GetSealingTemperature(),
            "set_sealing_temperature": lambda: obj.SetSealingTemperature(int(args[0])),
            "get_sealing_time": lambda: obj.GetSealingTime(),
            "set_sealing_time": lambda: obj.SetSealingTime(float(args[0])),
            "start_cycle": lambda: obj.StartCycle(),
            "stop_cycle": lambda: obj.StopCycle(),
            "apply_seal": lambda: obj.ApplySeal(),
            "move_stage_in": lambda: obj.MoveStageIn(),
            "move_stage_out": lambda: obj.MoveStageOut(),
            "abort": lambda: obj.Abort(),
            "retry": lambda: obj.Retry(),
            "ignore": lambda: obj.Ignore(),
            "get_firmware_version": lambda: obj.GetFirmwareVersion(),
            "get_version": lambda: obj.GetVersion(),
            "get_last_error": lambda: obj.GetLastError(),
            "get_cycle_count": lambda: obj.GetCycleCount(),
            "show_diags_dialog": lambda: obj.ShowDiagsDialog(
                bool(args[0]) if args else True,
                int(args[1]) if len(args) > 1 else 0,
            ),
        }

        fn = method_map.get(cmd)
        if fn is None:
            raise PlateLocError(f"Unknown command: {cmd}")
        return fn()

    @staticmethod
    def _check_result(result: Any, method_name: str) -> None:
        """Raise if the ActiveX method returned a nonzero error code."""
        if isinstance(result, (int, float)) and result != 0:
            raise PlateLocError(
                f"{method_name} returned error code {result}. "
                f"Call get_last_error() for details."
            )
