"""Simulateur hôte ISO 8583 (asyncio) : émetteur + switch simplifiés, piloté par le référentiel.

Il vérifie réellement PIN (ZPK), MAC (TAK), ARQC (IMK-AC), CVV/iCVV, expiration, provision,
statut carte, doublons et annulations, et sait injecter des pannes (montants magiques,
règles YAML). Il permet de tester les JMX générés sans plateforme disponible.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from ..crypto import card as cardlib
from ..crypto import emv, keys as keylib, mac as maclib, pin as pinlib
from ..iso.channel import ChannelSpec
from ..iso.message import IsoMessage
from ..referential.models import CardProfile
from ..referential.repository import Referential

log = logging.getLogger("itf.simulator")


@dataclass
class SimConfig:
    host_spec: str = "SWITCH_SIM"
    listen: str = "0.0.0.0"
    port: int = 8583
    verify_mac: bool = True
    verify_pin: bool = True
    verify_cvv: bool = True
    verify_arqc: bool = True
    verify_expiry: bool = True
    pin_tries_limit: int = 3
    supported_processing_codes: list[str] = field(default_factory=lambda: ["00", "01", "20", "31"])
    default_delay_ms: int = 0
    approve_unknown_terminals: bool = True
    trace_file: str | None = None
    rules: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None) -> "SimConfig":
        if not path or not Path(path).exists():
            return cls()
        return cls(**(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}))


@dataclass
class Txn:
    key: tuple
    mti: str
    pan: str
    amount: int
    stan: str
    dt7: str
    rrn: str
    auth_id: str
    pcode: str
    reversed: bool = False


class HostSimulator:
    def __init__(self, ref: Referential, cfg: SimConfig):
        self.ref = ref
        self.cfg = cfg
        self.host = ref.host(cfg.host_spec)
        self.pk = ref.packager(cfg.host_spec)
        ch = self.host.channel
        self.channel = ChannelSpec.from_config(ch.kind, ch.header, ch.header_hex, ch.length_digits)
        self.balances = {c.pan: int(round(c.balance * 100)) for c in ref.cards.values()}
        self.pin_tries: dict[str, int] = {}
        self.txns: dict[tuple, Txn] = {}
        self.seen: set[tuple] = set()
        self.stats = {"received": 0, "responded": 0, "dropped": 0, "by_code": {}}
        self.by_pan = {c.pan: c for c in ref.cards.values()}
        self._trace = None
        if cfg.trace_file:
            p = Path(cfg.trace_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._trace = p.open("a", encoding="utf-8")

    # ------------------------------------------------------------------ outils
    def code(self, semantic: str) -> str:
        return self.host.code(semantic)

    def _keys(self, card: CardProfile | None, name: str) -> str | None:
        ks = card.keyset if card and card.keyset else (next(iter(self.ref.keysets)) if self.ref.keysets else None)
        if not ks:
            return None
        try:
            return self.ref.key(ks, name)
        except Exception:
            return None

    def _default_key(self, name: str) -> str | None:
        return self._keys(None, name)

    def _mac_ok(self, raw: bytes, msg: IsoMessage) -> bool | None:
        mf = self.host.mac_field
        if not mf or mf not in msg.fields:
            return None
        card = self.by_pan.get(self._pan(msg))
        tak = self._keys(card, "tak")
        if not tak:
            return None
        packed_len = len(self.pk.pack_field(self.pk.field(mf), msg))
        expected = maclib.retail_mac(tak, raw[:-packed_len], 2).hex().upper()
        return msg.fields[mf].upper()[:16] == expected

    def _set_mac(self, resp: IsoMessage, card: CardProfile | None) -> None:
        mf = self.host.mac_field
        tak = self._keys(card, "tak")
        if not (mf and tak):
            return
        resp.fields[mf] = "0" * 16
        raw = self.pk.pack(resp)
        packed_len = len(self.pk.pack_field(self.pk.field(mf), resp))
        resp.fields[mf] = maclib.retail_mac(tak, raw[:-packed_len], 2).hex().upper()

    @staticmethod
    def _pan(msg: IsoMessage) -> str:
        if msg.get(2):
            return msg.get(2)
        t2 = msg.get(35) or ""
        return t2.split("=")[0].split("D")[0]

    def _rule_match(self, rule: dict, msg: IsoMessage) -> bool:
        for k, v in (rule.get("when") or {}).items():
            if k == "mti" and msg.mti != str(v):
                return False
            if k == "pan_prefix" and not self._pan(msg).startswith(str(v)):
                return False
            if k == "amount_eq" and msg.get(4) != str(v):
                return False
            if k == "amount_gt" and int(msg.get(4) or 0) <= int(v):
                return False
            if k == "tid" and (msg.get(41) or "").strip() != str(v):
                return False
            if k.startswith("field.") and msg.get(k[6:]) != str(v):
                return False
        return True

    # ------------------------------------------------------------------ logique émetteur
    def decide(self, raw: bytes, msg: IsoMessage) -> tuple[str | None, dict]:
        """Retourne (DE39 | None si pas de réponse, champs additionnels)."""
        extra: dict = {}
        mti = msg.mti
        amount = msg.get(4) or ""

        for rule in self.cfg.rules:
            if self._rule_match(rule, msg):
                if rule.get("drop"):
                    return None, {}
                if rule.get("delay_ms"):
                    extra["_delay"] = int(rule["delay_ms"])
                if rule.get("respond"):
                    return str(rule["respond"]), extra

        for sem, magic in self.host.magic_amounts.items():
            if amount == magic and mti[:2] in ("01", "02"):
                if sem == "timeout":
                    return None, {}
                return self.code(sem), extra

        if mti.startswith("08"):
            nmic = msg.get(70) or ""
            if nmic == "101":
                zmk = self._default_key("zmk")
                data = msg.get(48) or ""
                try:
                    clear = keylib.decrypt_under(data[:32], zmk)
                    ok = keylib.kcv(clear) == data[32:38].upper()
                except Exception:
                    ok = False
                return (self.code("approved") if ok else self.code("format_error")), extra
            return (self.code("approved") if nmic in ("001", "002", "301", "161") else self.code("format_error")), extra

        missing = [n for n in self.host.mandatory.get(mti, []) if not msg.has(n)]
        if missing:
            extra["_reason"] = f"champs manquants {missing}"
            return self.code("format_error"), extra

        mac_ok = self._mac_ok(raw, msg) if self.cfg.verify_mac else None
        if mac_ok is False:
            extra["_reason"] = "MAC invalide"
            return self.code("security_violation"), extra

        if mti[:2] in ("04",):
            return self._reversal(msg, extra)
        if mti.startswith("022"):
            return self._completion(msg, extra)

        # ---- 0100 / 0200
        dup_key = ((msg.get(41) or "").strip(), msg.get(11), msg.get(7), msg.get(37))
        if dup_key in self.seen:
            return self.code("duplicate"), extra
        self.seen.add(dup_key)

        pan = self._pan(msg)
        card = self.by_pan.get(pan)
        if not card:
            return self.code("invalid_card"), extra
        pcode = (msg.get(3) or "000000")[:2]
        if pcode not in self.cfg.supported_processing_codes:
            return self.code("invalid_transaction"), extra
        amt = int(amount or 0)
        if amt == 0 and pcode != "31":
            return self.code("invalid_amount"), extra
        if card.status in ("lost", "stolen", "blocked"):
            return self.code({"lost": "lost_card", "stolen": "stolen_card"}.get(card.status, "do_not_honor")), extra

        exp = msg.get(14) or ""
        t2 = msg.get(35) or ""
        if "=" in t2:
            exp = t2.split("=")[1][:4]
        if self.cfg.verify_expiry and exp and exp < datetime.now().strftime("%y%m"):
            return self.code("expired_card"), extra

        cvk = self._keys(card, "cvk")
        if self.cfg.verify_cvv and cvk and "=" in t2:
            after = t2.split("=")[1]
            svc, disc = after[4:7], after[7:]
            if len(disc) >= 3:
                chip = (msg.get(22) or "")[:2] in ("05", "07")
                expected = cardlib.cvv(cvk, pan, after[:4], "999" if chip else svc)
                if disc[-3:] != expected:
                    return self.code("cvv_failure"), extra

        if msg.get(52) and self.cfg.verify_pin:
            zpk = self._keys(card, "zpk")
            if zpk:
                tries = self.pin_tries.get(pan, 0)
                if tries >= self.cfg.pin_tries_limit:
                    return self.code("pin_tries_exceeded"), extra
                if not pinlib.verify(msg.get(52), zpk, pan, card.pin):
                    self.pin_tries[pan] = tries + 1
                    return self.code("wrong_pin"), extra
                self.pin_tries[pan] = 0

        icc = dict(msg.tlv.get(55, []))
        imk = self._keys(card, "imk_ac")
        if icc.get("9F26") and imk:
            psn = (msg.get(23) or card.psn)[-2:]
            if self.cfg.verify_arqc and not emv.verify_arqc(imk, pan, psn, icc):
                extra["_reason"] = "ARQC invalide"
                return self.code("arqc_failure"), extra
            arpc = emv.compute_arpc(imk, pan, psn, icc, icc["9F26"], "3030")
            extra["_icc_resp"] = [("91", arpc + "3030"), ("8A", "3030")]

        bal = self.balances.get(pan, 0)
        if pcode in ("00", "01"):
            if amt > bal:
                return self.code("insufficient_funds"), extra
            self.balances[pan] = bal - amt
        elif pcode == "20":
            self.balances[pan] = bal + amt
        if pcode == "31":
            extra["54"] = "1001" + card.currency.zfill(3) + "C" + str(self.balances[pan]).zfill(12)
        auth = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        extra["38"] = auth
        key = ((msg.get(41) or "").strip(), msg.get(11), msg.get(7))
        self.txns[key] = Txn(key, mti, pan, amt, msg.get(11), msg.get(7), msg.get(37), auth, pcode)
        return self.code("approved"), extra

    def _find_original(self, msg: IsoMessage) -> Txn | None:
        de90 = msg.get(90) or ""
        stan, dt7 = de90[4:10], de90[10:20]
        return self.txns.get(((msg.get(41) or "").strip(), stan, dt7))

    def _reversal(self, msg: IsoMessage, extra: dict):
        orig = self._find_original(msg)
        if not orig:
            # Un advice (0420) est toujours acquitté, même si l'origine n'a jamais été autorisée
            return (self.code("approved") if msg.mti.startswith("042") else self.code("original_not_found")), extra
        if not orig.reversed:
            orig.reversed = True
            if orig.pcode in ("00", "01"):
                self.balances[orig.pan] = self.balances.get(orig.pan, 0) + orig.amount
            elif orig.pcode == "20":
                self.balances[orig.pan] = self.balances.get(orig.pan, 0) - orig.amount
        return self.code("approved"), extra

    def _completion(self, msg: IsoMessage, extra: dict):
        rrn, auth = msg.get(37), msg.get(38)
        for t in self.txns.values():
            if t.rrn == rrn and t.auth_id == auth and t.mti.startswith("01"):
                return self.code("approved"), extra
        return self.code("original_not_found"), extra

    # ------------------------------------------------------------------ réponse
    def build_response(self, msg: IsoMessage, code: str, extra: dict) -> IsoMessage:
        resp = msg.copy()
        resp.mti = msg.response_mti()
        for n in (35, 52, 55, 53, 14, 23, self.host.mac_field or 0):
            resp.remove(n)
        resp.fields[39] = code
        for k, v in extra.items():
            if not k.startswith("_"):
                resp.fields[int(k)] = v
        if code != self.code("approved"):
            resp.fields.pop(38, None)
        if "_icc_resp" in extra and code == self.code("approved"):
            resp.tlv[55] = extra["_icc_resp"]
        card = self.by_pan.get(self._pan(msg))
        if self.host.mac_field and self.host.mac_field in msg.fields:
            self._set_mac(resp, card)
        return resp

    def handle(self, raw: bytes) -> tuple[bytes | None, dict]:
        self.stats["received"] += 1
        t0 = time.time()
        try:
            msg = self.pk.unpack(raw)
        except Exception as e:
            log.warning("Message indécodable : %s", e)
            self.stats["dropped"] += 1
            return None, {"error": str(e)}
        code, extra = self.decide(raw, msg)
        info = {"ts": datetime.now().isoformat(timespec="milliseconds"), "mti": msg.mti,
                "pan": cardlib.mask_pan(self._pan(msg)), "stan": msg.get(11), "amount": msg.get(4),
                "tid": (msg.get(41) or "").strip(), "de39": code, "reason": extra.get("_reason"),
                "request": _masked(msg)}
        if code is None:
            self.stats["dropped"] += 1
            self._write_trace(info)
            return None, info
        resp = self.build_response(msg, code, extra)
        out = self.pk.pack(resp)
        info["response"] = _masked(resp)
        info["ms"] = round((time.time() - t0) * 1000, 2)
        info["_delay"] = extra.get("_delay", self.cfg.default_delay_ms)
        self.stats["responded"] += 1
        self.stats["by_code"][code] = self.stats["by_code"].get(code, 0) + 1
        self._write_trace(info)
        return out, info

    def _write_trace(self, info: dict) -> None:
        if self._trace:
            self._trace.write(json.dumps({k: v for k, v in info.items() if not k.startswith("_")},
                                         ensure_ascii=False) + "\n")
            self._trace.flush()

    # ------------------------------------------------------------------ serveur TCP
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Connexion %s", peer)
        try:
            while True:
                raw = await self.channel.read(reader)
                out, info = self.handle(raw)
                log.info("%s %s STAN=%s -> %s %s", info.get("mti"), info.get("pan"), info.get("stan"),
                         info.get("de39") or "(pas de réponse)", info.get("reason") or "")
                if out is None:
                    continue
                if info.get("_delay"):
                    await asyncio.sleep(info["_delay"] / 1000)
                writer.write(self.channel.frame(out))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def serve(self, host: str | None = None, port: int | None = None) -> asyncio.base_events.Server:
        server = await asyncio.start_server(self._client, host or self.cfg.listen, port or self.cfg.port)
        log.info("Simulateur %s en écoute sur %s:%s (%s)", self.host.id, host or self.cfg.listen,
                 port or self.cfg.port, self.channel.kind)
        return server

    async def run_forever(self) -> None:
        server = await self.serve()
        async with server:
            await server.serve_forever()


def _masked(msg: IsoMessage) -> dict:
    d = msg.to_dict()
    f = d["fields"]
    if "2" in f:
        f["2"] = cardlib.mask_pan(f["2"])
    if "35" in f:
        f["35"] = cardlib.mask_pan(f["35"].split("=")[0]) + "=****"
    for k in ("52", "64", "128", "48"):
        if k in f:
            f[k] = "*" * 8
    return d
