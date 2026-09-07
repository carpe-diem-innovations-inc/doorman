"""One live instance, and a way for a second launch to surface the first.

Standard library only - `ctypes` against kernel32, no pywin32 - because the
dependency-free property of this module set is what makes the C# port cheap.

‼ WHY THIS EXISTS: doorman became reachable TWO ways - the logon Run entry and
the Start Menu shortcut - so a double launch is now ordinary user behaviour
rather than a mistake. Without a guard that produces TWO padlocks in the tray
reading the same store, and quitting one leaves the other behind. Measured
during the 0.2 work: two `pythonw.exe` processes, two icons, from exactly the
sequence a shortcut invites.

Two named kernel objects, both in the per-logon-session namespace:

    Local\\doorman.instance   a MUTEX. Holding it means "I am the live one."
    Local\\doorman.show       an EVENT. Setting it means "surface the window."

‼ `Local\\` and NOT `Global\\`, deliberately: one instance PER LOGON SESSION is
the right granularity for a per-user tray app holding per-user secrets, and
`Global\\` would want privileges this app must never need. It also means a
second logged-in user gets their own doorman, which is correct.
"""

import ctypes
import threading
from ctypes import wintypes

MUTEX_NAME = r"Local\doorman.instance"
SHOW_EVENT_NAME = r"Local\doorman.show"

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF
EVENT_MODIFY_STATE = 0x0002

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.SetEvent.argtypes = (wintypes.HANDLE,)
_k32.SetEvent.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL

_held = []                      # handles kept for the process lifetime, never GC'd


def acquire(name=None):
    """True if THIS process is now the live instance; False if another holds it.

    ‼ A failure to CREATE the mutex at all returns True, not False. A guard
    that cannot be established must never become the reason the app refuses to
    start - the worst outcome of failing open is two icons; the worst outcome
    of failing closed is a tray app that silently will not run. Same reasoning
    as `startup.disable()` treating an already-absent value as success.
    """
    h = _k32.CreateMutexW(None, True, name or MUTEX_NAME)
    err = ctypes.get_last_error()
    if not h:
        return True                                 # fail OPEN - see docstring
    if err == ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(h)
        return False
    _held.append(h)
    return True


def signal_show(name=None):
    """Ask the live instance to show its window. True if the signal was sent.

    False means nobody is listening - which the caller should read as "no live
    instance to surface", not as an error.
    """
    h = _k32.OpenEventW(EVENT_MODIFY_STATE, False, name or SHOW_EVENT_NAME)
    if not h:
        return False
    try:
        return bool(_k32.SetEvent(h))
    finally:
        _k32.CloseHandle(h)


def watch_show(callback, name=None):
    """Run `callback` every time another launch asks us to surface. Daemon thread.

    ‼ The event is AUTO-RESET, so each signal fires exactly once and there is
    no state to clear. The callback is expected to marshal onto the UI thread
    itself (`root.after`) - this thread must never touch tkinter directly.
    """
    h = _k32.CreateEventW(None, False, False, name or SHOW_EVENT_NAME)
    if not h:
        return None
    _held.append(h)

    def loop():
        while True:
            if _k32.WaitForSingleObject(h, INFINITE) != WAIT_OBJECT_0:
                return
            try:
                callback()
            except Exception:
                pass                    # a bad callback must not kill the watcher

    t = threading.Thread(target=loop, daemon=True, name="doorman-show-watch")
    t.start()
    return t


if __name__ == "__main__":
    import time

    failures = []
    probe_mutex = r"Local\doorman.selftest.instance"
    probe_event = r"Local\doorman.selftest.show"

    first = acquire(probe_mutex)
    print("first acquire    %s" % first)
    if not first:
        failures.append("first acquire returned False - a stale handle is held")

    # ‼ A SECOND CreateMutexW IN THE SAME PROCESS ALSO REPORTS ERROR_ALREADY_EXISTS,
    # which is what makes the guard testable without spawning a second process.
    second = acquire(probe_mutex)
    print("second acquire   %s" % second)
    if second:
        failures.append("second acquire returned True - the guard does not guard")

    seen = threading.Event()
    t = watch_show(seen.set, probe_event)
    print("watcher          %s" % ("started" if t else "FAILED to start"))
    if not t:
        failures.append("watch_show did not start")
    else:
        time.sleep(0.2)                             # let the wait arm
        sent = signal_show(probe_event)
        print("signal sent      %s" % sent)
        if not sent:
            failures.append("signal_show could not open the event")
        if not seen.wait(3):
            failures.append("the watcher never fired within 3 s")
        else:
            print("watcher fired    OK")

    # signalling an event nobody created must be a clean False, never a crash
    if signal_show(r"Local\doorman.selftest.nobody"):
        failures.append("signal_show claimed success with no listener")
    print("no listener      clean False OK")

    print()
    print("instance FAILED: " + "; ".join(failures) if failures else "instance OK")
    raise SystemExit(1 if failures else 0)
