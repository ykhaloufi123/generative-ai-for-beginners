"""Filtre avant LLM : aucun PAN, PIN block, piste ou clé ne doit partir vers un modèle."""
from __future__ import annotations

import re

from ..crypto.card import luhn_valid

_PAN = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
_TRACK2 = re.compile(r"\d{13,19}[=D]\d{4,}")
_HEXKEY = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?:[0-9A-Fa-f]{16})?(?![0-9A-Fa-f])")
_PINBLOCK = re.compile(r"(?i)(pin\s*block|de\s*52|champ\s*52)[^0-9A-F]{0,10}([0-9A-F]{16})")


class PiiBlocked(Exception):
    pass


def scrub(text: str) -> tuple[str, list[str]]:
    """Masque les données sensibles ; renvoie le texte nettoyé et la liste de ce qui a été masqué."""
    found = []

    def pan_repl(m):
        v = m.group(1)
        if luhn_valid(v):
            found.append("PAN")
            return "[PAN]"
        return v

    text = _TRACK2.sub(lambda m: (found.append("TRACK2"), "[TRACK2]")[1], text)
    text = _PAN.sub(pan_repl, text)
    text = _HEXKEY.sub(lambda m: (found.append("KEY"), "[KEY]")[1], text)
    text = _PINBLOCK.sub(lambda m: (found.append("PINBLOCK"), m.group(1) + " [PINBLOCK]")[1], text)
    return text, found


def guard(text: str, strict: bool = False) -> str:
    clean, found = scrub(text)
    if found and strict:
        raise PiiBlocked(f"Données sensibles détectées : {sorted(set(found))}")
    return clean
