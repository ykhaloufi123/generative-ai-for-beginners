"""Modèle de message ISO 8583 indépendant du format binaire."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IsoMessage:
    mti: str = ""
    fields: dict[int, str] = field(default_factory=dict)
    # Champs TLV (ex. 55) : liste ordonnée (tag, valeur hex)
    tlv: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    # Sous-champs positionnels (ex. 43.1, 43.2)
    sub: dict[int, dict[int, str]] = field(default_factory=dict)
    header: bytes = b""

    # ---- accès pratique -------------------------------------------------
    def set(self, key: str | int, value: str, tag: str | None = None) -> None:
        """key = "4", "43.2" ou "55" + tag (forme du plugin JMeter : 55.n + tag)."""
        key = str(key)
        if key == "0":
            self.mti = value
            return
        if "." in key:
            num, subn = key.split(".", 1)
            num_i = int(num)
            if tag:
                lst = [t for t in self.tlv.get(num_i, []) if t[0] != tag.upper()]
                lst.append((tag.upper(), value.upper()))
                self.tlv[num_i] = lst
            else:
                self.sub.setdefault(num_i, {})[int(subn)] = value
            return
        if tag:
            self.set(f"{key}.1", value, tag)
            return
        self.fields[int(key)] = value

    def get(self, key: str | int, default: str | None = None) -> str | None:
        key = str(key)
        if key == "0":
            return self.mti
        if "." in key:
            num, rest = key.split(".", 1)
            num_i = int(num)
            if num_i in self.tlv:
                for t, v in self.tlv[num_i]:
                    if t == rest.upper():
                        return v
                return default
            return self.sub.get(num_i, {}).get(int(rest), default)
        return self.fields.get(int(key), default)

    def tag(self, field_no: int, tag: str) -> str | None:
        for t, v in self.tlv.get(field_no, []):
            if t == tag.upper():
                return v
        return None

    def has(self, key: str | int) -> bool:
        k = int(str(key).split(".")[0])
        return k == 0 or k in self.fields or k in self.tlv or k in self.sub

    def present_fields(self) -> list[int]:
        return sorted(set(self.fields) | set(self.tlv) | set(self.sub))

    def remove(self, key: str | int) -> None:
        k = int(str(key).split(".")[0])
        for d in (self.fields, self.tlv, self.sub):
            d.pop(k, None)

    def copy(self) -> "IsoMessage":
        return IsoMessage(self.mti, dict(self.fields), {k: list(v) for k, v in self.tlv.items()},
                          {k: dict(v) for k, v in self.sub.items()}, self.header)

    def response_mti(self) -> str:
        return self.mti[:2] + str(int(self.mti[2]) + 1) + self.mti[3]

    def to_dict(self) -> dict:
        d: dict = {"mti": self.mti, "fields": {str(k): v for k, v in sorted(self.fields.items())}}
        if self.tlv:
            d["tlv"] = {str(k): [{"tag": t, "value": v} for t, v in lst] for k, lst in self.tlv.items()}
        if self.sub:
            d["sub"] = {str(k): {str(i): v for i, v in s.items()} for k, s in self.sub.items()}
        return d

    def to_jmeter_xml(self) -> str:
        """Représentation XML proche de celle affichée par le plugin (utile pour les assertions)."""
        parts = ["<isomsg>", f'  <field id="0" value="{self.mti}"/>']
        for k in self.present_fields():
            if k in self.tlv:
                parts.append(f'  <isomsg id="{k}">')
                for i, (t, v) in enumerate(self.tlv[k], 1):
                    parts.append(f'    <field id="{i}" value="{v}" type="binary" tag="{t}"/>')
                parts.append("  </isomsg>")
            elif k in self.sub:
                parts.append(f'  <isomsg id="{k}">')
                for i, v in sorted(self.sub[k].items()):
                    parts.append(f'    <field id="{i}" value="{v}"/>')
                parts.append("  </isomsg>")
            else:
                parts.append(f'  <field id="{k}" value="{self.fields[k]}"/>')
        parts.append("</isomsg>")
        return "\n".join(parts)
