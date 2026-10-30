"""Diff de spécifications : packagers et specs hôte -> scénarios impactés."""
from __future__ import annotations

from ..iso.packager import GenericPackager
from ..referential.models import HostSpec


def diff_packagers(old: GenericPackager, new: GenericPackager) -> list[dict]:
    changes = []
    for n in sorted(set(old.fields) | set(new.fields)):
        a, b = old.fields.get(n), new.fields.get(n)
        if a and not b:
            changes.append({"de": n, "change": "supprimé", "detail": a.name})
        elif b and not a:
            changes.append({"de": n, "change": "ajouté", "detail": b.name})
        elif a and b:
            diffs = []
            if a.cls != b.cls:
                diffs.append(f"classe {a.cls.rsplit('.', 1)[-1]} -> {b.cls.rsplit('.', 1)[-1]}")
            if a.length != b.length:
                diffs.append(f"longueur {a.length} -> {b.length}")
            if a.kind != b.kind:
                diffs.append(f"structure {a.kind} -> {b.kind}")
            if diffs:
                changes.append({"de": n, "change": "modifié", "detail": ", ".join(diffs)})
    return changes


def diff_hosts(old: HostSpec, new: HostSpec) -> list[dict]:
    changes = []
    for mti in sorted(set(old.mandatory) | set(new.mandatory)):
        a, b = set(old.mandatory.get(mti, [])), set(new.mandatory.get(mti, []))
        if a != b:
            changes.append({"mti": mti, "change": "champs obligatoires",
                            "added": sorted(b - a), "removed": sorted(a - b)})
    for sem in sorted(set(old.response_codes) | set(new.response_codes)):
        if old.response_codes.get(sem) != new.response_codes.get(sem):
            changes.append({"semantic": sem, "change": "code réponse",
                            "old": old.response_codes.get(sem), "new": new.response_codes.get(sem)})
    for attr in ("mac_field", "channel", "spec_version"):
        if getattr(old, attr) != getattr(new, attr):
            changes.append({"change": attr, "old": str(getattr(old, attr)), "new": str(getattr(new, attr))})
    return changes


def impacted(manifests: list[dict], bound_fields: dict[str, set[int]], pk_changes: list[dict],
             host_changes: list[dict]) -> list[dict]:
    """bound_fields : 'SCN.step' -> champs présents. Retourne les scénarios à revoir et pourquoi."""
    changed_de = {c["de"] for c in pk_changes}
    mti_changes = {c["mti"]: c for c in host_changes if "mti" in c}
    code_changes = {c["old"]: c for c in host_changes if c.get("change") == "code réponse" and c.get("old")}
    out = []
    for man in manifests:
        for s in man.get("scenarios", []):
            reasons = []
            for st in s.get("steps", []):
                key = f"{s['id']}.{st['id']}"
                hit = sorted(bound_fields.get(key, set()) & changed_de)
                if hit:
                    reasons.append(f"{st['id']} : champs modifiés {hit}")
                mc = mti_changes.get(st.get("mti"))
                if mc and mc.get("added"):
                    reasons.append(f"{st['id']} : nouveaux champs obligatoires {mc['added']} pour {st['mti']}")
                for c in st.get("expect", {}).get("39", []):
                    if c in code_changes:
                        cc = code_changes[c]
                        reasons.append(f"{st['id']} : code {c} ({cc['semantic']}) devient {cc['new']}")
            if reasons:
                out.append({"scenario": s["id"], "reasons": reasons,
                            "action": "régénérer" if any("code" in r or "champs modifiés" in r for r in reasons)
                            else "compléter le template"})
    return out
