"""RFC 6238 TOTP. Standard library only - no dependencies, by design.

The whole algorithm is HMAC(secret, floor(now / period)) truncated to N digits.
Anything that claims to need a service or an API key for this is wrong.
"""

import base64
import hashlib
import hmac
import struct
import time

_ALGOS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
    "MD5": hashlib.md5,
}


def b32_decode(secret):
    """Base32 with the padding Google omits, and spaces people paste."""
    s = secret.replace(" ", "").replace("-", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code(secret, digits=6, period=30, algorithm="SHA1", at=None):
    """Return the current code as a zero-padded string."""
    key = secret if isinstance(secret, bytes) else b32_decode(secret)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), _ALGOS[algorithm.upper()]).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def seconds_left(period=30, at=None):
    """Seconds until the current code expires."""
    now = time.time() if at is None else at
    return period - (now % period)


if __name__ == "__main__":
    # RFC 6238 Appendix B test vectors, SHA1, secret "12345678901234567890".
    key = b"12345678901234567890"
    vectors = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]
    failures = 0
    for t, expected in vectors:
        got = code(key, digits=8, period=30, algorithm="SHA1", at=t)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"t={t:<12} expected={expected} got={got} {'OK' if ok else 'FAIL'}")
    print(f"\n{len(vectors) - failures}/{len(vectors)} RFC 6238 vectors pass")
    raise SystemExit(1 if failures else 0)
