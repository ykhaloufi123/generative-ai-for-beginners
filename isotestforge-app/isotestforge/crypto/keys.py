"""Gestion des clés de test : KCV, génération, chiffrement sous KEK (échange de clés 0800)."""
from __future__ import annotations

from . import des


def kcv(key_hex: str, length: int = 6) -> str:
    """Key Check Value : 3DES(clé, 0000000000000000), 6 premiers chiffres hexa."""
    return des.encrypt_ecb(key_hex, b"\x00" * 8).hex().upper()[:length]


def generate(bits: int = 128) -> str:
    return des.random_key(bits // 8).hex().upper()


def encrypt_under(key_hex: str, kek_hex: str) -> str:
    """Chiffre une clé sous une clé de transport (ZMK/TMK), mode ECB comme jPOS __encryptDESKey."""
    return des.encrypt_ecb(kek_hex, bytes.fromhex(key_hex)).hex().upper()


def decrypt_under(enc_hex: str, kek_hex: str) -> str:
    return des.decrypt_ecb(kek_hex, bytes.fromhex(enc_hex)).hex().upper()


def is_test_key(key_hex: str) -> bool:
    """Heuristique de garde-fou : refuse les clés non hexadécimales ou de longueur inattendue."""
    try:
        return len(bytes.fromhex(key_hex)) in (8, 16, 24)
    except ValueError:
        return False
