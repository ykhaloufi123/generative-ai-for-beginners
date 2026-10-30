"""Données carte : Luhn, CVV/CVV2/iCVV, PVV Visa, Track 2."""
from __future__ import annotations

from . import des


def luhn_digit(partial: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(partial)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_valid(pan: str) -> bool:
    return pan.isdigit() and luhn_digit(pan[:-1]) == pan[-1]


def _decimalize(hex_str: str, n: int) -> str:
    digits = [c for c in hex_str if c.isdigit()]
    if len(digits) < n:
        digits += [str(int(c, 16) - 10) for c in hex_str if not c.isdigit()]
    return "".join(digits[:n])


def cvv(cvk_hex: str, pan: str, expiry_yymm: str, service_code: str) -> str:
    """Algorithme CVV standard (Visa/Mastercard) avec la paire de clés CVK A||B."""
    key = bytes.fromhex(cvk_hex)
    ka = key[:8]
    data = (pan + expiry_yymm + service_code).ljust(32, "0")
    b1, b2 = bytes.fromhex(data[:16]), bytes.fromhex(data[16:32])
    r = des.single_des(ka, b1)
    r = des.encrypt_ecb(key[:16], des.xor(r, b2))
    return _decimalize(r.hex().upper(), 3)


def pvv(pvk_hex: str, pan: str, pin: str, pvki: str = "1") -> str:
    tsp = pan[:-1][-11:] + pvki + pin[:4]
    r = des.encrypt_ecb(pvk_hex, bytes.fromhex(tsp)).hex().upper()
    return _decimalize(r, 4)


def track2(pan: str, expiry_yymm: str, service_code: str, discretionary: str = "") -> str:
    return f"{pan}={expiry_yymm}{service_code}{discretionary}"


def mask_pan(pan: str) -> str:
    return pan[:6] + "*" * (len(pan) - 10) + pan[-4:] if len(pan) >= 13 else pan
