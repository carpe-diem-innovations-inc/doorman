"""The Start Menu shortcut - the way back in after Quit.

Standard library only, same rule as the rest of these modules.

‼ WHY THIS EXISTS: Quit used to be TERMINAL. The only relaunch path was the
logon Run entry, so quitting the tray app meant no codes until the next
sign-in - or a command line he should never have to type. This puts `doorman`
in Start: press `Win`, type "doorman", Enter.

‼ PER-USER (`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs`), never
All Users: the machine-wide Start Menu needs elevation, and this must not cost
`sudo`. Exactly the reasoning that put start-at-logon in `HKCU\\...\\Run`
rather than in a scheduled task.

A `.lnk` is a binary shell-link structure. Rather than hand-roll it, this
shells out ONCE to `WScript.Shell` - on demand when he ticks the box, never at
startup - so the app keeps its zero-runtime-dependency property.
"""

import os
import subprocess
import sys

SHORTCUT_NAME = os.environ.get("DOORMAN_SHORTCUT_NAME", "doorman.lnk")

# ‼ WINDOWS POWERSHELL 5.1 BY ABSOLUTE PATH, NOT `pwsh`, AND NOT BY NAME.
# Canon makes pwsh the estate's terminal, and that is right for the estate - but
# this call is made from inside a GUI process whose PATH is whatever explorer.exe
# inherited at logon, which is the same class of assumption that made the Run
# entry bake in `--store` instead of trusting an env var. `powershell.exe` at a
# fixed system32 path is present on every Windows install and cannot be missed.
_PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                   "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

# ‼ CREATE_NO_WINDOW, because canon's console-window trap applies here too: a
# console-subsystem binary spawned from a hidden GUI process gets a BRAND NEW
# window at default visibility. Unlike a scheduled task's `-WindowStyle Hidden`,
# this flag is passed in STARTUPINFO at creation and genuinely works.
_CREATE_NO_WINDOW = 0x08000000


def start_menu_dir():
    """The per-user Start Menu Programs folder."""
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")


def path(name=None):
    return os.path.join(start_menu_dir(), name or SHORTCUT_NAME)


def exists(name=None):
    return os.path.isfile(path(name))


def launch_command():
    """(exe, arguments) for the shortcut - pythonw, this app, and the live store.

    ‼ IDENTICAL REASONING TO `startup._command()`, AND THE SAME TWO TRAPS:
    `pythonw.exe` because `python.exe` is console-subsystem and would flash a
    window every time he launches from Start; and the store path baked in as
    `--store` because a shortcut carries no environment of its own either.
    """
    exe = sys.executable or ""
    base, leaf = os.path.split(exe)
    if leaf.lower() == "python.exe":
        cand = os.path.join(base, "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.abspath(os.path.join(here, "app.py"))
    args = '"%s"' % app
    try:
        import store as _store
        if _store.STORE_PATH:
            args += ' --store "%s"' % _store.STORE_PATH
    except Exception:
        pass
    return exe, args


def _q(value):
    """Quote for a PowerShell single-quoted string."""
    return "'" + str(value).replace("'", "''") + "'"


def create(name=None, icon=None, exe=None, args=None):
    """Write the shortcut. Returns its path; raises RuntimeError on failure.

    `icon` is optional and injected by the caller rather than generated here -
    drawing the padlock needs Pillow, and this module stays standard-library
    only. Without one the shortcut wears pythonw's icon, which works and is ugly.
    """
    lnk = path(name)
    target, arguments = launch_command()
    target = exe or target
    arguments = args if args is not None else arguments
    os.makedirs(os.path.dirname(lnk), exist_ok=True)

    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);"
        "$s.TargetPath=%s;"
        "$s.Arguments=%s;"
        "$s.WorkingDirectory=%s;"
        "$s.Description='doorman - sign-in codes in the tray';"
        % (_q(lnk), _q(target), _q(arguments), _q(os.path.dirname(os.path.abspath(target))))
    )
    if icon and os.path.exists(icon):
        script += "$s.IconLocation=%s;" % _q(icon)
    script += "$s.Save()"

    proc = subprocess.run(
        [_PS, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
    )
    # ‼ VERIFY THE ARTIFACT, NOT THE EXIT CODE. Canon: never write DONE before
    # the verifying read - a COM call that reports success and leaves no file is
    # exactly the shape this estate keeps paying for.
    if not os.path.isfile(lnk):
        raise RuntimeError("shortcut not created (rc=%s): %s"
                           % (proc.returncode, (proc.stderr or proc.stdout or "").strip()))
    return lnk


def remove(name=None):
    """Delete the shortcut. True if one was removed, False if none was there.

    An already-absent shortcut is success, not an error - same contract as
    `startup.disable()`.
    """
    lnk = path(name)
    try:
        os.remove(lnk)
        return True
    except OSError:
        return False


def toggle(name=None, icon=None):
    if exists(name):
        remove(name)
        return False
    create(name, icon=icon)
    return True


if __name__ == "__main__":
    probe = "doorman_selftest_DELETEME.lnk"
    failures = []

    exe, args = launch_command()
    print("target           %s" % exe)
    print("arguments        %s" % args)
    if "pythonw.exe" not in exe.lower():
        failures.append("target is not pythonw: %r" % exe)
    if "app.py" not in args:
        failures.append("arguments do not name app.py: %r" % args)

    print("start menu       %s" % start_menu_dir())
    if not os.path.isdir(start_menu_dir()):
        failures.append("start menu dir does not exist: %s" % start_menu_dir())

    try:
        if exists(probe):
            failures.append("probe shortcut already present")
        made = create(probe)
        print("created          %s (%d bytes)" % (made, os.path.getsize(made)))
        if not exists(probe):
            failures.append("create() returned but exists() is False")
        if os.path.getsize(made) < 200:
            failures.append("shortcut is implausibly small: %d bytes" % os.path.getsize(made))

        if not remove(probe):
            failures.append("remove() reported no change on a present shortcut")
        if exists(probe):
            failures.append("remove() left the shortcut behind")
        print("removed          OK")

        if remove(probe):
            failures.append("remove() of an absent shortcut reported a change")
        print("remove absent    idempotent OK")
    except Exception as exc:                                    # noqa: BLE001
        failures.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        remove(probe)

    print()
    print("shortcut FAILED: " + "; ".join(failures) if failures else "shortcut OK")
    raise SystemExit(1 if failures else 0)
