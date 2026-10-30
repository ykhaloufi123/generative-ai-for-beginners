"""MAC ISO 9797-1 (algorithmes 1 et 3) alignés sur les options du plugin JMeter."""
from __future__ import annotations

from . import des

# Noms d'algorithmes acceptés par l'ISO8583 Crypto PreProcessor
PLUGIN_ALGORITHMS = {"retail": "ISO9797ALG3MACWITHISO7816-4PADDING", "desede": "DESEDE"}


def pad(data: bytes, method: int) -> bytes:
    if method == 1:
        return data + b"\x00" * ((-len(data)) % 8) if data else b"\x00" * 8
    if method == 2:
        d = data + b"\x80"
        return d + b"\x00" * ((-len(d)) % 8)
    raise ValueError("Méthode de padding 1 ou 2")


def retail_mac(key_hex: str, data: bytes, padding: int = 2) -> bytes:
    """ISO 9797-1 algorithme 3 : CBC simple DES avec KL, puis 3DES sur le dernier bloc."""
    key = bytes.fromhex(key_hex)
    kl, kr = key[:8], key[8:16]
    d = pad(data, padding)
    h = b"\x00" * 8
    for i in range(0, len(d), 8):
        h = des.single_des(kl, des.xor(h, d[i:i + 8]))
    h = des.single_des_decrypt(kr, h)
    return des.single_des(kl, h)


def cbc_mac_3des(key_hex: str, data: bytes, padding: int = 1) -> bytes:
    """ISO 9797-1 algorithme 1 avec 3DES complet."""
    return des.encrypt_cbc(key_hex, pad(data, padding))[-8:]


def generate(key_hex: str, data: bytes, algorithm: str = "retail") -> str:
    if algorithm in ("retail", PLUGIN_ALGORITHMS["retail"]):
        return retail_mac(key_hex, data, 2).hex().upper()
    if algorithm in ("desede", "DESEDE"):
        return cbc_mac_3des(key_hex, data, 1).hex().upper()
    raise ValueError(f"Algorithme MAC inconnu : {algorithm}")
