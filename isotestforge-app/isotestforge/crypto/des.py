"""Primitives DES / 3DES (clés de TEST uniquement)."""
from __future__ import annotations

import os
import warnings

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _key(key: bytes | str) -> bytes:
    k = bytes.fromhex(key) if isinstance(key, str) else key
    if len(k) not in (8, 16, 24):
        raise ValueError(f"Clé DES de longueur invalide : {len(k)} octets")
    # Normalisation en 3 clés (K1K2K1 ou K1K1K1) : résultat identique, sans API dépréciée
    if len(k) == 8:
        return k * 3
    if len(k) == 16:
        return k + k[:8]
    return k


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def encrypt_ecb(key: bytes | str, data: bytes) -> bytes:
    enc = Cipher(TripleDES(_key(key)), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def decrypt_ecb(key: bytes | str, data: bytes) -> bytes:
    dec = Cipher(TripleDES(_key(key)), modes.ECB()).decryptor()
    return dec.update(data) + dec.finalize()


def encrypt_cbc(key: bytes | str, data: bytes, iv: bytes = b"\x00" * 8) -> bytes:
    enc = Cipher(TripleDES(_key(key)), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def single_des(key8: bytes, data: bytes) -> bytes:
    return encrypt_ecb(key8, data)


def single_des_decrypt(key8: bytes, data: bytes) -> bytes:
    return decrypt_ecb(key8, data)


def adjust_parity(key: bytes) -> bytes:
    out = bytearray()
    for b in key:
        b &= 0xFE
        out.append(b | (bin(b).count("1") % 2 == 0))
    return bytes(out)


def random_key(length_bytes: int = 16) -> bytes:
    return adjust_parity(os.urandom(length_bytes))
