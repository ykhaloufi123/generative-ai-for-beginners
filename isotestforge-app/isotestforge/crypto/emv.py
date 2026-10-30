"""Cryptogrammes EMV : dérivation de clé carte (option A), clés de session, ARQC, ARPC.

Couvre les cas Visa CVN10 et CVN18 (VIS / EMV Common Session Key). Ces calculs servent
au simulateur, au précalcul et au diagnostic ; dans JMeter, l'ARQC est calculé à
l'exécution par l'ISO8583 Crypto PreProcessor (jPOS) à partir de l'IMK-AC.
"""
from __future__ import annotations

from . import des, mac

ARQC_TAGS = ["9F02", "9F03", "9F1A", "95", "5F2A", "9A", "9C", "9F37", "82", "9F36"]


def derive_udk(imk_hex: str, pan: str, psn: str = "00") -> str:
    y = (pan + psn)[-16:].rjust(16, "0")
    yb = bytes.fromhex(y)
    zl = des.encrypt_ecb(imk_hex, yb)
    zr = des.encrypt_ecb(imk_hex, des.xor(yb, b"\xff" * 8))
    return des.adjust_parity(zl + zr).hex().upper()


def session_key_common(udk_hex: str, atc_hex: str) -> str:
    atc = bytes.fromhex(atc_hex.zfill(4))
    left = des.encrypt_ecb(udk_hex, atc + b"\xf0" + b"\x00" * 5)
    right = des.encrypt_ecb(udk_hex, atc + b"\x0f" + b"\x00" * 5)
    return (left + right).hex().upper()


def cvn_from_iad(iad_hex: str) -> int:
    """Visa : l'octet 3 de l'IAD (format 0) contient la CVN (0x0A=10, 0x12=18)."""
    try:
        return bytes.fromhex(iad_hex)[2]
    except Exception:
        return 10


def arqc_input(icc: dict[str, str]) -> bytes:
    data = b"".join(bytes.fromhex(icc.get(t, "")) for t in ARQC_TAGS)
    iad = icc.get("9F10", "")
    if iad:
        cvn = cvn_from_iad(iad)
        raw = bytes.fromhex(iad)
        data += raw if cvn == 0x12 else raw[3:7]
    return data


def compute_arqc(imk_hex: str, pan: str, psn: str, icc: dict[str, str]) -> str:
    udk = derive_udk(imk_hex, pan, psn)
    cvn = cvn_from_iad(icc.get("9F10", "06010A03000000"))
    data = arqc_input(icc)
    if cvn == 0x12:
        sk = session_key_common(udk, icc.get("9F36", "0001"))
        return mac.retail_mac(sk, data, 2).hex().upper()
    return mac.retail_mac(udk, data, 1).hex().upper()


def compute_arpc(imk_hex: str, pan: str, psn: str, icc: dict[str, str], arqc_hex: str,
                 arc: str = "3030") -> str:
    """ARPC méthode 1 : 3DES(clé, ARQC XOR ARC||000000000000)."""
    udk = derive_udk(imk_hex, pan, psn)
    cvn = cvn_from_iad(icc.get("9F10", "06010A03000000"))
    key = session_key_common(udk, icc.get("9F36", "0001")) if cvn == 0x12 else udk
    block = des.xor(bytes.fromhex(arqc_hex), bytes.fromhex(arc.ljust(16, "0")))
    return des.encrypt_ecb(key, block).hex().upper()


def verify_arqc(imk_hex: str, pan: str, psn: str, icc: dict[str, str]) -> bool:
    got = icc.get("9F26")
    return bool(got) and got.upper() == compute_arqc(imk_hex, pan, psn, icc)
