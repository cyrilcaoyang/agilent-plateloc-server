"""
32-bit COM surrogate server for the PlateLoc ActiveX control.

This script is designed to be run by a 32-bit Python interpreter.
It hosts the PlateLoc ActiveX control inside a hidden AtlAxWin
window (required for visual ActiveX controls) and exposes a simple
JSON-over-stdin/stdout protocol so the main (64-bit) process can
send commands and receive results.

The main thread runs a Windows message pump (required by the
ActiveX control), while a background thread reads requests from
stdin and posts them to the main thread via a thread-safe queue.

Protocol (one JSON object per line):
  Request:  {"cmd": "method_name", "args": [...]}
  Response: {"ok": true, "result": ...}  or  {"ok": false, "error": "..."}

This file is an internal implementation detail and should not be
imported directly.
"""

import ctypes
import ctypes.wintypes
import json
import queue
import sys
import threading
import time
import traceback


def main():
    import pythoncom
    import win32api
    import win32com.client
    import win32com.client.gencache
    import win32con
    import win32gui

    # Initialise COM as STA — required for ActiveX controls
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)

    # --- Load ATL and register the AtlAxWin window class ---------------
    atl = None
    for lib_name in ("atl", "atl110", "atl100", "atl90", "atl80", "atl71"):
        try:
            atl = ctypes.windll.LoadLibrary(lib_name)
            atl.AtlAxWinInit()
            break
        except (OSError, AttributeError):
            atl = None

    if atl is None:
        _fatal("Could not load ATL library — AtlAxWin hosting unavailable")

    # --- Create a hidden host window -----------------------------------
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "PlateLocSurrogateHost"
    wc.lpfnWndProc = {
        win32con.WM_DESTROY: lambda hwnd, msg, wp, lp: win32gui.PostQuitMessage(0),
    }
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass  # class may already be registered from a previous run

    hwnd_parent = win32gui.CreateWindow(
        "PlateLocSurrogateHost", "PlateLoc COM Host",
        0, 0, 0, 100, 100,
        0, 0, win32api.GetModuleHandle(None), None,
    )

    # Shared state
    plateloc = None
    hwnd_ax = None
    progid = "PLATELOC.PlateLocCtrl.2"
    typelib_clsid = "{19D95F7D-D76D-4B5B-B665-68C92511ADCF}"
    typelib_major = 1
    typelib_minor = 0

    # Request queue: background stdin reader → main thread
    req_queue: queue.Queue = queue.Queue()
    shutdown = threading.Event()

    # ------------------------------------------------------------------
    # Request handler (runs on the main/STA thread)
    # ------------------------------------------------------------------
    def handle(request: dict) -> dict:
        nonlocal plateloc, hwnd_ax, progid, typelib_clsid, typelib_major, typelib_minor
        cmd = request.get("cmd", "")
        args = request.get("args", [])

        try:
            # -- lifecycle ---
            if cmd == "create":
                # args: [progid, typelib_clsid, typelib_major, typelib_minor]
                if args:
                    progid = args[0]
                if len(args) > 1 and args[1]:
                    typelib_clsid = args[1]
                if len(args) > 2 and args[2] is not None:
                    typelib_major = int(args[2])
                if len(args) > 3 and args[3] is not None:
                    typelib_minor = int(args[3])

                hwnd_ax = win32gui.CreateWindow(
                    "AtlAxWin", progid,
                    win32con.WS_CHILD,
                    0, 0, 100, 100,
                    hwnd_parent, 0,
                    win32api.GetModuleHandle(None), None,
                )
                # Retrieve the hosted IDispatch interface
                punk = ctypes.c_void_p()
                hr = atl.AtlAxGetControl(hwnd_ax, ctypes.byref(punk))
                if hr != 0 or not punk.value:
                    return {"ok": False, "error": f"AtlAxGetControl failed (hr={hr})"}
                idisp = pythoncom.ObjectFromAddress(
                    punk.value, pythoncom.IID_IDispatch,
                )
                # Use early-bound wrapper so ByRef output params work
                # (GetCycleCount, GetActualTemperature, etc.)
                try:
                    win32com.client.gencache.EnsureModule(
                        typelib_clsid, 0, typelib_major, typelib_minor,
                    )
                    plateloc = win32com.client.CastTo(
                        win32com.client.Dispatch(idisp), '_DPlateLoc',
                    )
                except Exception:
                    # Fall back to late-bound if gencache fails
                    plateloc = win32com.client.Dispatch(idisp)
                return {"ok": True, "result": f"Created {progid}"}

            if plateloc is None:
                return {"ok": False, "error": "COM object not created yet"}

            # Helper: the underlying OLE object for InvokeTypes calls.
            # Several PlateLoc methods use ByRef parameters; late-bound
            # Dispatch cannot infer the types, so we call InvokeTypes
            # directly with the type codes from the generated type library.
            #
            # Type constants (from the .tlb / makepy output):
            #   VT_I2    = 2      VT_I4    = 3      VT_R4  = 5
            #   VT_BSTR  = 8      VT_BOOL  = 11     VT_VARIANT = 12
            #   VT_BYREF = 0x4000 (16384)
            ole = plateloc._oleobj_
            CYCNT   = 0  # LCID

            # -- properties ---
            if cmd == "set_blocking":
                plateloc.Blocking = bool(args[0])
                return {"ok": True, "result": None}

            # -- init / close ---
            if cmd == "initialize":
                profile = args[0] if args else "default"
                # dispid 3, returns VT_I4, param VT_BSTR
                res = ole.InvokeTypes(3, CYCNT, 1, (3, 0), ((8, 0),), profile)
                return {"ok": True, "result": res}

            if cmd == "close":
                # dispid 4, returns VT_I4, no params
                res = ole.InvokeTypes(4, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            # -- profiles ---
            if cmd == "enumerate_profiles":
                # dispid 18, returns VT_VARIANT
                profiles = plateloc.EnumerateProfiles()
                return {"ok": True, "result": list(profiles) if profiles else []}

            # -- temperature (ByRef VT_I2 = 16386) ---
            # InvokeTypes with ByRef returns (hresult, byref_value)
            if cmd == "get_actual_temperature":
                # dispid 5, returns VT_I4, param ByRef VT_I2
                res = ole.InvokeTypes(5, CYCNT, 1, (3, 0), ((16386, 0),), 0)
                return {"ok": True, "result": res[1] if isinstance(res, (list, tuple)) else res}

            if cmd == "get_sealing_temperature":
                # dispid 6, returns VT_I4, param ByRef VT_I2
                res = ole.InvokeTypes(6, CYCNT, 1, (3, 0), ((16386, 0),), 0)
                return {"ok": True, "result": res[1] if isinstance(res, (list, tuple)) else res}

            if cmd == "set_sealing_temperature":
                # dispid 7, returns VT_I4, param ByRef VT_I2
                res = ole.InvokeTypes(7, CYCNT, 1, (3, 0), ((16386, 0),), int(args[0]))
                return {"ok": True, "result": res[0] if isinstance(res, (list, tuple)) else res}

            # -- sealing time (ByRef VT_R8 = 16389) ---
            if cmd == "get_sealing_time":
                # dispid 8, returns VT_I4, param ByRef VT_R8
                res = ole.InvokeTypes(8, CYCNT, 1, (3, 0), ((16389, 0),), 0.0)
                return {"ok": True, "result": res[1] if isinstance(res, (list, tuple)) else res}

            if cmd == "set_sealing_time":
                # dispid 9, returns VT_I4, param ByRef VT_R8
                res = ole.InvokeTypes(9, CYCNT, 1, (3, 0), ((16389, 0),), float(args[0]))
                return {"ok": True, "result": res[0] if isinstance(res, (list, tuple)) else res}

            # -- cycle control ---
            if cmd == "start_cycle":
                # dispid 10
                res = ole.InvokeTypes(10, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            if cmd == "stop_cycle":
                # dispid 11
                res = ole.InvokeTypes(11, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            if cmd == "apply_seal":
                # dispid 24
                res = ole.InvokeTypes(24, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            # -- stage ---
            if cmd == "move_stage_in":
                # dispid 23
                res = ole.InvokeTypes(23, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            if cmd == "move_stage_out":
                # dispid 22
                res = ole.InvokeTypes(22, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            # -- error handling ---
            if cmd == "abort":
                # dispid 19
                res = ole.InvokeTypes(19, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            if cmd == "retry":
                # dispid 20
                res = ole.InvokeTypes(20, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            if cmd == "ignore":
                # dispid 21
                res = ole.InvokeTypes(21, CYCNT, 1, (3, 0), ())
                return {"ok": True, "result": res}

            # -- info ---
            if cmd == "get_firmware_version":
                # dispid 16, returns VT_BSTR
                return {"ok": True, "result": ole.InvokeTypes(16, CYCNT, 1, (8, 0), ())}

            if cmd == "get_version":
                # dispid 15, returns VT_BSTR
                return {"ok": True, "result": ole.InvokeTypes(15, CYCNT, 1, (8, 0), ())}

            if cmd == "get_last_error":
                # dispid 12, returns VT_BSTR
                return {"ok": True, "result": ole.InvokeTypes(12, CYCNT, 1, (8, 0), ())}

            if cmd == "get_cycle_count":
                # dispid 17, returns VT_I4, param ByRef VT_I4 (16387)
                res = ole.InvokeTypes(17, CYCNT, 1, (3, 0), ((16387, 0),), 0)
                return {"ok": True, "result": res[1] if isinstance(res, (list, tuple)) else res}

            # -- UI ---
            if cmd == "show_diags_dialog":
                modal = bool(args[0]) if args else True
                sec = int(args[1]) if len(args) > 1 else 0
                # dispid 14, params VT_BOOL + VT_I2
                res = ole.InvokeTypes(14, CYCNT, 1, (3, 0), ((11, 0), (2, 0)), modal, sec)
                return {"ok": True, "result": res}

            # -- housekeeping ---
            if cmd == "ping":
                return {"ok": True, "result": "pong"}

            if cmd == "quit":
                return {"ok": True, "result": "bye"}

            return {"ok": False, "error": f"Unknown command: {cmd}"}

        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    # Background stdin reader
    # ------------------------------------------------------------------
    def stdin_reader():
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    request = {"_parse_error": str(exc)}
                req_queue.put(request)
                if request.get("cmd") == "quit":
                    break
        except Exception:
            pass
        finally:
            shutdown.set()
            # Unblock the message loop
            ctypes.windll.user32.PostQuitMessage(0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def write_response(resp: dict):
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    # Signal ready
    write_response({"ok": True, "result": "ready"})

    # Start stdin reader thread
    reader = threading.Thread(target=stdin_reader, daemon=True)
    reader.start()

    # ------------------------------------------------------------------
    # Main message loop
    # ------------------------------------------------------------------
    QS_ALLINPUT = 0x04FF
    PM_REMOVE = 0x0001
    msg = ctypes.wintypes.MSG()

    while not shutdown.is_set():
        # Drain request queue
        while True:
            try:
                request = req_queue.get_nowait()
            except queue.Empty:
                break

            if "_parse_error" in request:
                write_response({"ok": False, "error": f"Invalid JSON: {request['_parse_error']}"})
                continue

            response = handle(request)
            write_response(response)

            if request.get("cmd") == "quit":
                shutdown.set()
                break

        if shutdown.is_set():
            break

        # Wait for messages or a 100 ms timeout (to re-check the queue)
        ctypes.windll.user32.MsgWaitForMultipleObjects(
            0, None, False, 100, QS_ALLINPUT,
        )

        # Pump pending Windows messages
        while ctypes.windll.user32.PeekMessageW(
            ctypes.byref(msg), None, 0, 0, PM_REMOVE,
        ):
            if msg.message == 0x0012:  # WM_QUIT
                shutdown.set()
                break
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    if plateloc:
        try:
            plateloc.Close()
        except Exception:
            pass

    if hwnd_ax:
        try:
            win32gui.DestroyWindow(hwnd_ax)
        except Exception:
            pass
    try:
        win32gui.DestroyWindow(hwnd_parent)
    except Exception:
        pass

    pythoncom.CoUninitialize()


def _fatal(msg: str):
    """Write an error and exit."""
    sys.stdout.write(json.dumps({"ok": False, "error": msg}) + "\n")
    sys.stdout.flush()
    sys.exit(1)


if __name__ == "__main__":
    main()
