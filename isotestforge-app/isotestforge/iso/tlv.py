"""Encodage / décodage BER-TLV (champ 55, données EMV)."""
from __future__ import annotations

# Libellés des tags EMV les plus courants (pour le décodeur et les rapports)
EMV_TAGS = {
    "4F": "AID", "50": "Application Label", "57": "Track 2 Equivalent Data",
    "5A": "PAN", "5F24": "Expiration Date", "5F2A": "Transaction Currency Code",
    "5F34": "PAN Sequence Number", "82": "Application Interchange Profile",
    "84": "Dedicated File Name", "8A": "Authorisation Response Code",
    "91": "Issuer Authentication Data", "95": "Terminal Verification Results",
    "9A": "Transaction Date", "9B": "Transaction Status Information",
    "9C": "Transaction Type", "9F02": "Amount, Authorised", "9F03": "Amount, Other",
    "9F06": "AID (terminal)", "9F09": "Application Version Number",
    "9F10": "Issuer Application Data", "9F1A": "Terminal Country Code",
    "9F1E": "IFD Serial Number", "9F26": "Application Cryptogram",
    "9F27": "Cryptogram Information Data", "9F33": "Terminal Capabilities",
    "9F34": "CVM Results", "9F35": "Terminal Type", "9F36": "ATC",
    "9F37": "Unpredictable Number", "9F40": "Additional Terminal Capabilities",
    "9F41": "Transaction Sequence Counter", "9F53": "Transaction Category Code",
    "9F6E": "Form Factor Indicator",
}


def _read_tag(data: bytes, i: int) -> tuple[str, int]:
    start = i
    first = data[i]
    i += 1
    if first & 0x1F == 0x1F:  # tag sur plusieurs octets
        while data[i] & 0x80:
            i += 1
        i += 1
    return data[start:i].hex().upper(), i


def _read_len(data: bytes, i: int) -> tuple[int, int]:
    b = data[i]
    i += 1
    if b & 0x80:
        n = b & 0x7F
        return int.from_bytes(data[i:i + n], "big"), i + n
    return b, i


def decode(data: bytes | str) -> list[tuple[str, str]]:
    """Retourne la liste ordonnée (tag, valeur hex). Les tags construits ne sont pas éclatés."""
    if isinstance(data, str):
        data = bytes.fromhex(data)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(data):
        if data[i] in (0x00, 0xFF):  # octets de remplissage
            i += 1
            continue
        tag, i = _read_tag(data, i)
        length, i = _read_len(data, i)
        out.append((tag, data[i:i + length].hex().upper()))
        i += length
    return out


def _enc_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def encode(items: list[tuple[str, str]] | dict[str, str]) -> bytes:
    if isinstance(items, dict):
        items = list(items.items())
    out = bytearray()
    for tag, value in items:
        v = bytes.fromhex(value)
        out += bytes.fromhex(tag) + _enc_len(len(v)) + v
    return bytes(out)


def to_dict(data: bytes | str) -> dict[str, str]:
    return dict(decode(data))
