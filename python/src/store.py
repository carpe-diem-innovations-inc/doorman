"""Secret storage: a JSON blob wrapped with Windows DPAPI.

Why DPAPI and not a passphrase: the blob is bound to THIS user on THIS machine, so
a copy that leaves the machine - into a disk image, a backup, a stray file, a cloud
sync folder - is inert. Nothing to remember, nothing to type, and no passphrase of
ours to get wrong.

Default location is %LOCALAPPDATA%\\doorman\\secrets.dpapi. Set DOORMAN_STORE to put
it somewhere else - for example a directory whose ACL you have tightened, in which
case writing needs elevation and reading does not. That split is optional and the
app works either way.
"""

import ctypes
import json
import os
from ctypes import wintypes

STORE_PATH = os.environ.get(
    "DOORMAN_STORE",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                 "doorman", "secrets.dpapi"),
)

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_in(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _blob_out(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(blob.pbData)


def _describe():
    return ctypes.c_wchar_p("doorman secrets")


def protect(data):
    src, _keep = _blob_in(data)
    out = _BLOB()
    if not _crypt32.CryptProtectData(
        ctypes.byref(src), _describe(), None, None, None, 0, ctypes.byref(out)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(out)


def unprotect(data):
    src, _keep = _blob_in(data)
    out = _BLOB()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(out)


def load(path=None):
    """Return the account list, or [] when no store exists yet."""
    path = path or STORE_PATH
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw:
        return []
    return json.loads(unprotect(raw).decode("utf-8"))


def save(accounts, path=None):
    """Write atomically - a half-written secret store is worse than none."""
    path = path or STORE_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)      # first run on a clean machine
    blob = protect(json.dumps(accounts, ensure_ascii=False).encode("utf-8"))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(blob)


if __name__ == "__main__":
    # Round-trip against a temp path so the real store is never touched.
    import tempfile

    probe = os.path.join(tempfile.gettempdir(), "doorman_selftest.dpapi")
    sample = [{"issuer": "Example", "name": "admin@example.com", "secret": "JBSWY3DPEHPK3PXP"}]
    try:
        n = save(sample, probe)
        back = load(probe)
        print(f"wrote {n} bytes, read back {len(back)} account(s)")
        print("round-trip", "OK" if back == sample else "FAIL")
        raise SystemExit(0 if back == sample else 1)
    finally:
        if os.path.exists(probe):
            os.remove(probe)
