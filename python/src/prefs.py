"""UI state: sort mode, custom order, order lock. Standard library only.

‼ DELIBERATELY NOT IN THE SECRETS STORE. `B:\\safe\\` is read-only unelevated by
design, so anything written there would make a preference change cost `sudo`.
This file holds issuer/name keys and a boolean - no secrets - so it lives in the
ordinary per-user location and is writable by the running app.
"""

import json
import os

PREFS_PATH = os.environ.get(
    "DOORMAN_PREFS",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "doorman", "prefs.json"),
)

DEFAULTS = {"sort": "name", "order": [], "locked": False, "topmost": True, "pos": None}

# ‼ `pos` IS `None` UNTIL HE MOVES THE WINDOW, AND None MEANS "LET TK PLACE IT".
# A default of [0, 0] would look harmless and would pin every first run to the
# top-left corner - a saved position and "no saved position" must not be the
# same value. Whether a stored position is still REACHABLE is a screen question,
# not a preferences one: this module validates the SHAPE, `app.py` owns the
# multi-monitor check.


def key(acct):
    """Identity of an account for ordering purposes. Never the secret."""
    return "%s\x1f%s" % (acct.get("issuer") or "", acct.get("name") or "")


def load(path=None):
    path = path or PREFS_PATH
    out = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data:
                    out[k] = data[k]
    except (OSError, ValueError):
        pass                                    # absent or malformed -> defaults, never a crash
    if out["sort"] not in ("name", "custom"):
        out["sort"] = "name"
    if not isinstance(out["order"], list):
        out["order"] = []
    out["locked"] = bool(out["locked"])
    out["topmost"] = bool(out["topmost"])
    out["pos"] = _clean_pos(out["pos"])
    return out


def _clean_pos(value):
    """A saved position is exactly two ints, or None. Anything else is None.

    ‼ `bool` IS A SUBCLASS OF `int` IN PYTHON, so a naive isinstance check
    accepts `[True, False]` as a coordinate pair. Rejected explicitly.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    out = []
    for n in value:
        if isinstance(n, bool) or not isinstance(n, int):
            return None
        out.append(n)
    return out


def save(prefs, path=None):
    path = path or PREFS_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(prefs, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def apply_order(accounts, prefs):
    """Return accounts in the order the prefs ask for.

    `name` sorts by issuer then name. `custom` follows the saved order and
    ‼ appends anything the saved order has never seen, so a NEW account imported
    later shows up instead of vanishing - the failure mode of a naive index map.
    """
    if prefs.get("sort") != "custom" or not prefs.get("order"):
        return sorted(accounts, key=lambda a: ((a.get("issuer") or "").lower(),
                                               (a.get("name") or "").lower()))
    rank = {k: i for i, k in enumerate(prefs["order"])}
    known = [a for a in accounts if key(a) in rank]
    fresh = [a for a in accounts if key(a) not in rank]
    known.sort(key=lambda a: rank[key(a)])
    fresh.sort(key=lambda a: ((a.get("issuer") or "").lower(), (a.get("name") or "").lower()))
    return known + fresh


if __name__ == "__main__":
    import tempfile

    probe = os.path.join(tempfile.gettempdir(), "doorman_prefs_selftest.json")
    accts = [
        {"issuer": "Zebra", "name": "z@x"},
        {"issuer": "Alpha", "name": "a@x"},
        {"issuer": "Mango", "name": "m@x"},
    ]
    failures = []

    got = [a["issuer"] for a in apply_order(accts, {"sort": "name", "order": []})]
    print("name sort        %s" % got)
    if got != ["Alpha", "Mango", "Zebra"]:
        failures.append("name sort %r" % got)

    custom = {"sort": "custom", "order": [key(accts[0]), key(accts[2]), key(accts[1])]}
    got = [a["issuer"] for a in apply_order(accts, custom)]
    print("custom order     %s" % got)
    if got != ["Zebra", "Mango", "Alpha"]:
        failures.append("custom order %r" % got)

    # a NEW account absent from the saved order must appear, not disappear
    plus = accts + [{"issuer": "Newly", "name": "n@x"}]
    got = [a["issuer"] for a in apply_order(plus, custom)]
    print("custom + unseen  %s" % got)
    if got != ["Zebra", "Mango", "Alpha", "Newly"]:
        failures.append("unseen account handling %r" % got)

    try:
        # ‼ deliberately saved WITHOUT `topmost` - an older prefs file, which is the
        # real backward-compatibility case. load() must fill the missing key from
        # DEFAULTS rather than dropping it or raising.
        save({"sort": "custom", "order": ["a", "b"], "locked": True}, probe)
        back = load(probe)
        print("round-trip       %s" % back)
        expected = {"sort": "custom", "order": ["a", "b"], "locked": True,
                    "topmost": True, "pos": None}
        if back != expected:
            failures.append("round-trip %r != %r" % (back, expected))
        if set(back) != set(DEFAULTS):
            failures.append("load() returned keys %r, DEFAULTS has %r" % (set(back), set(DEFAULTS)))
        missing = load(os.path.join(tempfile.gettempdir(), "doorman_absent.json"))
        print("absent -> defaults %s" % missing)
        if missing != DEFAULTS:
            failures.append("absent defaults %r" % missing)
        # a real position survives a round trip, including negative coordinates -
        # ‼ a monitor left of or above the primary one has NEGATIVE x/y on Windows,
        # so rejecting negatives would break the most ordinary multi-monitor layout
        save({"pos": [-1200, -340]}, probe)
        if load(probe)["pos"] != [-1200, -340]:
            failures.append("negative position did not survive: %r" % load(probe)["pos"])
        print("negative pos     OK")

        for bad in ([1], [1, 2, 3], "10,20", [True, False], [1.5, 2.5], {"x": 1}, None):
            save({"pos": bad}, probe)
            if load(probe)["pos"] is not None:
                failures.append("malformed pos %r accepted as %r" % (bad, load(probe)["pos"]))
        print("malformed pos    all rejected to None OK")

        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        broken = load(probe)
        print("malformed -> defaults %s" % broken)
        if broken != DEFAULTS:
            failures.append("malformed defaults %r" % broken)
    finally:
        if os.path.exists(probe):
            os.remove(probe)

    print()
    print("prefs FAILED: " + "; ".join(failures) if failures else "prefs OK")
    raise SystemExit(1 if failures else 0)
