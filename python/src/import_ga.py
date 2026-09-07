"""Import accounts from a Google Authenticator export, or from plain otpauth URIs.

Google Authenticator's "Transfer accounts -> Export accounts" produces QR codes
holding:

    otpauth-migration://offline?data=<url-encoded base64 protobuf>

The protobuf is small enough that pulling in a protobuf runtime is not worth it -
the varint + length-delimited reader below is the whole parser. Standard library only.

Wire format (reverse-engineered, stable since 2020):

    MigrationPayload { repeated OtpParameters otp_parameters = 1; }
    OtpParameters {
        bytes  secret    = 1;
        string name      = 2;
        string issuer    = 3;
        enum   algorithm = 4;   1=SHA1 2=SHA256 3=SHA512 4=MD5
        enum   digits    = 5;   1=6    2=8
        enum   type      = 6;   1=HOTP 2=TOTP
        uint64 counter   = 7;
    }
"""

import base64
import urllib.parse

_ALGORITHM = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
_DIGITS = {0: 6, 1: 6, 2: 8}


def _varint(buf, i):
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _fields(buf):
    """Yield (field_number, value) for one protobuf message."""
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        number, wire = tag >> 3, tag & 0x07
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, value


def _b32(raw):
    """Google stores the raw secret bytes; every other tool wants base32."""
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def from_migration_uri(uri):
    """Decode one otpauth-migration:// URI into a list of account dicts."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "otpauth-migration":
        raise ValueError(f"not a migration URI: {parsed.scheme!r}")
    data = urllib.parse.parse_qs(parsed.query).get("data", [None])[0]
    if not data:
        raise ValueError("migration URI carries no data parameter")
    payload = base64.b64decode(data + "=" * (-len(data) % 4))

    accounts = []
    for number, value in _fields(payload):
        if number != 1:
            continue  # 2..5 are batch/version bookkeeping, not accounts
        entry = {"algorithm": "SHA1", "digits": 6, "period": 30, "type": "TOTP"}
        for fnum, fval in _fields(value):
            if fnum == 1:
                entry["secret"] = _b32(fval)
            elif fnum == 2:
                entry["name"] = fval.decode("utf-8", "replace")
            elif fnum == 3:
                entry["issuer"] = fval.decode("utf-8", "replace")
            elif fnum == 4:
                entry["algorithm"] = _ALGORITHM.get(fval, "SHA1")
            elif fnum == 5:
                entry["digits"] = _DIGITS.get(fval, 6)
            elif fnum == 6 and fval == 1:
                entry["type"] = "HOTP"
        if entry.get("secret"):
            entry.setdefault("issuer", "")
            entry.setdefault("name", "")
            split_label(entry)
            accounts.append(entry)
    return accounts


def split_label(entry):
    """Some exports carry no issuer field and put it in the label as 'Issuer:name'.

    Left unsplit, the window's title line shows 'GitHub: you@example.com' as one blob.
    Idempotent, so it is safe to call on already-clean entries and on load.
    """
    if not entry.get("issuer") and ":" in (entry.get("name") or ""):
        issuer, _, name = entry["name"].partition(":")
        if issuer.strip() and name.strip():
            entry["issuer"], entry["name"] = issuer.strip(), name.strip()
    return entry


def from_otpauth_uri(uri):
    """Decode a single standard otpauth://totp/... URI."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "otpauth":
        raise ValueError(f"not an otpauth URI: {parsed.scheme!r}")
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    label = urllib.parse.unquote(parsed.path.lstrip("/"))
    issuer, _, name = label.partition(":") if ":" in label else ("", "", label)
    return {
        "secret": query["secret"].replace(" ", "").upper(),
        "name": name.strip() or label,
        "issuer": (query.get("issuer") or issuer).strip(),
        "algorithm": query.get("algorithm", "SHA1").upper(),
        "digits": int(query.get("digits", 6)),
        "period": int(query.get("period", 30)),
        "type": parsed.netloc.upper() or "TOTP",
    }


def parse(text):
    """Accept either scheme, one URI per line. Returns a list of accounts."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("otpauth-migration://"):
            out.extend(from_migration_uri(line))
        elif line.startswith("otpauth://"):
            out.append(from_otpauth_uri(line))
        else:
            raise ValueError(f"unrecognised line: {line[:40]!r}")
    return out


if __name__ == "__main__":
    # Known-good otpauth URI, and a hand-built migration payload, so both
    # legs are exercised without needing a real export on disk.
    single = from_otpauth_uri(
        "otpauth://totp/Example:admin@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&digits=6&period=30"
    )
    assert single["secret"] == "JBSWY3DPEHPK3PXP", single
    assert single["issuer"] == "Example", single
    print("otpauth URI      OK ->", single["issuer"], single["name"], single["secret"])

    secret = base64.b32decode("JBSWY3DPEHPK3PXP")
    inner = (
        b"\x0a" + bytes([len(secret)]) + secret
        + b"\x12\x11" + b"admin@example.com"
        + b"\x1a\x07" + b"Example"
        + b"\x20\x01" + b"\x28\x01" + b"\x30\x02"
    )
    payload = b"\x0a" + bytes([len(inner)]) + inner
    uri = "otpauth-migration://offline?data=" + urllib.parse.quote(
        base64.b64encode(payload).decode()
    )
    batch = from_migration_uri(uri)
    assert len(batch) == 1 and batch[0]["secret"] == "JBSWY3DPEHPK3PXP", batch
    print("migration URI    OK ->", batch[0]["issuer"], batch[0]["name"], batch[0]["secret"])
    print("\nboth import legs pass")
