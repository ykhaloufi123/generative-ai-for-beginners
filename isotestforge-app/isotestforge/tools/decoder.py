"""Décodeur de messages : hexadécimal / ASCII / log jPOS -> champs nommés, DE55 en TLV détaillé."""
from __future__ import annotations

import re

from lxml import etree

from ..crypto import card as cardlib
from ..iso.message import IsoMessage
from ..iso.packager import GenericPackager
from ..iso.tlv import EMV_TAGS


def _raw_candidates(text: str) -> list[bytes]:
    """Le texte peut être un message ASCII tel quel ou un dump hexadécimal : on essaie les deux."""
    t = text.strip()
    out = [t.encode("latin-1")]
    compact = re.sub(r"[\s:]", "", t)
    if re.fullmatch(r"[0-9A-Fa-f]+", compact) and len(compact) % 2 == 0:
        out.append(bytes.fromhex(compact))
    return out


def parse_jpos_xml(text: str) -> IsoMessage:
    root = etree.fromstring(text.strip().encode())
    node = root if root.tag == "isomsg" else root.find(".//isomsg")
    msg = IsoMessage()
    for f in node:
        if f.tag == "field":
            fid = f.get("id")
            if fid == "0":
                msg.mti = f.get("value")
            else:
                msg.fields[int(fid)] = f.get("value")
        elif f.tag == "isomsg":
            n = int(f.get("id"))
            for sf in f:
                if sf.get("tag"):
                    msg.set(f"{n}.{sf.get('id')}", sf.get("value"), sf.get("tag"))
                else:
                    msg.sub.setdefault(n, {})[int(sf.get("id"))] = sf.get("value")
    return msg


def decode(text: str, pk: GenericPackager, mask: bool = True, header_len: int = 0) -> dict:
    t = text.strip()
    if t.startswith("<"):
        msg = parse_jpos_xml(t)
        source = "jpos-xml"
    else:
        candidates = []
        for data in _raw_candidates(t):
            if data[:4].isdigit() and int(data[:4]) == len(data) - 4:      # préfixe ASCIIChannel
                candidates.append(data[4 + header_len:])
            if len(data) > 2 and int.from_bytes(data[:2], "big") == len(data) - 2:  # NACChannel
                candidates.append(data[2 + header_len:])
            candidates.append(data[header_len:])
        last = None
        msg = None
        for c in candidates:
            try:
                msg = pk.unpack(c)
                break
            except Exception as e:
                last = e
        if msg is None:
            raise ValueError(f"Décodage impossible avec ce packager : {last}")
        source = "binary"
    return describe(msg, pk, mask) | {"source": source}


def describe(msg: IsoMessage, pk: GenericPackager, mask: bool = True) -> dict:
    rows = [{"de": "0", "name": "MTI", "value": msg.mti}]
    for n in msg.present_fields():
        name = pk.fields[n].name if n in pk.fields else "?"
        if n in msg.tlv:
            rows.append({"de": str(n), "name": name, "value": f"{len(msg.tlv[n])} tags",
                         "tags": [{"tag": t, "name": EMV_TAGS.get(t, "?"), "value": v} for t, v in msg.tlv[n]]})
        elif n in msg.sub:
            rows.append({"de": str(n), "name": name, "value": " | ".join(msg.sub[n].values())})
        else:
            v = msg.fields[n]
            if mask and n == 2:
                v = cardlib.mask_pan(v)
            if mask and n == 35:
                v = cardlib.mask_pan(v.split("=")[0]) + "=****"
            if mask and n in (52,):
                v = "********"
            rows.append({"de": str(n), "name": name, "value": v})
    return {"mti": msg.mti, "fields": rows}


def to_text(decoded: dict) -> str:
    lines = [f"MTI {decoded['mti']}"]
    for r in decoded["fields"][1:]:
        lines.append(f"  DE{r['de']:>3}  {r['name'][:38]:38}  {r['value']}")
        for t in r.get("tags", []):
            lines.append(f"         {t['tag']:>4}  {t['name'][:33]:33}  {t['value']}")
    return "\n".join(lines)
