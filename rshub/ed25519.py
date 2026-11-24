"""Ed25519 (RFC 8032) in plain Python: verify for the app, sign for the owner's release tool (tools/release_sign.py).

Small and readable on purpose, so anyone can audit what decides whether an update is accepted. It follows the
reference code in RFC 8032 section 6. It is NOT constant-time: verifying works on public data, and signing only
runs on the owner's own PC, never in the app. Checked against every test vector of RFC 8032 section 7.1
(tests/test_ed25519.py).
"""
import hashlib

P = 2 ** 255 - 19                                           # the field
Q = 2 ** 252 + 27742317777372353535851937790883648493       # order of the base point
D = -121665 * pow(121666, P - 2, P) % P                     # curve constant: -x^2 + y^2 = 1 + D x^2 y^2
I = pow(2, (P - 1) // 4, P)                                 # sqrt(-1)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _recover_x(y, sign):
    """x for a given y and the parity bit, or None when there is no such point."""
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * I % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x


# points are (X, Y, Z, T) in extended coordinates, x = X/Z, y = Y/Z, T = X*Y/Z
def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    C, Dd = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _mul(s, pt):
    r = (0, 1, 1, 0)  # the neutral point
    while s:
        if s & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        s >>= 1
    return r


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _compress(pt):
    zi = pow(pt[2], P - 2, P)
    x, y = pt[0] * zi % P, pt[1] * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(seed):
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed):
    """The 32-byte public key of a 32-byte secret seed."""
    return _compress(_mul(_expand(seed)[0], G))


def sign(seed, message):
    """The 64-byte signature of message."""
    a, prefix = _expand(seed)
    A = _compress(_mul(a, G))
    r = int.from_bytes(_sha512(prefix + message), "little") % Q
    R = _compress(_mul(r, G))
    h = int.from_bytes(_sha512(R + A + message), "little") % Q
    return R + ((r + h * a) % Q).to_bytes(32, "little")


def verify(public, message, signature):
    """True only if signature is a valid Ed25519 signature of message by public. Never raises on bad input."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A, R = _decompress(public), _decompress(signature[:32])
        s = int.from_bytes(signature[32:], "little")
        if A is None or R is None or s >= Q:
            return False
        h = int.from_bytes(_sha512(signature[:32] + public + message), "little") % Q
        left, right = _mul(8 * s, G), _add(_mul(8, R), _mul(8 * h, A))  # cofactored check, as RFC 8032 allows
        return _equal(left, right)
    except (TypeError, ValueError):
        return False
