"""Couverture et traçabilité : besoin -> scénarios -> résultat, détection des trous et suggestions DSL."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..engine import mutators
from ..referential.repository import Referential

OPERATIONS = ["PURCHASE", "PREAUTH", "REFUND", "WITHDRAWAL", "BALANCE_INQUIRY"]


@dataclass
class CoverageReport:
    requirements: dict[str, dict] = field(default_factory=dict)
    uncovered_requirements: list[str] = field(default_factory=list)
    matrix: dict[str, dict[str, int]] = field(default_factory=dict)       # type de carte -> opération -> nb
    reversal_by_card_type: dict[str, bool] = field(default_factory=dict)
    response_codes_never_expected: list[str] = field(default_factory=list)
    mutators_unused: list[str] = field(default_factory=list)
    templates_unused: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__


def compute(manifests: list[dict], ref: Referential, requirements: list[dict] | None = None,
            analyses: list[dict] | None = None, host_id: str | None = None) -> CoverageReport:
    rep = CoverageReport()
    status_by_scn = {}
    for a in analyses or []:
        for s in a.get("scenarios", []):
            status_by_scn[s["id"]] = s["status"]

    card_types = sorted({c.card_type for c in ref.cards.values()})
    rep.matrix = {ct: {op: 0 for op in OPERATIONS} for ct in card_types}
    rep.reversal_by_card_type = {ct: False for ct in card_types}
    expected_codes: set[str] = set()
    used_mut: set[str] = set()
    used_tpl: set[str] = set()

    for man in manifests:
        for s in man.get("scenarios", []):
            ct = ref.cards[s["card"]].card_type if s.get("card") in ref.cards else None
            if s.get("mutation"):
                used_mut.add(s["mutation"])
            for st in s.get("steps", []):
                tpl = st.get("template") or ""
                used_tpl.add(tpl)
                for c in st.get("expect", {}).get("39", []):
                    expected_codes.add(c)
                if ct and tpl in OPERATIONS and not s.get("mutation"):
                    rep.matrix[ct][tpl] += 1
                if ct and tpl in ("REVERSAL", "REVERSAL_ADVICE"):
                    rep.reversal_by_card_type[ct] = True
            for r in s.get("requirements", []):
                e = rep.requirements.setdefault(r, {"scenarios": [], "status": "not_run"})
                e["scenarios"].append(s["id"])

    for r, e in rep.requirements.items():
        sts = {status_by_scn.get(sid, "not_run") for sid in e["scenarios"]}
        e["status"] = "failed" if "failed" in sts else "passed" if sts == {"passed"} else \
            "not_run" if sts == {"not_run"} else "partial"
    for req in requirements or []:
        if req["id"] not in rep.requirements:
            rep.uncovered_requirements.append(req["id"])
            rep.requirements[req["id"]] = {"scenarios": [], "status": "uncovered", "title": req.get("title", "")}

    host = ref.host(host_id) if host_id else (next(iter(ref.hosts.values())) if ref.hosts else None)
    if host:
        rep.response_codes_never_expected = sorted(c for c in host.response_labels if c not in expected_codes)
    rep.mutators_unused = sorted(m for m in mutators.CATALOG if m not in used_mut)
    rep.templates_unused = sorted(t for t in ref.templates if t not in used_tpl)

    # Trous et suggestions DSL (déterministes, acceptables en un clic)
    by_type = {}
    for c in ref.cards.values():
        if c.status == "active":
            by_type.setdefault(c.card_type, c)
    pos = next((t for t in ref.terminals.values() if t.terminal_kind == "pos"), None)
    for ct, ops in rep.matrix.items():
        if ct not in by_type or not pos:
            continue
        if ops["PURCHASE"] == 0:
            rep.gaps.append(f"Aucun achat nominal sur carte {ct}")
            rep.suggestions.append(_sugg(f"GAP-{ct.upper()}-PURCHASE", f"Achat nominal {ct}", by_type[ct].id, pos.id,
                                         [{"id": "auth", "template": "PURCHASE"}]))
        if not rep.reversal_by_card_type.get(ct):
            rep.gaps.append(f"Aucune annulation sur carte {ct}")
            rep.suggestions.append(_sugg(f"GAP-{ct.upper()}-REVERSAL", f"Achat puis annulation {ct}", by_type[ct].id,
                                         pos.id, [{"id": "auth", "template": "PURCHASE"},
                                                  {"id": "rev", "template": "REVERSAL", "ref": "auth"}]))
    for m in rep.mutators_unused:
        rep.gaps.append(f"Cas négatif jamais testé : {m}")
    if host:
        for c in rep.response_codes_never_expected:
            rep.gaps.append(f"Code réponse {c} ({host.response_labels[c]}) jamais provoqué")

    cells = sum(len(v) for v in rep.matrix.values())
    covered = sum(1 for v in rep.matrix.values() for n in v.values() if n)
    neg_total = len(mutators.CATALOG)
    req_total = len(rep.requirements) or 1
    rep.score = round(100 * (0.4 * (len(rep.requirements) - len(rep.uncovered_requirements)) / req_total
                             + 0.3 * covered / max(1, cells)
                             + 0.3 * (neg_total - len(rep.mutators_unused)) / neg_total), 1)
    return rep


def _sugg(sid: str, title: str, card: str, term: str, steps: list[dict]) -> dict:
    return {"id": sid, "title": title, "tags": ["coverage"], "origin": "manual",
            "profiles": {"card": card, "terminal": term}, "steps": steps}
