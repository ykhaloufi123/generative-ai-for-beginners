"""Générateur de cartes de test (sans IA) : PAN Luhn, Track 2, CVV/iCVV, PVV."""
from __future__ import annotations

import random

from ..crypto import card as cardlib
from ..referential.models import CardProfile, EmvData

SCHEME_BY_PREFIX = {"4": "VISA", "5": "MASTERCARD", "2": "MASTERCARD", "6": "LOCAL", "3": "AMEX"}


def make_pan(bin_prefix: str, length: int = 16, seq: int | None = None, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    body_len = length - len(bin_prefix) - 1
    body = str(seq).zfill(body_len)[-body_len:] if seq is not None else \
        "".join(rng.choice("0123456789") for _ in range(body_len))
    partial = bin_prefix + body
    return partial + cardlib.luhn_digit(partial)


def generate(bin_prefix: str, count: int = 1, card_type: str = "emv", expiry: str = "3012",
             service_code: str | None = None, pin: str = "1234", keyset: str | None = None,
             cvn: int = 10, id_prefix: str | None = None, start_seq: int = 1,
             tags: list[str] | None = None) -> list[CardProfile]:
    scheme = SCHEME_BY_PREFIX.get(bin_prefix[0], "LOCAL")
    if service_code is None:
        service_code = "101" if card_type == "magstripe" else "201"
    iad = "06010A03A00000" if cvn == 10 else "06011203A00000"
    out = []
    for i in range(count):
        pan = make_pan(bin_prefix, seq=start_seq + i)
        cid = f"{id_prefix or scheme + '_' + card_type.upper()}_{start_seq + i:03d}"
        out.append(CardProfile(id=cid, scheme=scheme, pan=pan, expiry=expiry, service_code=service_code,
                               pin=pin, card_type=card_type, keyset=keyset, emv=EmvData(iad=iad),
                               tags=list(tags or []),
                               description=f"Carte de test générée ({card_type}, BIN {bin_prefix})"))
    return out


def card_report(cp: CardProfile, cvk: str | None = None, pvk: str | None = None) -> dict:
    d = {"id": cp.id, "pan": cp.pan, "masked": cardlib.mask_pan(cp.pan), "expiry": cp.expiry,
         "service_code": cp.service_code, "track2": cp.track2(cvk)}
    if cvk:
        d["cvv"] = cardlib.cvv(cvk, cp.pan, cp.expiry, cp.service_code)
        d["icvv"] = cardlib.cvv(cvk, cp.pan, cp.expiry, "999")
        d["cvv2"] = cardlib.cvv(cvk, cp.pan, cp.expiry, "000")
    if pvk:
        d["pvv"] = cardlib.pvv(pvk, cp.pan, cp.pin)
    return d
