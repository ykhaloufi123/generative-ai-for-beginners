"""Chemin sans IA : analyse des besoins par règles et conception déterministe des scénarios.

La majorité des besoins de test monétique suivent quelques schémas récurrents (achat, retrait,
annulation, cas négatifs connus). Ce module les traite sans aucun token ; seuls les besoins
qu'il ne sait pas interpréter (marqués « complex ») sont envoyés au LLM.
"""
from __future__ import annotations

import re
import unicodedata

from ..dsl.models import Profiles, Scenario, Step
from ..referential.repository import Referential
from .intents import OPERATION_TEMPLATE, AnalysisResult, Intent


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


OPS = [
    ("balance_inquiry", r"consultation de solde|solde|balance"),
    ("preauth", r"pre-?autori|preauth|pre-auth|hotel|location de voiture"),
    ("completion", r"completion|cloture de pre"),
    ("refund", r"rembours|refund|credit marchand"),
    ("withdrawal", r"retrait|withdraw|cash|gab|dab|atm"),
    ("key_exchange", r"echange de cle|key exchange|changement de cle"),
    ("network", r"\becho\b|sign-?on|sign-?off|gestion reseau|0800"),
    ("reversal", r"annulation seule|reversal seul"),
    ("purchase", r"achat|paiement|purchase|vente|payment"),
]
CHANNELS = [
    ("contactless", r"sans contact|contactless|nfc|tap"),
    ("ecom", r"e-?commerce|en ligne|internet|vad|cnp|card not present"),
    ("magstripe", r"piste|magstripe|bande magnetique|swipe"),
    ("manual", r"saisie manuelle|manual entry|key-?entered"),
    ("emv", r"emv|puce|chip|contact\b|insertion"),
]
NEGATIVES = [
    ("wrong_pin", r"(pin|code) (errone|faux|incorrect|invalide)|wrong pin|bad pin"),
    ("expired_card", r"expir"),
    ("invalid_cvv", r"cvv|cvc|icvv"),
    ("bad_mac", r"\bmac\b"),
    ("bad_arqc", r"arqc|cryptogramme"),
    ("amount_over_balance", r"provision|solde insuffisant|insufficient|fonds insuffisants"),
    ("zero_amount", r"montant nul|montant (a|de) zero|zero amount"),
    ("missing_field", r"champ (obligatoire )?(absent|manquant)|missing field|erreur de format"),
    ("timeout_then_advice", r"timeout|time-?out|absence de reponse|pas de reponse|non reponse"),
    ("duplicate_stan", r"doublon|duplicate|retransmission|rejeu"),
    ("orphan_reversal", r"annulation (orpheline|sans origine|inexistante)"),
    ("unknown_card", r"carte inconnue|pan inconnu|unknown card"),
    ("issuer_unavailable", r"emetteur indisponible|issuer (down|unavailable)"),
    ("invalid_processing_code", r"code (de )?traitement (invalide|inconnu)"),
]
LOST = r"carte (perdue|volee|bloquee|opposee)|lost|stolen|opposition"
SCHEMES = [("visa", r"\bvisa\b"), ("mastercard", r"mastercard|\bmc\b|maestro"), ("local", r"domestique|local|cmi")]
_AMOUNT = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:mad|dh|dhs|dirhams?|eur|€|usd|\$)", re.I)
_REQ_ID = re.compile(r"\b((?:REQ|EXG|US|BR|RG)[-_ ]?\d+)\b", re.I)


def split_requirements(text: str, default_prefix: str = "REQ") -> list[tuple[str, str]]:
    """Découpe un texte en besoins : lignes « REQ-12 : ... » ou paragraphes/puces numérotés."""
    items: list[tuple[str, str]] = []
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s)", text) if b.strip()]
    n = 0
    for b in blocks:
        if b.lstrip().startswith("#"):          # titres Markdown
            continue
        b = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", b)
        m = _REQ_ID.search(b)
        if m:
            rid = m.group(1).upper().replace(" ", "-").replace("_", "-")
            body = (b[:m.start()] + b[m.end():]).strip(" :-–")
        else:
            n += 1
            rid, body = f"{default_prefix}-{n:03d}", b
        if body:
            items.append((rid, body))
    return items


