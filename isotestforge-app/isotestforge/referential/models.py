"""Modèles du référentiel : profils carte, terminal, clés, specs hôte, templates."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..crypto import card as cardlib
from ..crypto import keys as keylib

CardType = Literal["magstripe", "emv", "contactless", "ecom", "manual"]
Cvm = Literal["online_pin", "offline_pin", "signature", "no_cvm"]


class EmvData(BaseModel):
    aip: str = "5C00"
    iad: str = "06010A03A00000"          # Visa CVN10 par défaut ; 06011203... pour CVN18
    atc: str = "0001"
    aid: str = "A0000000031010"
    tvr: str = "0000000000"
    cvm_results: str = "420300"
    app_version: str = "0096"


class CardProfile(BaseModel):
    id: str
    scheme: str = "VISA"
    pan: str
    psn: str = "01"
    expiry: str = Field(..., description="AAMM")
    service_code: str = "201"
    pin: str = "1234"
    card_type: CardType = "emv"
    cvm: Cvm = "online_pin"
    keyset: str | None = None
    status: Literal["active", "blocked", "lost", "stolen", "expired"] = "active"
    balance: float = 100000.0
    currency: str = "504"
    emv: EmvData = Field(default_factory=EmvData)
    tags: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("pan")
    @classmethod
    def _luhn(cls, v: str) -> str:
        if not cardlib.luhn_valid(v):
            raise ValueError(f"PAN {cardlib.mask_pan(v)} : clé de Luhn invalide")
        return v

    @property
    def bin(self) -> str:
        return self.pan[:6]

    def track2(self, cvk: str | None = None, cvv_override: str | None = None) -> str:
        disc = ""
        if cvk or cvv_override:
            code = cvv_override or cardlib.cvv(cvk, self.pan, self.expiry, self.service_code)
            disc = "00000" + code
        return cardlib.track2(self.pan, self.expiry, self.service_code, disc)


class TerminalProfile(BaseModel):
    id: str
    tid: str
    mid: str
    mcc: str = "5411"
    name_location: str = "ISO TESTFORGE SHOP     CASABLANCA   MA"
    country: str = "504"
    currency: str = "504"
    terminal_kind: Literal["pos", "atm", "ecom", "mpos"] = "pos"
    condition_code: str = "00"
    acquirer_id: str = "123456"
    capabilities: str = "E0F8C8"          # 9F33
    terminal_type: str = "22"              # 9F35
    add_capabilities: str = "F000F0A001"   # 9F40
    contactless_cvm_limit: float = 500.0
    pin_capable: bool = True
    tags: list[str] = Field(default_factory=list)

    @field_validator("tid")
    @classmethod
    def _tid(cls, v: str) -> str:
        if len(v) > 8:
            raise ValueError("TID : 8 caractères maximum")
        return v


class KeySet(BaseModel):
    """Clés de TEST. Une valeur peut être littérale, 'env:NOM' ou 'vault:chemin#cle'."""
    id: str
    zmk: str | None = None
    zpk: str | None = None
    tak: str | None = None
    imk_ac: str | None = None
    cvk: str | None = None
    pvk: str | None = None
    mac_algorithm: Literal["retail", "desede"] = "retail"
    description: str = ""

    def kcvs(self, resolver=None) -> dict[str, str]:
        out = {}
        for name in ("zmk", "zpk", "tak", "imk_ac", "cvk", "pvk"):
            raw = getattr(self, name)
            val = resolver(raw) if resolver and raw else raw
            if val and keylib.is_test_key(val):
                out[name] = keylib.kcv(val)
        return out


class ChannelConfig(BaseModel):
    kind: str = "ASCIIChannel"
    header: str = ""
    header_hex: bool | None = None
    length_digits: int = 4


class HostSpec(BaseModel):
    id: str
    description: str = ""
    packager: str = "data/packagers/iso87ascii.xml"
    host: str = "localhost"
    port: int = 8583
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    timeout_ms: int = 15000
    mac_field: int | None = 128
    mux_key: str = "41 11"
    signon: bool = True
    mandatory: dict[str, list[int]] = Field(default_factory=dict)
    # Codes réponse sémantiques -> DE39 de cet hôte
    response_codes: dict[str, str] = Field(default_factory=dict)
    response_labels: dict[str, str] = Field(default_factory=dict)
    # Montants « magiques » interprétés par le simulateur (pannes, refus)
    magic_amounts: dict[str, str] = Field(default_factory=dict)
    spec_version: str = "1.0"

    def code(self, semantic: str) -> str:
        return self.response_codes.get(semantic, semantic)


class MessageTemplate(BaseModel):
    id: str
    mti: str
    description: str = ""
    processing_code: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    crypto: list[str] = Field(default_factory=list)
    expect: dict[str, str | list[str]] = Field(default_factory=dict)
    chain: Literal["none", "reversal", "advice", "completion"] = "none"
    copy_from_ref: list[str] = Field(default_factory=list)


class BinRange(BaseModel):
    prefix: str
    label: str = ""
