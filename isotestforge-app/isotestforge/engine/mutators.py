"""Catalogue de mutateurs : produit les cas négatifs sans aucun appel LLM.

Chaque mutateur copie un scénario « nominal », modifie l'étape cible et fixe le code réponse
attendu via son nom sémantique (traduit en DE39 par la spec de l'hôte).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..dsl.models import Scenario, Step
from ..referential.models import CardProfile, TerminalProfile

FINANCIAL_TEMPLATES = {"PURCHASE", "PREAUTH", "REFUND", "WITHDRAWAL", "BALANCE_INQUIRY"}


@dataclass
class Mutator:
    name: str
    description: str
    expect: str                      # code sémantique attendu
    fn: Callable[[Scenario, Step, dict], None]
    requires: Callable[[CardProfile, TerminalProfile], bool] = lambda c, t: True


def _target(scn: Scenario, step_id: str | None) -> Step:
    if step_id:
        for s in scn.steps:
            if s.id == step_id:
                return s
        raise ValueError(f"Étape {step_id} absente du scénario {scn.id}")
    for s in scn.steps:
        if (s.template or "") in FINANCIAL_TEMPLATES or (s.mti or "").startswith(("01", "02")):
            return s
    return scn.steps[0]


def _is_chip(c: CardProfile, t: TerminalProfile) -> bool:
    return c.card_type in ("emv", "contactless") and t.terminal_kind != "ecom"


def _has_track(c: CardProfile, t: TerminalProfile) -> bool:
    return t.terminal_kind != "ecom" and c.card_type in ("emv", "magstripe")


def _pin_ok(c: CardProfile, t: TerminalProfile) -> bool:
    return t.pin_capable and t.terminal_kind != "ecom"


def _m_wrong_pin(scn, step, args):
    step.pin = str(args.get("pin", "9999"))


def _m_expired(scn, step, args):
    scn.variables["expiry"] = str(args.get("expiry", "2001"))


def _m_invalid_cvv(scn, step, args):
    scn.variables["cvv"] = str(args.get("cvv", "000"))


def _m_bad_mac(scn, step, args):
    scn.variables["mac_key"] = "bad"


def _m_bad_arqc(scn, step, args):
    scn.variables["imk_ac"] = "bad"


def _m_missing_field(scn, step, args):
    step.remove.append(str(args.get("field", "25")))


def _m_invalid_pcode(scn, step, args):
    step.fields["3"] = str(args.get("processing_code", "990000"))


def _m_zero_amount(scn, step, args):
    step.amount = 0


def _m_over_balance(scn, step, args):
    step.amount = float(args.get("amount", 99999.0))


def _m_unknown_card(scn, step, args):
    scn.variables["pan"] = str(args.get("pan", "4761730000009999"))


def _m_duplicate(scn, step, args):
    scn.steps.insert(scn.steps.index(step) + 1, Step(
        id=f"{step.id}_dup", template=step.template, mti=step.mti, ref=step.id,
        description="Retransmission avec le même STAN / RRN",
        fields={**step.fields, "7": "{ref.7}", "11": "{ref.11}", "37": "{ref.37}"},
        amount=step.amount, expect={"39": "duplicate"}))


def _m_timeout(scn, step, args):
    scn.variables["magic"] = "timeout"
    step.expect = {"39": "timeout"}
    step.timeout_ms = int(args.get("timeout_ms", 3000))
    idx = scn.steps.index(step)
    scn.steps = scn.steps[:idx + 1] + [Step(
        id=f"{step.id}_adv", template="REVERSAL_ADVICE", ref=step.id,
        description="Annulation automatique après absence de réponse", expect={"39": "approved"})]


def _m_orphan_reversal(scn, step, args):
    scn.steps = [Step(id="rev_orphan", template="REVERSAL", description="Annulation d'une transaction inexistante",
                      expect={"39": "original_not_found"})]


def _m_issuer_down(scn, step, args):
    scn.variables["magic"] = "issuer_unavailable"


CATALOG: dict[str, Mutator] = {m.name: m for m in [
    Mutator("wrong_pin", "PIN erroné saisi par le porteur", "wrong_pin", _m_wrong_pin, _pin_ok),
    Mutator("expired_card", "Date d'expiration dépassée", "expired_card", _m_expired),
    Mutator("invalid_cvv", "CVV/iCVV de la piste invalide", "cvv_failure", _m_invalid_cvv, _has_track),
    Mutator("bad_mac", "MAC calculé avec une mauvaise clé", "security_violation", _m_bad_mac),
    Mutator("bad_arqc", "ARQC calculé avec une mauvaise IMK", "arqc_failure", _m_bad_arqc, _is_chip),
    Mutator("missing_field", "Champ obligatoire absent (arg: field)", "format_error", _m_missing_field),
    Mutator("invalid_processing_code", "Code traitement inconnu", "invalid_transaction", _m_invalid_pcode),
    Mutator("zero_amount", "Montant nul", "invalid_amount", _m_zero_amount),
    Mutator("amount_over_balance", "Montant supérieur à la provision", "insufficient_funds", _m_over_balance),
    Mutator("unknown_card", "PAN inconnu de l'émetteur", "invalid_card", _m_unknown_card),
    Mutator("duplicate_stan", "Retransmission du même message", "approved", _m_duplicate),
    Mutator("timeout_then_advice", "Pas de réponse puis annulation 0420", "timeout", _m_timeout),
    Mutator("orphan_reversal", "Annulation sans transaction d'origine", "original_not_found", _m_orphan_reversal),
    Mutator("issuer_unavailable", "Émetteur indisponible", "issuer_unavailable", _m_issuer_down),
]}


def apply(base: Scenario, name: str, args: dict | None = None, new_id: str | None = None) -> Scenario:
    if name not in CATALOG:
        raise ValueError(f"Mutateur inconnu : {name}. Disponibles : {', '.join(CATALOG)}")
    m = CATALOG[name]
    args = dict(args or {})
    scn = base.model_copy(deep=True)
    scn.id = new_id or f"{base.id}-{name.upper()}"
    scn.title = f"{base.title} — {m.description}"
    scn.mutation, scn.derived_from, scn.origin = name, None, "mutation"
    scn.mutation_args = {}
    scn.tags = sorted(set(scn.tags) | {"negative"})
    target = _target(scn, args.pop("step", None))
    m.fn(scn, target, args)
    if name not in ("duplicate_stan", "timeout_then_advice", "orphan_reversal"):
        target.expect = {"39": m.expect}   # un refus ne porte pas d'autorisation (DE38...)
        # Les étapes suivantes (ex. reversal) n'ont plus de sens après un refus
        scn.steps = scn.steps[:scn.steps.index(target) + 1]
    return scn


def applicable(name: str, card: CardProfile, terminal: TerminalProfile) -> bool:
    return CATALOG[name].requires(card, terminal)


def catalog_for_prompt() -> str:
    return "\n".join(f"- {m.name}: {m.description} (attendu: {m.expect})" for m in CATALOG.values())
