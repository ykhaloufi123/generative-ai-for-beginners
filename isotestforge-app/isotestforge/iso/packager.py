"""Codec ISO 8583 piloté par un fichier jPOS GenericPackager (le même que celui du plugin JMeter).

Utiliser le même packager XML côté JMeter et côté outils Python garantit que le simulateur,
le décodeur et le runner natif interprètent les messages exactement comme le plugin.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from . import tlv as tlvmod
from .message import IsoMessage


class PackagerError(Exception):
    pass


@dataclass
class ClassSpec:
    prefix_digits: int      # 0 = longueur fixe, 2 = LL, 3 = LLL, 4 = LLLL
    prefix_enc: str         # "A" ascii, "B" bcd, "H" binaire
    data: str               # ascii | num | hexascii | raw | bcd | abitmap | bbitmap | nop | amount


_FIXED = {
    "IFA_NUMERIC": ClassSpec(0, "", "num"), "IF_CHAR": ClassSpec(0, "", "ascii"),
    "IFA_AMOUNT": ClassSpec(0, "", "amount"), "IFA_BINARY": ClassSpec(0, "", "hexascii"),
    "IFA_BITMAP": ClassSpec(0, "", "abitmap"), "IFB_NUMERIC": ClassSpec(0, "", "bcd"),
    "IFB_BINARY": ClassSpec(0, "", "raw"), "IFB_BITMAP": ClassSpec(0, "", "bbitmap"),
    "IF_NOP": ClassSpec(0, "", "nop"), "IFB_AMOUNT": ClassSpec(0, "", "amount"),
}
_VAR = re.compile(r"^IF([AB])_(L{2,4})(H?)(NUM|CHAR|BINARY)$")


def class_spec(cls: str) -> ClassSpec:
    short = cls.rsplit(".", 1)[-1]
    if short in _FIXED:
        return _FIXED[short]
    m = _VAR.match(short)
    if not m:
        raise PackagerError(f"Classe de champ non supportée : {short}")
    kind, ls, h, dtype = m.groups()
    prefix_enc = "H" if h else ("A" if kind == "A" else "B")
    if dtype == "NUM":
        data = "num" if kind == "A" else "bcd"
    elif dtype == "CHAR":
        data = "ascii"
    else:
        data = "hexascii" if kind == "A" else "raw"
    return ClassSpec(len(ls), prefix_enc, data)


@dataclass
class FieldDef:
    id: int
    length: int
    name: str
    cls: str
    pad: bool = True
    kind: str | None = None          # "tlv" | "sub" | None
    children: list["FieldDef"] = field(default_factory=list)

    @property
    def spec(self) -> ClassSpec:
        return class_spec(self.cls)

    @property
    def is_binary(self) -> bool:
        return self.spec.data in ("hexascii", "raw")

    @property
    def max_len(self) -> int:
        return self.length


def _bcd(digits: str) -> bytes:
    if len(digits) % 2:
        digits = "0" + digits
    return bytes.fromhex(digits)


class GenericPackager:
    def __init__(self, fields: dict[int, FieldDef], source: str = ""):
        self.fields = fields
        self.source = source

    # ------------------------------------------------------------------ chargement
    @classmethod
    def load(cls, path: str | Path) -> "GenericPackager":
        parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
        root = etree.parse(str(path), parser).getroot()
        return cls(cls._parse_children(root), str(path))

    @classmethod
    def _parse_children(cls, node) -> dict[int, FieldDef]:
        out: dict[int, FieldDef] = {}
        for el in node:
            if not isinstance(el.tag, str):
                continue
            if el.tag not in ("isofield", "isofieldpackager"):
                continue
            fd = FieldDef(int(el.get("id")), int(el.get("length", "0")), el.get("name", ""),
                          el.get("class", ""), el.get("pad", "true").lower() == "true")
            if el.tag == "isofieldpackager":
                pk = el.get("packager", "")
                if "TLV" in pk.upper():
                    fd.kind = "tlv"
                else:
                    fd.kind = "sub"
                    fd.children = list(cls._parse_children(el).values())
            out[fd.id] = fd
        return out

    def field(self, n: int) -> FieldDef:
        if n not in self.fields:
            raise PackagerError(f"Champ {n} absent du packager")
        return self.fields[n]

    # ------------------------------------------------------------------ encodage d'un champ
    def _encode_data(self, fd: FieldDef, value: str) -> tuple[bytes, int]:
        sp = fd.spec
        if sp.data in ("ascii", "amount"):
            v = value
            if sp.prefix_digits == 0 and fd.pad:
                v = v.ljust(fd.length) if sp.data == "ascii" else v.rjust(fd.length, "0")
            return v.encode("latin-1"), len(v)
        if sp.data == "num":
            if not value.isdigit():
                raise PackagerError(f"Champ {fd.id} numérique : '{value}'")
            v = value.zfill(fd.length) if sp.prefix_digits == 0 else value
            return v.encode(), len(v)
        if sp.data in ("hexascii", "raw"):
            raw = bytes.fromhex(value)
            if sp.prefix_digits == 0:
                raw = raw.ljust(fd.length, b"\x00")
            payload = raw.hex().upper().encode() if sp.data == "hexascii" else raw
            return payload, len(raw)
        if sp.data == "bcd":
            v = value.zfill(fd.length) if sp.prefix_digits == 0 else value
            return _bcd(v), len(v)
        if sp.data == "nop":
            return b"", 0
        raise PackagerError(f"Encodage {sp.data} non géré pour le champ {fd.id}")

    def _encode_raw_content(self, fd: FieldDef, raw: bytes) -> tuple[bytes, int]:
        """Contenu déjà sous forme d'octets (TLV ou sous-champs concaténés)."""
        sp = fd.spec
        if sp.data == "hexascii":
            return raw.hex().upper().encode(), len(raw)
        return raw, len(raw)

    def _prefix(self, fd: FieldDef, length: int) -> bytes:
        sp = fd.spec
        if sp.prefix_digits == 0:
            if length != fd.length and sp.data not in ("nop",):
                raise PackagerError(f"Champ {fd.id} : longueur {length} au lieu de {fd.length}")
            return b""
        if length > fd.length:
            raise PackagerError(f"Champ {fd.id} : longueur {length} > max {fd.length}")
        if sp.prefix_enc == "A":
            return str(length).zfill(sp.prefix_digits).encode()
        if sp.prefix_enc == "B":
            return _bcd(str(length).zfill(sp.prefix_digits))
        return length.to_bytes(sp.prefix_digits - 1, "big")  # LL→1 octet, LLL→2 octets

    def pack_field(self, fd: FieldDef, msg: IsoMessage) -> bytes:
        if fd.kind == "tlv":
            payload, ln = self._encode_raw_content(fd, tlvmod.encode(msg.tlv.get(fd.id, [])))
        elif fd.kind == "sub" and fd.id in msg.sub:
            inner = bytearray()
            subs = msg.sub[fd.id]
            for child in fd.children:
                if child.id in subs:
                    p, ln_c = self._encode_data(child, subs[child.id])
                    inner += self._prefix(child, ln_c) + p
            payload, ln = self._encode_raw_content(fd, bytes(inner))
        else:
            payload, ln = self._encode_data(fd, msg.fields[fd.id])
        return self._prefix(fd, ln) + payload

    # ------------------------------------------------------------------ message complet
    def pack(self, msg: IsoMessage) -> bytes:
        present = [n for n in msg.present_fields() if n > 1]
        for n in present:
            self.field(n)
        out = bytearray(self.pack_field(self.field(0), IsoMessage(fields={0: msg.mti})))
        secondary = any(n > 64 for n in present)
        bits = 128 if secondary else 64
        bm = 0
        for n in present:
            bm |= 1 << (bits - n)
        if secondary:
            bm |= 1 << (bits - 1)
        bm_bytes = bm.to_bytes(bits // 8, "big")
        if self.field(1).spec.data == "abitmap":
            out += bm_bytes.hex().upper().encode()
        else:
            out += bm_bytes
        for n in present:
            out += self.pack_field(self.field(n), msg)
        return bytes(out)

    def _read_prefix(self, fd: FieldDef, data: bytes, i: int) -> tuple[int, int]:
        sp = fd.spec
        if sp.prefix_digits == 0:
            return fd.length, i
        if sp.prefix_enc == "A":
            n = sp.prefix_digits
            return int(data[i:i + n].decode()), i + n
        if sp.prefix_enc == "B":
            nb = (sp.prefix_digits + 1) // 2
            return int(data[i:i + nb].hex()), i + nb
        nb = sp.prefix_digits - 1
        return int.from_bytes(data[i:i + nb], "big"), i + nb

    def _read_payload(self, fd: FieldDef, data: bytes, i: int, ln: int) -> tuple[bytes, int, str]:
        """Retourne (octets logiques, nouvel index, valeur texte)."""
        sp = fd.spec
        if sp.data in ("ascii", "num", "amount"):
            raw = data[i:i + ln]
            return raw, i + ln, raw.decode("latin-1")
        if sp.data == "hexascii":
            txt = data[i:i + 2 * ln].decode()
            return bytes.fromhex(txt), i + 2 * ln, txt.upper()
        if sp.data == "raw":
            raw = data[i:i + ln]
            return raw, i + ln, raw.hex().upper()
        if sp.data == "bcd":
            nb = (ln + 1) // 2
            digits = data[i:i + nb].hex()
            return data[i:i + nb], i + nb, digits[-ln:] if ln else ""
        if sp.data == "nop":
            return b"", i, ""
        raise PackagerError(f"Décodage {sp.data} non géré (champ {fd.id})")

    def unpack(self, data: bytes) -> IsoMessage:
        msg = IsoMessage()
        f0 = self.field(0)
        ln, i = self._read_prefix(f0, data, 0)
        _, i, msg.mti = self._read_payload(f0, data, i, ln)
        f1 = self.field(1)
        ascii_bm = f1.spec.data == "abitmap"

        def read_bm(i: int) -> tuple[int, int]:
            if ascii_bm:
                return int(data[i:i + 16].decode(), 16), i + 16
            return int.from_bytes(data[i:i + 8], "big"), i + 8

        bm, i = read_bm(i)
        bits = 64
        if bm >> 63 & 1:
            bm2, i = read_bm(i)
            bm = (bm << 64) | bm2
            bits = 128
        present = [n for n in range(2, bits + 1) if bm >> (bits - n) & 1]
        for n in present:
            fd = self.field(n)
            ln, i = self._read_prefix(fd, data, i)
            raw, i, txt = self._read_payload(fd, data, i, ln)
            if fd.kind == "tlv":
                msg.tlv[n] = tlvmod.decode(raw)
            elif fd.kind == "sub":
                j, subs = 0, {}
                for child in fd.children:
                    if j >= len(raw):
                        break
                    cl, j = self._read_prefix(child, raw, j)
                    _, j, ctxt = self._read_payload(child, raw, j, cl)
                    subs[child.id] = ctxt.rstrip() if child.spec.data == "ascii" else ctxt
                msg.sub[n] = subs
            else:
                msg.fields[n] = txt.rstrip() if fd.spec.data == "ascii" and fd.spec.prefix_digits == 0 else txt
        if i != len(data):
            raise PackagerError(f"{len(data) - i} octets non consommés après décodage")
        return msg

    # ------------------------------------------------------------------ validation de format
    def check_value(self, n: int, value: str) -> str | None:
        """Retourne un message d'erreur si la valeur ne respecte pas le format du champ, sinon None.
        Les variables JMeter (${...}) ne sont pas vérifiées."""
        if "${" in value:
            return None
        try:
            fd = self.field(n)
        except PackagerError as e:
            return str(e)
        if fd.kind:
            return None
        sp = fd.spec
        if sp.data in ("num", "bcd") and not value.isdigit():
            return f"DE{n} doit être numérique ('{value}')"
        if sp.data in ("hexascii", "raw"):
            if not re.fullmatch(r"[0-9A-Fa-f]*", value) or len(value) % 2:
                return f"DE{n} doit être en hexadécimal pair ('{value}')"
            ln = len(value) // 2
        else:
            ln = len(value)
        if sp.prefix_digits == 0:
            if sp.data in ("ascii", "num", "bcd", "amount") and ln > fd.length:
                return f"DE{n} : longueur {ln} > {fd.length}"
            if sp.data in ("hexascii", "raw") and ln != fd.length:
                return f"DE{n} : {ln} octets au lieu de {fd.length}"
        elif ln > fd.length:
            return f"DE{n} : longueur {ln} > max {fd.length}"
        return None

    def describe(self) -> list[dict]:
        return [{"id": f.id, "name": f.name, "class": f.cls.rsplit(".", 1)[-1], "length": f.length,
                 "kind": f.kind} for f in sorted(self.fields.values(), key=lambda x: x.id)]
