"""Validateur déterministe : formats, champs obligatoires, chaînages, structure JMX."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from ..crypto import card as cardlib
from ..referential.repository import Referential, ReferentialError
from .binder import BoundSuite

PKG = "nz.co.breakpoint.jmeter.iso8583"
KNOWN_ELEMENTS = {
    f"{PKG}.ISO8583Config": {"host", "port", "packager", "classname"},
    f"{PKG}.ISO8583Sampler": {"timeout", "responseCodeField", "successResponseCode"},
    f"{PKG}.ISO8583Crypto": {"macAlgorithm", "macKey", "pinField", "pinKey", "iccField", "imkac", "macField"},
}


@dataclass
class Issue:
    level: str        # error | warning
    where: str
    message: str


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    def error(self, where, msg):
        self.issues.append(Issue("error", where, msg))

    def warn(self, where, msg):
        self.issues.append(Issue("warning", where, msg))

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    def errors_for(self, scenario_id: str) -> list[str]:
        return [i.message for i in self.issues if i.level == "error" and i.where.startswith(scenario_id + ".")]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": sum(i.level == "error" for i in self.issues),
                "warnings": sum(i.level == "warning" for i in self.issues),
                "issues": [i.__dict__ for i in self.issues]}


def validate_bound(bound: BoundSuite, ref: Referential) -> ValidationReport:
    rep = ValidationReport()
    pk = ref.packager(bound.host_id)
    host = bound.host
    names: set[str] = set()
    for w in bound.warnings:
        rep.warn("binder", w)
    all_steps = [("SETUP", s) for s in bound.setup_steps] + [("TEARDOWN", s) for s in bound.teardown_steps]
    for scn in bound.scenarios:
        all_steps += [(scn.id, s) for s in scn.steps]
        if not scn.requirements:
            rep.warn(scn.id, "aucune exigence rattachée (traçabilité)")
    for sid, st in all_steps:
        where = f"{sid}.{st.id}"
        if st.name in names:
            rep.error(where, f"nom de sampler en double : {st.name}")
        names.add(st.name)
        present = set()
        for f in st.fields:
            n = int(f.key.split(".")[0])
            present.add(n)
            if f.tag:
                continue
            if f.value == "":
                rep.error(where, f"DE{f.key} vide")
                continue
            err = pk.check_value(n, f.value)
            if err:
                rep.error(where, err)
        if st.crypto.mac and st.crypto.mac_field:
            present.add(st.crypto.mac_field)
            if not pk.fields.get(st.crypto.mac_field) or not pk.fields[st.crypto.mac_field].is_binary:
                rep.error(where, f"DE{st.crypto.mac_field} doit être binaire pour porter le MAC")
        for n in host.mandatory.get(st.mti, []):
            if n not in present:
                rep.error(where, f"DE{n} obligatoire pour {st.mti} (spec {host.id})") \
                    if not _expected_format_error(st) else None
        pan = st.field_value("2")
        if pan and "${" not in pan:
            if not cardlib.luhn_valid(pan) and not _expected_invalid_card(st):
                rep.warn(where, f"PAN {cardlib.mask_pan(pan)} : Luhn invalide")
            try:
                ref.check_test_pan(pan)
            except ReferentialError as e:
                rep.error(where, str(e))
        for k, codes in st.expect.items():
            if k == "39":
                for c in codes:
                    if c not in ("present", "absent") and not c.startswith("re:") and host.response_labels \
                            and c not in host.response_labels:
                        rep.warn(where, f"code réponse {c} absent du dictionnaire de l'hôte")
        if st.ref is None and st.template in ("REVERSAL", "REVERSAL_ADVICE", "COMPLETION") and \
                "original_not_found" not in str(st.expect):
            rep.warn(where, "annulation/complétion sans étape d'origine")
    return rep


def _expected_format_error(st) -> bool:
    return "30" in st.expect.get("39", [])


def _expected_invalid_card(st) -> bool:
    return "14" in st.expect.get("39", [])


def validate_jmx(path: str | Path) -> ValidationReport:
    rep = ValidationReport()
    try:
        tree = etree.parse(str(path))
    except etree.XMLSyntaxError as e:
        rep.error("jmx", f"XML invalide : {e}")
        return rep
    root = tree.getroot()
    if root.tag != "jmeterTestPlan":
        rep.error("jmx", "racine jmeterTestPlan absente")
    # Règle JMeter : chaque élément de test d'un hashTree est suivi de son propre hashTree
    for ht in root.iter("hashTree"):
        kids = list(ht)
        i = 0
        while i < len(kids):
            el = kids[i]
            if el.tag == "hashTree":
                rep.error("jmx", "hashTree orphelin")
                i += 1
                continue
            if i + 1 >= len(kids) or kids[i + 1].tag != "hashTree":
                rep.error("jmx", f"élément {el.get('testname')} sans hashTree")
            i += 2
    counts = {"samplers": 0, "crypto": 0, "configs": 0}
    for tag, required in KNOWN_ELEMENTS.items():
        for el in root.iter(tag):
            props = {p.get("name") for p in el}
            missing = required - props
            if missing:
                rep.error("jmx", f"{el.get('testname')} : propriétés manquantes {sorted(missing)}")
            counts["samplers" if tag.endswith("Sampler") else "crypto" if tag.endswith("Crypto") else "configs"] += 1
    if counts["configs"] == 0:
        rep.error("jmx", "aucun ISO8583Config")
    if counts["samplers"] == 0:
        rep.error("jmx", "aucun ISO8583Sampler")
    text = Path(path).read_text(encoding="utf-8")
    for bad in ("imkac\">0", "pinKey\">0", "macKey\">0"):
        if bad in text:
            rep.error("jmx", "clé en clair détectée dans le JMX")
    return rep
