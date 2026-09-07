"""doorman - a tray icon that opens a small window of sign-in codes. Click one to copy.

Deliberately not a framework: one window, one update loop, no settings screen,
no cloud, no telemetry. Secrets live in B:\\safe\\ wrapped with DPAPI (store.py);
UI state lives in %LOCALAPPDATA%\\doorman (prefs.py) so a reorder never needs sudo.

    python src/app.py                    run in the tray
    python src/app.py --import FILE      add accounts from otpauth / otpauth-migration URIs
    python src/app.py --import-qr IMG..  read the export QR straight off screenshots
    python src/app.py --list             print stored account names (never the secrets)
    python src/app.py --smoke            headless check of the window logic
"""

import argparse
import ctypes
import os
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw
import pystray

import import_ga
import instance
import prefs
import shortcut
import startup
import store
import totp

PERIOD_DEFAULT = 30
BG = "#1b1b1f"
FG = "#f1f1ee"
MUTED = "#8b9196"
ACCENT = "#4f8cc9"
COPIED = "#5fb35f"
ROW_BG = "#24242a"
ROW_DRAG = "#33333c"
DRAG_THRESHOLD = 6          # px before a press becomes a drag rather than a click

TRAY_ATTEMPTS = 5           # Shell_NotifyIcon can lose a race with the shell at logon
TRAY_RETRY_SEC = 2.0
LOG_MAX_BYTES = 64 * 1024


# --------------------------------------------------------------- startup log

LOG_PATH = os.path.join(os.path.dirname(prefs.PREFS_PATH), "startup.log")


def _log(msg, exc=False):
    """Append one stamped line to the startup log. Never raises.

    ‼‼ THIS IS THE FIX FOR A BUG THAT HAD NO EVIDENCE, AND THAT ABSENCE WAS THE
    REAL DEFECT. He logged out, logged back in, and doorman was not there. The
    Run value was correct and still is; the entry was not disabled in
    `StartupApproved`; the identical command launches and survives when run by
    hand. But under `pythonw.exe` there is NO CONSOLE, and a Python exception
    produces no Windows Error Reporting entry either - so a startup failure at
    logon left nothing anywhere: no icon, no window, no log, no crash dump.
    ‼ THE APP WAS UNFALSIFIABLE. "It did not launch" and "it launched and died"
    are different faults pointing at different places, and nothing on the box
    could tell them apart.

    Canon's own rule - a deliberately visible failure beats a confident wrong
    answer, the same instrument as `!! PULSE COMPUTE EMPTY` - applied to a GUI
    process that has no stderr to fail into.
    """
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # bounded, because a log that grows forever is a second defect
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (stamp, msg))
            if exc:
                fh.write(traceback.format_exc())
    except Exception:
        pass                    # logging must never be the thing that breaks it


# ------------------------------------------------------- where the window sits

def virtual_screen():
    """The whole virtual desktop as (left, top, width, height) - ALL monitors.

    ‼‼ TK CANNOT ANSWER THIS AND THE OBVIOUS CALL IS WRONG. `winfo_screenwidth()`
    and `winfo_screenheight()` report the PRIMARY MONITOR ONLY, so validating a
    saved position against them would drag the window off his second or third
    display and back onto the first - "fixing" a position that was never broken.
    He runs three panels, so that is the normal case here, not an edge case.

    ‼ AND COORDINATES GO NEGATIVE: a monitor placed left of or above the primary
    one has negative x/y on Windows. Any check that assumes 0,0 is the top-left
    of the desktop is wrong on an ordinary layout.

    `SM_*VIRTUALSCREEN` covers the union of every monitor. Returns None if the
    call fails, which the caller must read as "cannot judge" and leave the saved
    position alone rather than discard it.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        rect = (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if rect[2] <= 0 or rect[3] <= 0:
            return None
        return rect
    except Exception:
        return None


def position_reachable(pos, margin=80):
    """Is a saved (x, y) still somewhere he could grab the window?

    `margin` is how much of the window's top-left must fall inside the desktop -
    enough that the title area is on screen and draggable. A position that fails
    this is DISCARDED rather than clamped: he moved that window somewhere
    deliberately, and quietly relocating it is worse than letting Tk place it
    fresh, because a clamped position looks like the app forgot.

    ‼ Returns True when the desktop cannot be measured. Unknown is not a reason
    to throw away his saved position.
    """
    if not pos:
        return False
    rect = virtual_screen()
    if rect is None:
        return True
    left, top, width, height = rect
    x, y = pos
    return (left - margin) <= x <= (left + width - margin) and \
           (top - margin) <= y <= (top + height - margin)


def _fatal(msg):
    """Log a startup failure AND put it on his screen, because a tray app that
    dies silently is indistinguishable from one that was never started."""
    _log("FATAL " + msg, exc=True)
    try:
        from tkinter import messagebox
        messagebox.showerror(
            "doorman could not start",
            "%s\n\nDetails were written to:\n%s" % (msg, LOG_PATH),
        )
    except Exception:
        pass


# ----------------------------------------------------------------- tray icon

def _icon_image():
    """A padlock, drawn rather than shipped so the repo carries no asset."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    steel = (79, 140, 201, 255)
    dark = (27, 27, 31, 255)
    d.arc((19, 6, 45, 38), start=180, end=360, fill=steel, width=7)      # shackle
    d.rounded_rectangle((12, 28, 52, 58), radius=7, fill=steel)          # body
    d.ellipse((28, 36, 36, 44), fill=dark)                               # keyhole
    d.polygon([(30, 42), (34, 42), (33, 52), (31, 52)], fill=dark)
    return img