def analyse(req_id: str, text: str) -> AnalysisResult:
    t = _norm(text)
    ops = [o for o, rx in OPS if re.search(rx, t)]
    chans = [c for c, rx in CHANNELS if re.search(rx, t)]
    negs = [n for n, rx in NEGATIVES if re.search(rx, t)]
    scheme = next((s for s, rx in SCHEMES if re.search(rx, t)), "any")
    amounts = [float(a.replace(",", ".")) for a in _AMOUNT.findall(text)]
    questions: list[str] = []
    intents: list[Intent] = []
    op = ops[0] if ops else ("purchase" if negs else None)
    if op is None:
        return AnalysisResult(intents=[Intent(id=f"{req_id}-I1", requirement=req_id, summary=text[:140],
                                              operation="other", complex=True)],
                              questions=[f"{req_id} : opération non identifiée, préciser (achat, retrait...)"])
    if op == "completion":
        op = "preauth"
    channel = chans[0] if chans else ("atm" if op == "withdrawal" else "any")
    cond = "none"
    if re.search(r"au-?dela|superieur|depass|above|over|plus de", t) and channel == "contactless":
        cond = "above_cvm_limit"
    elif re.search(r"en-?dessous|inferieur|sous le plafond|below|moins de", t) and channel == "contactless":
        cond = "below_cvm_limit"
    follow = "none"
    if re.search(r"annul|revers|0400", t) and op != "reversal":
        follow = "reversal"
    if re.search(r"completion|cloture|0220", t) and op == "preauth":
        follow = "completion"
    base = Intent(id=f"{req_id}-I1", requirement=req_id, summary=text[:140].strip(), operation=op, channel=channel,
                  scheme=scheme, amount=amounts[0] if amounts else None, amount_condition=cond, followed_by=follow,
                  priority="P1" if re.search(r"critique|bloquant|p1|obligatoire", t) else "P2")
    lost = bool(re.search(LOST, t))
    is_negative_only = bool(negs or lost) and not re.search(r"nominal|accept|approuv|succes|reussi", t)
    if not is_negative_only:
        intents.append(base)
    for i, n in enumerate(negs, 2):
        intents.append(base.model_copy(update={"id": f"{req_id}-I{i}", "negative": n, "followed_by": "none",
                                               "summary": f"{base.summary[:100]} — cas négatif {n}"}))
    if lost:
        intents.append(base.model_copy(update={"id": f"{req_id}-I{len(intents) + 2}", "expect": "lost_card",
                                               "summary": "Carte en opposition", "followed_by": "none"}))
    if op == "reversal":
        intents = [base.model_copy(update={"negative": "orphan_reversal"})] if "orphan_reversal" in negs else \
            [base.model_copy(update={"operation": "purchase", "followed_by": "reversal"})]
    if channel == "any" and op in ("purchase", "preauth", "refund"):
        questions.append(f"{req_id} : canal non précisé — EMV contact par défaut (préciser si sans contact, piste...)")
    return AnalysisResult(intents=intents, questions=questions)


# ---------------------------------------------------------------------------- conception déterministe
def pick_card(ref: Referential, channel: str, scheme: str, status: str = "active"):
    ctype = {"atm": "emv", "any": "emv"}.get(channel, channel)
    cands = [c for c in ref.cards.values() if c.status == status and c.card_type == ctype and
             (scheme == "any" or c.scheme.lower() == scheme)]
    if not cands and status == "active":
        cands = [c for c in ref.cards.values() if c.status == status and c.card_type == ctype]
    if not cands and ctype == "ecom":
        cands = [c for c in ref.cards.values() if c.status == status and c.card_type == "emv"]
    return cands[0] if cands else None


def pick_terminal(ref: Referential, channel: str, operation: str):
    kind = "atm" if operation == "withdrawal" or channel == "atm" else "ecom" if channel == "ecom" else "pos"
    cands = [t for t in ref.terminals.values() if t.terminal_kind == kind]
    return cands[0] if cands else (next(iter(ref.terminals.values())) if ref.terminals else None)


def design(intent: Intent, ref: Referential) -> Scenario | None:
    """Intention -> scénario DSL ; None si l'intention sort des schémas connus (-> LLM)."""
    if intent.complex or intent.operation == "other":
        return None
    tpl = OPERATION_TEMPLATE.get(intent.operation)
    if not tpl or tpl not in ref.templates:
        return None
    status = "lost" if intent.expect == "lost_card" else "active"
    card = pick_card(ref, intent.channel, intent.scheme, status) if tpl not in ("ECHO", "KEY_EXCHANGE") else None
    term = pick_terminal(ref, intent.channel, intent.operation)
    if tpl not in ("ECHO", "KEY_EXCHANGE") and (card is None or term is None):
        return None
    amount = intent.amount
    if intent.amount_condition in ("above_cvm_limit", "below_cvm_limit") and term:
        amount = term.contactless_cvm_limit * (1.5 if intent.amount_condition == "above_cvm_limit" else 0.5)
    elif intent.amount_condition == "zero":
        amount = 0
    steps = [Step(id="s1", template=tpl, amount=amount,
                  expect={"39": intent.expect} if intent.expect != "approved" else {})]
    if intent.followed_by == "reversal":
        steps.append(Step(id="s2", template="REVERSAL", ref="s1"))
    elif intent.followed_by == "reversal_advice":
        steps.append(Step(id="s2", template="REVERSAL_ADVICE", ref="s1"))
    elif intent.followed_by == "completion" and "COMPLETION" in ref.templates:
        steps.append(Step(id="s2", template="COMPLETION", ref="s1"))
    scn = Scenario(id=intent.id.replace("_", "-"), title=intent.summary[:120], requirement=[intent.requirement],
                   tags=[intent.channel if intent.channel != "any" else "emv", intent.operation],
                   priority=intent.priority,
                   profiles=Profiles(card=card.id if card else None, terminal=term.id if term else None),
                   steps=steps, origin="heuristic")
    if intent.negative:
        if intent.negative == "amount_over_balance" and card:
            scn.steps[0].amount = card.balance + 100
        scn.mutation = intent.negative
    return scn
