"""PIN blocks ISO 9564 (formats 0, 1, 3) et chiffrement sous ZPK/TPK."""
from __future__ import annotations

import secrets

from . import des

# Correspondance format ISO -> code jPOS utilisé par __calculatePINBlock dans JMeter
JPOS_FORMAT = {"iso0": 1, "iso1": 5, "iso3": 47}


def _pan_block(pan: str) -> bytes:
    return bytes.fromhex("0000" + pan[:-1][-12:].rjust(12, "0"))


def clear_block(pin: str, pan: str, fmt: str = "iso0") -> str:
    if not (4 <= len(pin) <= 12 and pin.isdigit()):
        raise ValueError("PIN invalide (4 à 12 chiffres)")
    fmt = fmt.lower()
    if fmt == "iso0":
        p = ("0" + format(len(pin), "X") + pin).ljust(16, "F")
        return des.xor(bytes.fromhex(p), _pan_block(pan)).hex().upper()
    if fmt == "iso1":
        fill = "".join(secrets.choice("0123456789") for _ in range(16))
        return ("1" + format(len(pin), "X") + pin + fill)[:16]
    if fmt == "iso3":
        fill = "".join(secrets.choice("ABCDEF") for _ in range(16))
        p = ("3" + format(len(pin), "X") + pin + fill)[:16]
        return des.xor(bytes.fromhex(p), _pan_block(pan)).hex().upper()
    raise ValueError(f"Format de PIN block non supporté : {fmt}")


def extract_pin(clear_block_hex: str, pan: str) -> str:
    raw = bytes.fromhex(clear_block_hex)
    fmt = raw[0] >> 4
    if fmt in (0, 3):
        raw = des.xor(raw, _pan_block(pan))
    h = raw.hex().upper()
    n = int(h[1], 16)
    return h[2:2 + n]


def encrypt(clear_block_hex: str, zpk_hex: str) -> str:
    return des.encrypt_ecb(zpk_hex, bytes.fromhex(clear_block_hex)).hex().upper()


def decrypt(enc_block_hex: str, zpk_hex: str) -> str:
    return des.decrypt_ecb(zpk_hex, bytes.fromhex(enc_block_hex)).hex().upper()


def verify(enc_block_hex: str, zpk_hex: str, pan: str, expected_pin: str) -> bool:
    try:
        return extract_pin(decrypt(enc_block_hex, zpk_hex), pan) == expected_pin
    except Exception:
        return False
