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
import os
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw
import pystray

import import_ga
import prefs
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
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()


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

    probe = os.path.join(tempfile.gettempdir(), "doorman_smoke.dpapi")
    store.STORE_PATH = probe
    prefs.PREFS_PATH = os.path.join(tempfile.gettempdir(), "doorman_smoke_prefs.json")
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

    accounts = [import_ga.split_label(a) for a in store.load()]
    root = tk.Tk()
    root.withdraw()
    window = Window(root, accounts)

    icon = pystray.Icon(
        "doorman", _icon_image(), "doorman",
        menu=pystray.Menu(
            pystray.MenuItem("Open", lambda *_: root.after(0, window.show), default=True),
            pystray.MenuItem("Quit", lambda *_: root.after(0, lambda: (icon.stop(), root.destroy()))),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
