# doorman

**Your 2FA codes on your Windows desktop. Click one, it's on your clipboard.**

Tired of reaching for your phone to type six digits into the machine you're already
sitting at? Ever wonder why, in 2026, the most widely used authenticator app on earth
still has no desktop client — while quietly shipping an *export* button?

The codes were never the hard part. They're `HMAC-SHA1(your_secret, time / 30)` —
[RFC 6238](https://datatracker.ietf.org/doc/html/rfc6238), an open standard since 2011.
The generator in this repo is about twenty lines of Python standard library. The only
thing you ever needed was your own secrets, and that export button hands them to you.

So: a padlock in your tray, a small window of codes, click to copy. That's the whole thing.

---

## What it does

- **Padlock tray icon.** Click it for the window; close the window and it goes back.
- **No taskbar button, ever.** Tray only.
- One row per account: issuer, name, the code, a countdown ring.
- **Click a row to copy** the code.
- **`A→Z`** sorts by name · **drag a row** for your own order · **`🔓/🔒`** freezes it.
- **`☐ logon`** starts it with Windows · **`☐ in Start`** puts it in the Start Menu ·
  **`📌`** always-on-top · **`–`** back to the tray.
- **Quit isn't a one-way door.** Tick `☐ in Start` and `doorman` is in the Start Menu, so
  after you quit it you get it back with `Win` → type "doorman" → Enter.
- **One instance only.** Launching it again — from Start, or by logging on while it runs —
  brings the existing window to the front instead of adding a second padlock.

## What it deliberately doesn't do

No settings screen. No cloud. No sync. No telemetry. No account of its own. No network
access at all — **it never makes a single outbound connection**, which you can verify by
reading four short files.

It also **doesn't scroll.** If you have so many codes that they won't fit on screen,
that's a signal about your account hygiene rather than a missing feature.

## Requirements

| | |
|---|---|
| OS | **Windows 10 or 11.** DPAPI and the tray are Windows-specific. |
| Python | **3.10+** (developed and tested on 3.14) |
| Packages | `pystray`, `pillow`, `pyzbar` — three, for the tray icon and the QR reader |
| Your phone | to display the export QR once |

Everything security-relevant — the code generator, the encrypted store, the import
parser — is **standard library only**. The three packages are for drawing an icon and
reading a QR image.

## Install

```
git clone https://github.com/<owner>/doorman.git
cd doorman
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r python\requirements.txt
```

A venv anywhere is fine. If you'd rather keep it outside the repo, that works too —
nothing here depends on where it lives.

## Transfer your accounts off your phone

This is the part that matters, and it takes about two minutes.

**1. Export from your authenticator app.**

In Google Authenticator: **⋮ → Transfer accounts → Export accounts**, authenticate,
select the accounts you want, **Next**. You'll get one or more QR codes.

> Most authenticator apps built on the same standard have an equivalent export. If
> yours has no export at all, that is worth knowing about the app you chose.

**2. Screenshot each QR code.** There is no copyable text — the QR *is* the payload —
so a screenshot is the route. If it shows several, screenshot every one; each holds a
different batch of accounts.

**3. Get the images onto your PC.** Anything works: a USB cable, Windows Phone Link,
OneDrive, emailing them to yourself.

**4. Import them.**

```
.venv\Scripts\python.exe python\src\app.py --import-qr shot1.png shot2.png
```

It'll print what it added — issuer and account name only. **The secrets are never
printed, never logged, and go straight into the encrypted store.**

**5. Check a code matches your phone**, then delete the screenshots.

> ⚠ **A screenshot of that QR is your seeds in plain sight.** Anyone who gets that
> image gets every account in it. Delete them from your PC *and* your phone's gallery
> once you've confirmed a code works.

**6. Run it.**

```
.venv\Scripts\pythonw.exe python\src\app.py
```

`pythonw.exe`, not `python.exe` — otherwise a console window rides along.

Then tick **`☐ logon`** in the window and it'll be there after every restart.

### Already have your secrets as text?

If you have `otpauth://totp/...` URIs (or an `otpauth-migration://` string), put them in
a file one per line and use `--import` instead:

```
.venv\Scripts\python.exe python\src\app.py --import mycodes.txt
```

## Where your secrets live

`%LOCALAPPDATA%\doorman\secrets.dpapi`, encrypted with **Windows DPAPI**.

DPAPI ties the blob to **your Windows account on that machine**. Copy the file to
another PC and it is useless — no passphrase for you to remember, and nothing for you
to get wrong. Set `DOORMAN_STORE` to put it elsewhere.

| what | where |
|---|---|
| secrets | `%LOCALAPPDATA%\doorman\secrets.dpapi` (`DOORMAN_STORE`) |
| sort order, lock, on-top | `%LOCALAPPDATA%\doorman\prefs.json` (`DOORMAN_PREFS`) |
| start at logon | `HKCU\...\CurrentVersion\Run`, value `doorman` |

**Back up your seeds somewhere else too.** DPAPI is deliberately unportable, so a
reinstall, a new machine or a wiped profile means your store is gone. Keep the original
export QR somewhere genuinely safe, or keep the accounts on your phone as well.

### Optional hardening

Point `DOORMAN_STORE` at a directory you have ACL'd to read-only for your own account
and writable only by administrators. Then `--import` needs an elevated shell and normal
running doesn't — writing your second factor costs elevation, reading a code doesn't.
Entirely optional; the app behaves the same either way.

## Commands

| | |
|---|---|
| `app.py` | run in the tray |
| `app.py --import-qr IMG...` | import from export QR screenshots |
| `app.py --import FILE` | import from a file of otpauth URIs |
| `app.py --list` | list account names (never the secrets) |
| `app.py --smoke` | self-check the window logic |
| `app.py --install-shortcut` | put doorman in the Start Menu and exit |
| `app.py --remove-shortcut` | take it out again |

## If it doesn't start

There is a log, and it exists because of a real failure: doorman is launched by
`pythonw.exe`, which has **no console** — and a Python error produces no Windows crash
report either. So a startup failure used to leave *nothing at all* behind: no icon, no
window, no log. You could not tell "it never started" from "it started and died."

```
%LOCALAPPDATA%\doorman\startup.log
```

Every launch appends what it got through — the store, the window, the tray — so the last
line names the phase that failed. A failure that gets that far also puts a dialog on
screen rather than vanishing.

## Security notes, stated plainly

- **No network access.** The app makes no outbound connections of any kind.
- **Secrets are never printed or logged**, including by `--list` and by the importer.
- The store is **DPAPI, user scope** — not machine scope, so another account on the same
  PC cannot decrypt it.
- ⚠ **A time-based code is phishable.** A convincing fake login page can harvest the six
  digits and replay them inside 30 seconds — that's true of every TOTP app, this one
  included. For accounts that support it, a hardware security key is strictly stronger.
- ⚠ Anything with your seeds **is** your second factor: this store, the export QR, a
  screenshot of it. Treat them the way you'd treat the password itself.

## Known limits

Written down rather than discovered:

- **HOTP (counter-based) accounts are parsed but rendered as time-based**, so they would
  show a wrong code. Rare — almost everything issues TOTP — but it fails silently.
- **The window doesn't scroll.** Deliberate; see above.
- **The tray icon can't be covered by tests.** Everything below it is.

## Tests

Every module self-checks when run directly:

```powershell
foreach ($m in 'totp','import_ga','store','prefs','startup','instance','shortcut') {
    .venv\Scripts\python.exe python\src\$m.py
}
.venv\Scripts\python.exe python\src\app.py --smoke
```

`totp.py` runs the **six official test vectors from RFC 6238 Appendix B** — the check is
"does it match the specification's own answers", not "does it emit six digits". `--smoke`
builds the window against a throwaway store and asserts the rendered code, the clipboard,
the ordering, the lock, and the logon toggle.

## Layout

```
doorman/
  python/
    src/     totp.py  store.py  import_ga.py  prefs.py  startup.py
             instance.py  shortcut.py  app.py
    requirements.txt
```

`totp.py`, `store.py`, `import_ga.py`, `prefs.py`, `startup.py`, `instance.py` and
`shortcut.py` import nothing outside the standard library. Only `app.py` has
dependencies. That's on purpose — the parts that touch your secrets are small enough to
read in full.

## License

**Apache License 2.0** — see [`LICENSE`](LICENSE). Copyright 2026 Carpe Diem Innovations Inc.

Use it, ship it, fork it, sell something built on it. Two things the licence asks of you:
keep the copyright and `NOTICE` attribution with it (§4(c), §4(d)), and if you change a
file, say so in that file (§4(b)). There's also a patent grant in both directions, which
is the main reason this isn't MIT.

And one thing the licence doesn't ask, so consider it a request instead: **if doorman was
useful to you, a mention is appreciated.** Not required, not enforced, just decent.

## Contributing

Pull requests welcome. Two things before you open one:

- **Every module self-checks.** Run the tests above; a PR that breaks `totp.py`'s RFC
  vectors is a PR that breaks the codes.
- **Keep the security-relevant files dependency-free.** `totp.py`, `store.py`,
  `import_ga.py`, `prefs.py` and `startup.py` use only the standard library, so anyone
  can audit the parts that handle secrets without reading a supply chain. New dependencies
  belong in `app.py` or nowhere.

Bug reports are more useful than feature requests. The scope is deliberately small.
