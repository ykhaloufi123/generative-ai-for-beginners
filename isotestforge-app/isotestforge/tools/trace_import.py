"""Import de traces réelles -> scénarios DSL de régression, sans IA et sans conserver de donnée porteur.

Formats acceptés :
  - JSONL du simulateur (ou d'un outil maison) : {"request": {...}, "response": {...}}
  - logs jPOS XML contenant des blocs <isomsg>
  - lignes « HEX: 0200... » (message brut en hexadécimal)

Chaque PAN rencontré est remplacé par un profil de TEST du même schéma et du même type de carte.
Seul le préfixe (schéma) du PAN réel est utilisé pour ce choix ; il n'est jamais écrit.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..dsl.models import Profiles, Scenario, Step, Suite
from ..iso.message import IsoMessage
from ..iso.packager import GenericPackager
from ..referential.repository import Referential
from .decoder import parse_jpos_xml

SCHEME = {"4": "VISA", "5": "MASTERCARD", "2": "MASTERCARD", "6": "LOCAL"}
_ISOMSG = re.compile(r"<isomsg[^>]*>.*?</isomsg>", re.S)


def _from_dict(d: dict) -> IsoMessage:
    m = IsoMessage(d.get("mti", ""))
    for k, v in (d.get("fields") or {}).items():
        m.fields[int(k)] = v
    for k, lst in (d.get("tlv") or {}).items():
        m.tlv[int(k)] = [(x["tag"], x["value"]) for x in lst]
    return m


def read_messages(path: str | Path, pk: GenericPackager | None = None) -> list[tuple[IsoMessage, IsoMessage | None]]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    pairs: list[tuple[IsoMessage, IsoMessage | None]] = []
    if text.lstrip().startswith("{"):
        for line in text.splitlines():
            if line.strip():
                d = json.loads(line)
                if "request" in d:
                    pairs.append((_from_dict(d["request"]), _from_dict(d["response"]) if d.get("response") else None))
        return pairs
    if "<isomsg" in text:
        msgs = [parse_jpos_xml(b) for b in _ISOMSG.findall(text)]
    else:
        if pk is None:
            raise ValueError("Un packager est nécessaire pour décoder des messages hexadécimaux")
        msgs = []
        for line in text.splitlines():
            m = re.search(r"HEX:\s*([0-9A-Fa-f]+)", line)
            if m:
                msgs.append(pk.unpack(bytes.fromhex(m.group(1))))
    # association requête / réponse (MTI de réponse + STAN)
    i = 0
    while i < len(msgs):
        req = msgs[i]
        resp = None
        if int(req.mti[2]) % 2 == 0 and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            if nxt.mti == req.response_mti() and nxt.get(11) == req.get(11):
                resp = nxt
                i += 1
        if int(req.mti[2]) % 2 == 0:
            pairs.append((req, resp))
        i += 1
    return pairs


def _card_type(de22: str) -> str:
    return {"05": "emv", "07": "contactless", "90": "magstripe", "02": "magstripe", "81": "ecom",
            "01": "manual"}.get(de22[:2], "emv")


def _template(m: IsoMessage) -> str | None:
    pc = (m.get(3) or "000000")[:2]
    if m.mti.startswith("08"):
        return {"001": "SIGNON", "002": "SIGNOFF", "301": "ECHO", "101": "KEY_EXCHANGE"}.get(m.get(70) or "", "ECHO")
    if m.mti.startswith("04"):
        return "REVERSAL_ADVICE" if m.mti.startswith("042") else "REVERSAL"
    if m.mti.startswith("022"):
        return "COMPLETION"
    if m.mti.startswith("01"):
        return "BALANCE_INQUIRY" if pc == "31" else "PREAUTH"
    if m.mti.startswith("02"):
        return {"00": "PURCHASE", "01": "WITHDRAWAL", "20": "REFUND", "31": "BALANCE_INQUIRY"}.get(pc, "PURCHASE")
    return None


def import_traces(path: str | Path, ref: Referential, host_id: str, suite_name: str = "TRACE_REGRESSION",
                  pk: GenericPackager | None = None, requirement: str | None = None) -> tuple[Suite, dict]:
    pairs = read_messages(path, pk)
    stats = {"messages": len(pairs), "scenarios": 0, "skipped": 0, "unmapped_cards": 0}
    term_by_tid = {t.tid: t.id for t in ref.terminals.values()}
    default_term = next(iter(ref.terminals)) if ref.terminals else None
    scenarios: list[Scenario] = []
    open_txn: dict[tuple, tuple[Scenario, str]] = {}   # (TID, STAN) -> (scénario, id d'étape)

    for idx, (req, resp) in enumerate(pairs, 1):
        tpl = _template(req)
        if not tpl:
            stats["skipped"] += 1
            continue
        tid = (req.get(41) or "").strip()
        expect = {"39": resp.get(39)} if resp and resp.get(39) else {}
        if tpl in ("REVERSAL", "REVERSAL_ADVICE") and req.get(90):
            key = (tid, req.get(90)[4:10])
            if key in open_txn:
                scn, sid = open_txn[key]
                scn.steps.append(Step(id=f"s{len(scn.steps) + 1}", template=tpl, ref=sid, expect=expect))
                continue
        pan = req.get(2) or (req.get(35) or "").split("=")[0]
        card_id = None
        if pan:
            scheme = SCHEME.get(pan[0], "LOCAL")
            ctype = _card_type(req.get(22) or "051")
            cands = [c for c in ref.cards.values() if c.status == "active" and c.card_type == ctype
                     and c.scheme == scheme] or [c for c in ref.cards.values()
                                                 if c.status == "active" and c.card_type == ctype]
            if not cands:
                stats["unmapped_cards"] += 1
                stats["skipped"] += 1
                continue
            card_id = cands[0].id
        amount = int(req.get(4)) / 100 if (req.get(4) or "").isdigit() else None
        step = Step(id="s1", template=tpl, amount=amount if tpl not in ("SIGNON", "SIGNOFF", "ECHO", "KEY_EXCHANGE")
                    else None, expect=expect)
        scn = Scenario(id=f"TRC-{idx:04d}", title=f"Trace {idx} : {tpl.lower()} {req.mti}",
                       requirement=[requirement] if requirement else [], tags=["trace", "regression"],
                       profiles=Profiles(card=card_id, terminal=term_by_tid.get(tid, default_term)),
                       steps=[step], origin="trace")
        scenarios.append(scn)
        if req.get(11):
            open_txn[(tid, req.get(11))] = (scn, "s1")
    stats["scenarios"] = len(scenarios)
    return Suite(suite=suite_name, host=host_id, description=f"Importée depuis {Path(path).name}",
                 scenarios=scenarios), stats