def _icon_file():
    """Write the padlock out as a real `.ico` so the Start Menu shortcut wears it.

    ‼ The repo still ships no asset - this is GENERATED into
    `%LOCALAPPDATA%\\doorman\\` on demand, the same padlock `_icon_image()`
    draws for the tray. `shortcut.py` stays standard-library only and takes the
    path as an argument, so the drawing dependency lives here where Pillow
    already is. Returns None on failure; a shortcut with pythonw's icon is ugly
    and works, which is not worth failing over.
    """
    try:
        target = os.path.join(os.path.dirname(prefs.PREFS_PATH), "doorman.ico")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _icon_image().save(target, format="ICO",
                           sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
        return target
    except Exception:
        _log("could not write the .ico for the shortcut", exc=True)
        return None


# --------------------------------------------------------------------- window

class Window:
    def __init__(self, root, accounts, ui=None):
        self.root = root
        self.ui = ui if ui is not None else prefs.load()
        self.accounts = prefs.apply_order(accounts, self.ui)
        self.rows = []
        self._flash_jobs = {}
        self._press_y = None
        self._press_row = None
        self._dragged = False

        root.title("doorman")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.attributes("-topmost", bool(self.ui.get("topmost", True)))
        # ‼ -toolwindow is what keeps it OUT of the taskbar entirely: tray only,
        # which is the point of a tray app. It also drops the min/max buttons.
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass                                    # non-Windows: harmless

        self._pos_job = None
        self.restore_position()
        # ‼ SAVE ON MOVE, DEBOUNCED - not only on exit. He asked for "saves its
        # coordinates on exit", and exit alone would lose the position to a
        # logoff, a crash, or a kill, which is the case a tray app hits most.
        # <Configure> fires on every drag step, so the write is deferred and
        # coalesced rather than run per pixel.
        root.bind("<Configure>", self._position_changed)

        self.mono = tkfont.Font(family="Consolas", size=20, weight="bold")
        self.name_font = tkfont.Font(family="Segoe UI", size=9)
        self.issuer_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.btn_font = tkfont.Font(family="Segoe UI", size=9)

        wrap = tk.Frame(root, bg=BG, padx=10, pady=8)
        wrap.pack(fill="both", expand=True)

        self._build_header(wrap)

        self.body = tk.Frame(wrap, bg=BG)
        self.body.pack(fill="both", expand=True)

        if not accounts:
            tk.Label(
                self.body, bg=BG, fg=MUTED, font=self.name_font, justify="left",
                text="No accounts yet.\n\nExport from Google Authenticator\n"
                     "(Transfer accounts \u2192 Export), screenshot it, then:\n\n"
                     "  app.py --import-qr shot.png",
            ).pack(padx=6, pady=6)
            return

        for acct in self.accounts:
            self.rows.append(self._build_row(acct))
        self._repack()
        self.tick()

    # ------------------------------------------------------------- chrome

    def _build_header(self, parent):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", pady=(0, 6))

        self.sort_btn = tk.Label(bar, text="A\u2192Z", bg=ROW_BG, fg=FG, font=self.btn_font,
                                 padx=8, pady=3, cursor="hand2")
        self.sort_btn.pack(side="left")
        self.sort_btn.bind("<Button-1>", lambda _e: self.sort_by_name())

        self.lock_btn = tk.Label(bar, bg=ROW_BG, fg=FG, font=self.btn_font,
                                 padx=8, pady=3, cursor="hand2")
        self.lock_btn.pack(side="left", padx=(6, 0))
        self.lock_btn.bind("<Button-1>", lambda _e: self.toggle_lock())

        self.logon_btn = tk.Label(bar, bg=ROW_BG, fg=FG, font=self.btn_font,
                                  padx=8, pady=3, cursor="hand2")
        self.logon_btn.pack(side="left", padx=(6, 0))
        self.logon_btn.bind("<Button-1>", lambda _e: self.toggle_logon())

        self.shortcut_btn = tk.Label(bar, bg=ROW_BG, fg=FG, font=self.btn_font,
                                     padx=8, pady=3, cursor="hand2")
        self.shortcut_btn.pack(side="left", padx=(6, 0))
        self.shortcut_btn.bind("<Button-1>", lambda _e: self.toggle_shortcut())

        # right-hand pair. Minimize exists because -toolwindow removes the titlebar's
        # own minimize button, so without this the only way back to the tray is the X.
        self.min_btn = tk.Label(bar, text="–", bg=ROW_BG, fg=MUTED, font=self.btn_font,
                                padx=9, pady=3, cursor="hand2")
        self.min_btn.pack(side="right")
        self.min_btn.bind("<Button-1>", lambda _e: self.hide())

        self.top_btn = tk.Label(bar, bg=ROW_BG, fg=FG, font=self.btn_font,
                                padx=8, pady=3, cursor="hand2")
        self.top_btn.pack(side="right", padx=(6, 6))
        self.top_btn.bind("<Button-1>", lambda _e: self.toggle_topmost())

        self._refresh_chrome()

    def _refresh_chrome(self):
        locked = self.ui.get("locked")
        self.lock_btn.config(text="\U0001f512 locked" if locked else "\U0001f513 unlocked",
                             fg=COPIED if locked else MUTED)
        custom = self.ui.get("sort") == "custom"
        self.sort_btn.config(fg=MUTED if custom else ACCENT)
        # \u203c read the REGISTRY, never a cached flag - if he removes the Run value by
        # hand the checkbox must tell the truth on the next open.
        on = startup.is_enabled()
        self.logon_btn.config(text=("\u2611" if on else "\u2610") + " logon",
                              fg=COPIED if on else MUTED)
        # \u203c same rule: read the FILESYSTEM, not a cached flag. If he deletes the
        # shortcut from Start by hand the checkbox must tell the truth next time.
        sc = shortcut.exists()
        self.shortcut_btn.config(text=("\u2611" if sc else "\u2610") + " in Start",
                                 fg=COPIED if sc else MUTED)
        top = bool(self.ui.get("topmost", True))
        self.top_btn.config(text="\U0001f4cc on top" if top else "\U0001f4cc off",
                            fg=ACCENT if top else MUTED)

    def _build_row(self, acct):
        row = tk.Frame(self.body, bg=ROW_BG, padx=12, pady=8, cursor="hand2")

        left = tk.Frame(row, bg=ROW_BG)
        left.pack(side="left", fill="x", expand=True)

        issuer = acct.get("issuer") or acct.get("name") or "(unnamed)"
        name = acct.get("name") or ""
        tk.Label(left, text=issuer, bg=ROW_BG, fg=FG, font=self.issuer_font, anchor="w").pack(fill="x")
        if name and name != issuer:
            tk.Label(left, text=name, bg=ROW_BG, fg=MUTED, font=self.name_font, anchor="w").pack(fill="x")

        right = tk.Frame(row, bg=ROW_BG)
        right.pack(side="right")
        code_label = tk.Label(right, text="------", bg=ROW_BG, fg=ACCENT, font=self.mono)
        code_label.pack(side="left", padx=(14, 10))
        countdown = tk.Canvas(right, width=22, height=22, bg=ROW_BG, highlightthickness=0)
        countdown.pack(side="left")

        entry = {"acct": acct, "code": code_label, "arc": countdown,
                 "frame": row, "raw": "------", "kids": [left, right, code_label, countdown]}

        for w in [row, left, right, code_label, countdown] + list(left.winfo_children()):
            w.bind("<Button-1>", lambda e, r=entry: self._press(e, r))
            w.bind("<B1-Motion>", lambda e, r=entry: self._motion(e, r))
            w.bind("<ButtonRelease-1>", lambda e, r=entry: self._release(e, r))
        return entry

    def _repack(self):
        for r in self.rows:
            r["frame"].pack_forget()
        for r in self.rows:
            r["frame"].pack(fill="x", pady=3)

    def _tint(self, entry, colour):
        entry["frame"].config(bg=colour)
        for w in entry["kids"]:
            try:
                w.config(bg=colour)
            except tk.TclError:
                pass
        for w in entry["kids"]:
            for kid in getattr(w, "winfo_children", lambda: [])():
                try:
                    kid.config(bg=colour)
                except tk.TclError:
                    pass

    # ---------------------------------------------------- click vs drag

    def _press(self, event, entry):
        self._press_y = event.y_root
        self._press_row = entry
        self._dragged = False

    def _motion(self, event, entry):
        """A press only becomes a drag past the threshold - otherwise every copy-click
        with a twitch in it would reorder the list."""
        if self.ui.get("locked") or self._press_y is None:
            return
        if not self._dragged and abs(event.y_root - self._press_y) < DRAG_THRESHOLD:
            return
        if not self._dragged:
            self._dragged = True
            self._tint(entry, ROW_DRAG)

        target = self._index_at(event.y_root)
        current = self.rows.index(entry)
        if target is not None and target != current:
            self.rows.insert(target, self.rows.pop(current))
            self._repack()

    def _release(self, event, entry):
        if self._dragged:
            self._tint(entry, ROW_BG)
            self.ui["sort"] = "custom"
            self.ui["order"] = [prefs.key(r["acct"]) for r in self.rows]
            prefs.save(self.ui)
            self._refresh_chrome()
        elif self._press_row is entry:
            self.copy(entry)
        self._press_y = None
        self._press_row = None
        self._dragged = False

    def _index_at(self, y_root):
        for i, r in enumerate(self.rows):
            f = r["frame"]
            top = f.winfo_rooty()
            if top <= y_root <= top + f.winfo_height():
                return i
        return None

    # ------------------------------------------------------------ actions

    def sort_by_name(self):
        self.ui["sort"] = "name"
        prefs.save(self.ui)
        self.accounts = prefs.apply_order([r["acct"] for r in self.rows], self.ui)
        order = {prefs.key(a): i for i, a in enumerate(self.accounts)}
        self.rows.sort(key=lambda r: order[prefs.key(r["acct"])])
        self._repack()
        self._refresh_chrome()

    def toggle_lock(self):
        self.ui["locked"] = not self.ui.get("locked")
        prefs.save(self.ui)
        self._refresh_chrome()

    def toggle_logon(self):
        """Add or remove the per-user Run value. No elevation, no prefs entry -
        the registry IS the state, so there is nothing to keep in sync."""
        startup.toggle()
        self._refresh_chrome()

    def toggle_shortcut(self):
        """Add or remove the Start Menu shortcut - the way back in after Quit.

        No prefs entry: the shortcut file IS the state, so there is nothing to
        keep in sync. Same contract as the logon toggle above.
        """
        try:
            shortcut.toggle(icon=_icon_file())
        except Exception as exc:                                # noqa: BLE001
            _log("shortcut toggle failed: %s" % exc, exc=True)
            try:
                from tkinter import messagebox
                messagebox.showerror("doorman", "Could not change the Start Menu "
                                                "shortcut:\n\n%s" % exc)
            except Exception:
                pass
        self._refresh_chrome()

    def toggle_topmost(self):
        want = not bool(self.ui.get("topmost", True))
        self.ui["topmost"] = want
        prefs.save(self.ui)
        self.root.attributes("-topmost", want)
        self._refresh_chrome()

    def copy(self, entry):
        if not entry["raw"] or entry["raw"].startswith("-"):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(entry["raw"])
        self.root.update_idletasks()

        entry["code"].config(fg=COPIED)
        i = id(entry)
        job = self._flash_jobs.pop(i, None)
        if job:
            self.root.after_cancel(job)
        self._flash_jobs[i] = self.root.after(900, lambda: entry["code"].config(fg=ACCENT))

    def tick(self):
        for row in self.rows:
            acct = row["acct"]
            period = int(acct.get("period", PERIOD_DEFAULT))
            try:
                value = totp.code(acct["secret"], digits=int(acct.get("digits", 6)),
                                  period=period, algorithm=acct.get("algorithm", "SHA1"))
            except Exception:
                row["raw"] = ""
                row["code"].config(text="error", fg="#b35f5f")
                continue

            row["raw"] = value
            half = len(value) // 2
            if row["code"].cget("fg") != COPIED:
                row["code"].config(text=f"{value[:half]} {value[half:]}", fg=ACCENT)

            left = totp.seconds_left(period)
            arc = row["arc"]
            arc.delete("all")
            arc.create_oval(3, 3, 19, 19, outline=MUTED, width=2)
            arc.create_arc(3, 3, 19, 19, start=90, extent=-(360 * (left / period)),
                           style="arc", outline=ACCENT if left > 5 else "#c9884f", width=3)

        self.root.after(200, self.tick)

    def show(self):
        # ‼ THE POSITION IS APPLIED HERE, AND A CONSTRUCTION-TIME CALL ALONE WAS
        # NOT ENOUGH - MEASURED 2026-09-07. `main()` withdraws the root BEFORE the
        # Window is built, so the geometry request made during `__init__` belongs
        # to a window that has never been mapped, and it did not survive the first
        # map: with `pos` = [1444, 377] on disk and correctly judged reachable, the
        # window still appeared at 2131,289 - which is exactly the horizontal
        # CENTRE of his 5120-wide desktop, i.e. a default placement, not our value.
        # Setting it immediately before `deiconify()` is the moment that holds, and
        # it costs nothing because `restore_position()` is a no-op when nothing is
        # saved.
        self.restore_position()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.save_position()                # BEFORE withdrawing - see save_position
        self.root.withdraw()

    # ------------------------------------------------------ window position

    def _position_changed(self, _event=None):
        """Debounce a move into one write ~700 ms after he stops dragging."""
        if self._pos_job is not None:
            try:
                self.root.after_cancel(self._pos_job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._pos_job = self.root.after(700, self.save_position)
        except tk.TclError:
            self._pos_job = None

    def save_position(self):
        """Persist where he put the window. Cheap, and a no-op when nothing moved.

        ‼ THE ORDER MATTERS IN `hide()`: this must run BEFORE `withdraw()`.
        `winfo_x/y` on a withdrawn window report a stale or zeroed position, so
        saving after withdrawing would write a coordinate he never chose - and it
        would look exactly like the bug this feature is meant to fix.
        """
        try:
            if not self.root.winfo_viewable():
                return                  # not on screen: any coordinate now is a lie
            pos = [self.root.winfo_x(), self.root.winfo_y()]
        except tk.TclError:
            return                      # window already destroyed
        if pos == self.ui.get("pos"):
            return                      # unchanged: never rewrite prefs for nothing
        self.ui["pos"] = pos
        try:
            prefs.save(self.ui)
        except OSError:
            pass                        # a failed position write is not worth a dialog

    def restore_position(self):
        """Put the window back where he left it, if that is still somewhere real."""
        pos = self.ui.get("pos")
        if not pos:
            return                      # never saved: let Tk place it, which is correct
        if not position_reachable(pos):
            # ‼ DISCARDED, NOT CLAMPED. He has three panels and one of them is
            # physically shared with another box, so a monitor genuinely does come
            # and go here - and a position nudged back onto the primary display
            # reads as "it forgot again", which is the complaint, not the fix.
            _log("saved position %r is off the current desktop - letting Tk place it" % (pos,))
            self.ui["pos"] = None
            try:
                prefs.save(self.ui)
            except OSError:
                pass
            return
        try:
            self.root.geometry("+%d+%d" % (pos[0], pos[1]))
        except tk.TclError:
            pass


# ----------------------------------------------------------------------- CLI

def _merge_and_save(incoming):
    """Add what is new, keep what is there, and report WITHOUT echoing a secret."""
    existing = store.load()
    seen = {(a.get("issuer"), a.get("name")) for a in existing}
    added = [a for a in incoming if (a.get("issuer"), a.get("name")) not in seen]
    store.save(existing + added)
    print(f"parsed {len(incoming)}, added {len(added)}, total {len(existing) + len(added)}")
    print(f"store: {store.STORE_PATH}")
    if len(added) != len(incoming):
        print(f"skipped {len(incoming) - len(added)} already present")
    for a in added:
        print(f"  + {a.get('issuer', ''):<28} {a.get('name', '')}")
    return 0


def cmd_import(path):
    with open(path, "r", encoding="utf-8") as fh:
        return _merge_and_save(import_ga.parse(fh.read()))


def cmd_import_qr(paths):
    """Read the export QR straight off a screenshot.

    Google Authenticator's "Transfer accounts -> Export" renders a QR on the phone
    screen and there is no copyable text, so a screenshot is the only practical route.
    ‼ The decoded payload IS every seed - it is passed to the parser in memory and
    never printed, never logged, never written anywhere but the DPAPI store.
    """
    from PIL import Image as _Image                 # lazy: the tray app needs neither
    from pyzbar.pyzbar import decode as zbar_decode

    uris, missed = [], []
    for p in paths:
        with _Image.open(p) as img:
            found = zbar_decode(img)
        name = os.path.basename(p)
        if not found:
            missed.append(name)
            print(f"  {name}: NO QR CODE FOUND")
            continue
        for r in found:
            text = r.data.decode("utf-8", "replace")
            if not text.startswith(("otpauth-migration://", "otpauth://")):
                print(f"  {name}: a QR that is not an otpauth export - skipped")
                continue
            uris.append(text)
        print(f"  {name}: {len(found)} code(s)")

    if not uris:
        print("nothing to import")
        return 1
    rc = _merge_and_save(import_ga.parse("\n".join(uris)))
    if missed:
        print(f"\u203c {len(missed)} image(s) yielded no code: {', '.join(missed)}")
    return rc


def cmd_list():
    ui = prefs.load()
    for a in prefs.apply_order([import_ga.split_label(x) for x in store.load()], ui):
        print(f"{a.get('issuer', ''):<24} {a.get('name', '')}")
    print(f"\nsort={ui['sort']} locked={ui['locked']} prefs={prefs.PREFS_PATH}")


def cmd_smoke():
    """Headless check of the window logic: build it, tick it, copy, reorder, lock.

    A tray app cannot assert its own tray, but everything BELOW the tray is
    testable - and untested GUI logic is where a crash reaches him first.
    Never touches the real store or the real prefs.
    """
    import tempfile

    global LOG_PATH

    probe = os.path.join(tempfile.gettempdir(), "doorman_smoke.dpapi")
    store.STORE_PATH = probe
    prefs.PREFS_PATH = os.path.join(tempfile.gettempdir(), "doorman_smoke_prefs.json")
    # ‼ REDIRECT THE LOG TOO, NOT JUST THE PREFS. `LOG_PATH` is computed at import
    # from the REAL prefs directory, so redirecting `prefs.PREFS_PATH` alone left
    # `--smoke` writing its off-desktop test value into his live startup log -
    # measured 2026-09-07, and it made a test artifact read as a real event. This
    # function's own docstring promises it never touches real state; the log is
    # real state.
    LOG_PATH = os.path.join(tempfile.gettempdir(), "doorman_smoke_startup.log")
    known = "JBSWY3DPEHPK3PXP"                      # RFC/Google documentation secret
    accounts = [
        {"issuer": "Zebra", "name": "z@x", "secret": known, "algorithm": "SHA1",
         "digits": 6, "period": 30},
        {"issuer": "Alpha", "name": "a@x", "secret": known, "algorithm": "SHA1",
         "digits": 8, "period": 30},
    ]
    failures = []
    try:
        store.save(accounts, probe)

        img = _icon_image()
        print("icon             %s %s" % (img.mode, img.size))
        if img.size != (64, 64):
            failures.append("icon size %r" % (img.size,))

        root = tk.Tk()
        root.withdraw()
        w = Window(root, store.load(probe), ui=dict(prefs.DEFAULTS))
        if len(w.rows) != 2:
            failures.append("built %d row(s), expected 2" % len(w.rows))

        shown = [r["acct"]["issuer"] for r in w.rows]
        print("name order       %s" % shown)
        if shown != ["Alpha", "Zebra"]:
            failures.append("name order %r" % shown)

        Window.tick(w)
        root.update_idletasks()
        for i, row in enumerate(w.rows):
            digits = int(row["acct"]["digits"])
            expected = totp.code(known, digits=digits, period=30, algorithm="SHA1")
            if row["raw"] != expected:
                failures.append("row %d code %r != %r" % (i, row["raw"], expected))
            if row["code"].cget("text").replace(" ", "") != expected:
                failures.append("row %d label does not show the code" % i)
        print("codes            OK (6- and 8-digit)")

        w.copy(w.rows[0])
        root.update_idletasks()
        if root.clipboard_get() != w.rows[0]["raw"]:
            failures.append("clipboard mismatch")
        print("clipboard        OK")

        # reorder by hand the way _motion does, then persist the way _release does
        w.rows.insert(1, w.rows.pop(0))
        w._repack()
        w.ui["sort"] = "custom"
        w.ui["order"] = [prefs.key(r["acct"]) for r in w.rows]
        prefs.save(w.ui)
        reread = prefs.load()
        after = [a["issuer"] for a in prefs.apply_order(accounts, reread)]
        print("custom persisted %s" % after)
        if after != ["Zebra", "Alpha"]:
            failures.append("custom order did not persist %r" % after)

        # ---- window position: the part he asked for, and the multi-monitor guard
        rect = virtual_screen()
        print("virtual desktop  %s" % (rect,))
        if rect is None:
            failures.append("virtual_screen() returned None on Windows")
        else:
            left, top, width, height = rect
            inside = [left + 50, top + 50]
            if not position_reachable(inside):
                failures.append("a position inside the desktop was judged unreachable: %r" % inside)
            # ‼ far off the RIGHT of the whole virtual desktop, not merely off the
            # primary monitor - the check must span every panel, which is the entire
            # reason it does not use winfo_screenwidth()
            outside = [left + width + 5000, top + 50]
            if position_reachable(outside):
                failures.append("an off-desktop position was judged reachable: %r" % outside)
            print("reach check      inside OK, off-desktop rejected OK")

        # a withdrawn window must NOT record a position - see save_position
        w.ui["pos"] = None
        w.save_position()
        if w.ui["pos"] is not None:
            failures.append("save_position wrote %r for a withdrawn window" % (w.ui["pos"],))
        print("withdrawn no-save OK")

        # a reachable saved position round-trips through prefs and is applied
        if rect is not None:
            want = [rect[0] + 120, rect[1] + 90]
            w.ui["pos"] = want
            prefs.save(w.ui)
            if prefs.load()["pos"] != want:
                failures.append("position did not persist: %r" % prefs.load()["pos"])
            w.restore_position()
            if w.ui["pos"] != want:
                failures.append("restore_position discarded a reachable position")
            # and an unreachable one is DISCARDED rather than clamped
            w.ui["pos"] = [rect[0] + rect[2] + 5000, rect[1] + 50]
            w.restore_position()
            if w.ui["pos"] is not None:
                failures.append("restore_position kept an off-desktop position: %r" % (w.ui["pos"],))
            print("position persist OK (reachable kept, off-desktop discarded)")

        # ‼ AND THE CHAIN THAT ACTUALLY MATTERS: MOVE -> <Configure> -> debounce ->
        # SAVE, with the event loop PUMPED. The checks above only exercise the
        # helpers; this is the one that proves the feature works, and it exists
        # because an external Win32 `MoveWindow` did NOT persist a position while
        # a Tk-side move does - so the first instrument said "broken" about
        # working code. Pump the loop, do not sleep past it.
        if rect is not None:
            root.deiconify()
            root.update()
            w.ui["pos"] = None
            prefs.save(w.ui)
            root.geometry("+%d+%d" % (rect[0] + 220, rect[1] + 160))
            deadline = time.time() + 4
            while time.time() < deadline and prefs.load().get("pos") is None:
                root.update()
                time.sleep(0.05)
            landed = prefs.load().get("pos")
            print("move persisted   %s" % (landed,))
            if landed is None:
                failures.append("a move never persisted a position through <Configure>")
            elif abs(landed[0] - (rect[0] + 220)) > 40 or abs(landed[1] - (rect[1] + 160)) > 40:
                failures.append("persisted position %r is not where the window was put" % (landed,))

            # and hide() must persist too - that is the path the X button takes
            w.ui["pos"] = None
            prefs.save(w.ui)
            root.geometry("+%d+%d" % (rect[0] + 300, rect[1] + 240))
            root.update()
            w.hide()
            if prefs.load().get("pos") is None:
                failures.append("hide() did not persist the position")
            print("hide persisted   %s" % (prefs.load().get("pos"),))

        w.ui["pos"] = None
        prefs.save(w.ui)

        w.toggle_lock()
        if not prefs.load()["locked"]:
            failures.append("lock did not persist")
        before = [r["acct"]["issuer"] for r in w.rows]
        class _E:
            y_root = 10 ** 6
        w._press_y = 0
        w._motion(_E(), w.rows[0])
        if [r["acct"]["issuer"] for r in w.rows] != before:
            failures.append("LOCK DID NOT BLOCK the reorder")
        print("lock blocks drag OK")

        real_value = startup.VALUE_NAME
        startup.VALUE_NAME = "doorman_smoke_DELETEME"
        try:
            if startup.is_enabled():
                failures.append("probe Run value already present")
            w.toggle_logon()
            if not startup.is_enabled():
                failures.append("logon toggle did not write the Run value")
            if "pythonw.exe" not in (startup.read() or "").lower():
                failures.append("Run value does not use pythonw: %r" % startup.read())
            w.toggle_logon()
            if startup.is_enabled():
                failures.append("logon toggle did not remove the Run value")
            print("logon checkbox   OK")
        finally:
            startup.disable()
            startup.VALUE_NAME = real_value

        w.sort_by_name()
        if [r["acct"]["issuer"] for r in w.rows] != ["Alpha", "Zebra"]:
            failures.append("A-Z button did not re-sort")
        print("A-Z button       OK")

        # always-on-top: the prefs value AND the live window attribute must both move
        if not bool(root.attributes("-topmost")):
            failures.append("topmost not applied from defaults")
        w.toggle_topmost()
        if bool(root.attributes("-topmost")) or prefs.load()["topmost"]:
            failures.append("topmost did not turn off / did not persist")
        w.toggle_topmost()
        if not bool(root.attributes("-topmost")) or not prefs.load()["topmost"]:
            failures.append("topmost did not turn back on")
        print("on-top toggle    OK")

        w.hide()                                     # the minimize path
        if root.state() != "withdrawn":
            failures.append("minimize did not withdraw the window, state=%r" % root.state())
        w.show()
        print("minimize/show    OK")

        Window(tk.Toplevel(root), [], ui=dict(prefs.DEFAULTS))
        print("empty state      OK")
        root.destroy()
    finally:
        for p in (probe, prefs.PREFS_PATH):
            if os.path.exists(p):
                os.remove(p)

    print()
    print("smoke FAILED: " + "; ".join(failures) if failures else "window smoke OK")
    return 1 if failures else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doorman", description=__doc__.splitlines()[0])
    ap.add_argument("--import", dest="import_path", metavar="FILE",
                    help="add accounts from a file of otpauth / otpauth-migration URIs")
    ap.add_argument("--import-qr", dest="qr_paths", nargs="+", metavar="IMAGE",
                    help="read otpauth export QR codes straight off screenshots")
    ap.add_argument("--list", action="store_true", help="print stored account names")
    ap.add_argument("--smoke", action="store_true",
                    help="headless check of the window logic against temp state")
    ap.add_argument("--store", metavar="PATH",
                    help="use this secrets store instead of the default "
                         "(overrides DOORMAN_STORE; an explicit flag survives the "
                         "process-environment inheritance that an env var does not)")
    ap.add_argument("--install-shortcut", action="store_true",
                    help="put doorman in the Start Menu and exit")
    ap.add_argument("--remove-shortcut", action="store_true",
                    help="remove the Start Menu shortcut and exit")
    args = ap.parse_args(argv)

    if args.store:
        store.STORE_PATH = os.path.abspath(args.store)

    if args.qr_paths:
        return cmd_import_qr(args.qr_paths)
    if args.import_path:
        return cmd_import(args.import_path)
    if args.list:
        return cmd_list()
    if args.smoke:
        return cmd_smoke()
    if args.install_shortcut:
        print(shortcut.create(icon=_icon_file()))
        return 0
    if args.remove_shortcut:
        print("removed" if shortcut.remove() else "none present")
        return 0

    _log("start pid=%d exe=%s store=%s" % (os.getpid(), sys.executable, store.STORE_PATH))

    # ‼ ONE LIVE INSTANCE. Doorman is now reachable from the logon Run entry AND
    # from the Start Menu, so a double launch is ordinary rather than a mistake -
    # and without this it produces two padlocks reading the same store. A second
    # launch is not an error to report: it is him asking to SEE the window, which
    # is exactly what clicking a shortcut means.
    if not instance.acquire():
        _log("another instance is live - signalling it to surface, exiting 0")
        instance.signal_show()
        return 0

    try:
        accounts = [import_ga.split_label(a) for a in store.load()]
        _log("store loaded: %d account(s)" % len(accounts))
    except Exception:
        _fatal("The secrets store could not be read:\n%s" % store.STORE_PATH)
        return 1

    try:
        root = tk.Tk()
        root.withdraw()
        window = Window(root, accounts)
        _log("window built")
    except Exception:
        _fatal("The window could not be built.")
        return 1

    def _quit(*_):
        # ‼ SAVE THE POSITION BEFORE TEARING DOWN. The X button routes through
        # `hide()`, which saves - but Quit from the tray menu does not touch the
        # window at all, so without this the one path he explicitly asked about
        # ("saves its coordinates on exit") would be the one that dropped them.
        def teardown():
            window.save_position()
            icon.stop()
            root.destroy()
        root.after(0, teardown)

    icon = pystray.Icon(
        "doorman", _icon_image(), "doorman",
        menu=pystray.Menu(
            pystray.MenuItem("Open", lambda *_: root.after(0, window.show), default=True),
            pystray.MenuItem("Quit", _quit),
        ),
    )

    def _run_tray():
        """Own the tray icon, and never fail silently doing it.

        ‼ THE OLD FORM WAS `Thread(target=icon.run, daemon=True)` WITH NO
        HANDLER, AND THAT IS A SILENT-FAILURE MACHINE: if `icon.run` raises,
        the daemon thread dies unheard while `root.mainloop()` keeps running
        against a WITHDRAWN window - a live process with no tray icon and
        nothing on screen. Indistinguishable from "it did not launch."

        ‼ WHY IT RETRIES: adding a tray icon is `Shell_NotifyIcon`, which
        needs the shell's tray window to exist. At logon the Run entry can fire
        before the taskbar is ready, and the call fails. Stated as the leading
        HYPOTHESIS for the logout/login failure, NOT as a proven cause - the
        startup log is what will settle it at the next logon. The retry is
        worth having either way, because it costs nothing when the shell is up.

        ‼ AND IF IT NEVER COMES UP, THE APP SHOWS ITS WINDOW INSTEAD OF
        HIDING: no icon plus no window is the one outcome that must not happen.
        """
        for attempt in range(1, TRAY_ATTEMPTS + 1):
            try:
                icon.run()
                _log("tray loop ended normally")
                return
            except Exception:
                _log("tray attempt %d/%d failed" % (attempt, TRAY_ATTEMPTS), exc=True)
                time.sleep(TRAY_RETRY_SEC)
        _log("tray never came up after %d attempts - showing the window instead"
             % TRAY_ATTEMPTS)
        try:
            root.after(0, window.show)
        except Exception:
            pass

    def _surface():
        # ‼ LOGGED, because during the 0.2 work the signal could be seen leaving
        # and NOT seen arriving - the sender logged "signalling it to surface"
        # and the receiver logged nothing, so a working path and a swallowed
        # callback looked identical. Both ends of a handoff get a line.
        _log("show requested by a second launch")
        root.after(0, window.show)

    threading.Thread(target=_run_tray, daemon=True, name="doorman-tray").start()
    instance.watch_show(_surface)
    _log("running")
    root.mainloop()
    _log("exit")


if __name__ == "__main__":
    sys.exit(main())
