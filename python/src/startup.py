"""Start at logon, via the per-user Run key. Standard library only.

`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` is the right home for this:
it is PER-USER, so enabling it needs no elevation, and it is removed by deleting one
value. A scheduled task would be the estate's normal instrument, but a task is
Administrators-write and this is a checkbox in a tray app - it must not cost `sudo`.

‼ It launches `pythonw.exe`, never `python.exe`. Canon's own trap: `python.exe` is a
CONSOLE-subsystem binary, so conhost creates a window BEFORE the interpreter parses
its arguments - a console would flash on his screen at every logon.
"""

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = os.environ.get("DOORMAN_RUN_VALUE", "doorman")


def _command(store_path=None):
    """The exact command the Run key should hold: pythonw + this app + the live store.

    ‼ The store path is BAKED IN as `--store` rather than left to `DOORMAN_STORE`.
    A logon entry is launched by explorer.exe, which inherited its environment at
    boot - so an env var set afterwards is invisible to it, and the app would
    silently start against an empty default store. An explicit argument cannot
    be lost that way. Measured 2026-09-07.
    """
    exe = sys.executable or ""
    base, name = os.path.split(exe)
    if name.lower() == "python.exe":
        cand = os.path.join(base, "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.abspath(os.path.join(here, "app.py"))
    cmd = '"%s" "%s"' % (exe, app)
    if store_path is None:
        try:
            import store as _store
            store_path = _store.STORE_PATH
        except Exception:
            store_path = None
    if store_path:
        cmd += ' --store "%s"' % store_path
    return cmd


def read(value_name=None):
    """Return the stored command, or None. Never raises on a missing key."""
    name = value_name or VALUE_NAME
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def is_enabled(value_name=None):
    return read(value_name) is not None


def enable(value_name=None, command=None):
    name = value_name or VALUE_NAME
    cmd = command or _command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
    return cmd


def disable(value_name=None):
    name = value_name or VALUE_NAME
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except OSError:
        return False                        # already absent is success, not an error


def toggle(value_name=None):
    if is_enabled(value_name):
        disable(value_name)
        return False
    enable(value_name)
    return True


if __name__ == "__main__":
    probe = "doorman_selftest_DELETEME"
    failures = []

    cmd = _command()
    print("command          %s" % cmd)
    if "pythonw.exe" not in cmd.lower():
        failures.append("command does not use pythonw: %r" % cmd)
    if '"app.py"' not in cmd.replace("\\", "/").replace("/app.py", '"app.py"'):
        if "app.py" not in cmd:
            failures.append("command does not name app.py: %r" % cmd)
    if "--store" not in cmd:
        failures.append("command does not bake in --store, so a logon launch would "
                        "use the default store: %r" % cmd)

    try:
        print("initially        %s" % is_enabled(probe))
        if is_enabled(probe):
            failures.append("probe value already present")

        enable(probe, 'X:\\fake\\pythonw.exe app.py')
        print("after enable     %s -> %r" % (is_enabled(probe), read(probe)))
        if not is_enabled(probe):
            failures.append("enable did not stick")

        on = toggle(probe)
        print("toggle           enabled=%s" % on)
        if on or is_enabled(probe):
            failures.append("toggle did not turn it off")

        if disable(probe):
            failures.append("disable of an absent value reported a change")
        print("disable absent   idempotent OK")
    finally:
        disable(probe)

    print()
    print("startup FAILED: " + "; ".join(failures) if failures else "startup OK")
    raise SystemExit(1 if failures else 0)
