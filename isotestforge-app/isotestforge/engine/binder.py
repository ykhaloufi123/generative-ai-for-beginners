"""Data Binder : transforme des scénarios DSL en messages concrets (sans IA).

Il résout les profils, applique les règles métier (mode d'entrée, CVM, sans contact au-delà
du plafond, chaînage des annulations, DE90), construit les données EMV et prépare la
configuration crypto. Les valeurs dynamiques (STAN, RRN, dates) restent des fonctions JMeter
qui mémorisent leur valeur dans une variable, pour être réutilisées par les étapes chaînées.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..crypto import card as cardlib
from ..crypto import keys as keylib
from ..crypto import pin as pinlib
from ..dsl.models import Scenario, Step, Suite
from ..referential.models import CardProfile, HostSpec, KeySet, MessageTemplate, TerminalProfile
from ..referential.repository import Referential

BAD_KEY = "0123456789ABCDEFFEDCBA9876543210"   # clé volontairement fausse (mutations)
_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


class BindError(Exception):
    pass


@dataclass
class MsgField:
    key: str              # "4", "55.3"
    value: str
    tag: str = ""         # tag EMV pour les champs TLV
    comment: str = ""


@dataclass
class CryptoConfig:
    pin: bool = False
    mac: bool = False
    arqc: bool = False
    pin_key_var: str = ""
    mac_key_var: str = ""
    imk_var: str = ""
    mac_algorithm: str = "retail"
    mac_field: int | None = None
    pan: str = ""
    psn: str = ""


@dataclass
class BoundStep:
    id: str
    name: str
    mti: str
    fields: list[MsgField]
    crypto: CryptoConfig
    expect: dict[str, list[str]]
    timeout_ms: int
    delay_ms: int = 0
    template: str = ""
    ref: str | None = None
    var_prefix: str = ""
    description: str = ""
    expects_timeout: bool = False

    def field_value(self, key: str) -> str | None:
        for f in self.fields:
            if f.key == key:
                return f.value
        return None

    @property
    def field_map(self) -> dict[str, str]:
        return {f.key if not f.tag else f"55.{f.tag}": f.value for f in self.fields}


@dataclass
class BoundScenario:
    id: str
    title: str
    group: str
    requirements: list[str]
    tags: list[str]
    priority: str
    card_id: str | None
    terminal_id: str | None
    keyset_id: str | None
    origin: str
    mutation: str | None
    steps: list[BoundStep] = field(default_factory=list)


@dataclass
class BoundSuite:
    suite: str
    host_id: str
    host: HostSpec
    scenarios: list[BoundScenario]
    setup_steps: list[BoundStep]
    teardown_steps: list[BoundStep]
    keysets: list[str]
    warnings: list[str] = field(default_factory=list)


def var_name(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(parts))


def key_var(kind: str, keyset: str) -> str:
    return var_name(kind.upper(), keyset)


class Binder:
    def __init__(self, ref: Referential, suite: Suite):
        self.ref = ref
        self.suite = suite
        self.host = ref.host(suite.host)
        self.atc: dict[str, int] = {}
        self.warnings: list[str] = []
        self.keysets_used: set[str] = set()

    # ------------------------------------------------------------------ utilitaires
    def _amount12(self, amount: float | None) -> str:
        return str(int(round((amount or 0) * 100))).zfill(12)

    def _next_atc(self, card: CardProfile) -> str:
        cur = self.atc.get(card.id, int(card.emv.atc, 16))
        self.atc[card.id] = cur + 1
        return format(cur + 1, "04X")

    def _resolve_expect(self, raw: dict) -> tuple[dict[str, list[str]], bool]:
        out: dict[str, list[str]] = {}
        timeout = False
        for k, v in raw.items():
            vals = v if isinstance(v, list) else [v]
            res = []
            for x in vals:
                x = str(x)
                if x == "timeout":
                    timeout = True
                    continue
                if x in ("present", "absent") or x.startswith("re:"):
                    res.append(x)
                else:
                    res.append(self.host.code(x))
            if res:
                out[str(k)] = res
        return out, timeout

    @staticmethod
    def _fill(text: str, ctx: dict) -> str:
        def repl(m):
            path = m.group(1)
            cur = ctx
            for part in path.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = getattr(cur, part, None)
                if cur is None:
                    raise BindError(f"Variable de template inconnue : {{{path}}}")
            return str(cur)
        return _PLACEHOLDER.sub(repl, text)

    # ------------------------------------------------------------------ règles métier
    @staticmethod
    def entry_mode(card: CardProfile, term: TerminalProfile) -> str:
        pin_cap = "1" if term.pin_capable and term.terminal_kind != "ecom" else "2"
        if term.terminal_kind == "ecom" or card.card_type == "ecom":
            return "812"
        if card.card_type == "manual":
            return "01" + pin_cap
        return {"emv": "05", "contactless": "07", "magstripe": "90"}[card.card_type] + pin_cap

    def needs_pin(self, card: CardProfile, term: TerminalProfile, step: Step, amount: float, mti: str) -> bool:
        if not mti.startswith(("01", "02")):
            return False
        if step.pin:
            return True
        if not term.pin_capable or term.terminal_kind == "ecom":
            return False
        if card.card_type == "contactless":
            return amount > term.contactless_cvm_limit   # règle : PIN au-delà du plafond sans contact
        return card.cvm == "online_pin"

    # ------------------------------------------------------------------ cœur
    def bind_scenario(self, scn: Scenario) -> BoundScenario:
        card = self.ref.card(scn.profiles.card) if scn.profiles.card else None
        term = self.ref.terminal(scn.profiles.terminal) if scn.profiles.terminal else None
        ks_id = scn.profiles.keys or (card.keyset if card else None) or self.suite.defaults.keys
        ks = self.ref.keyset(ks_id) if ks_id else None
        if ks_id:
            self.keysets_used.add(ks_id)
        bs = BoundScenario(scn.id, scn.title, scn.group or (scn.tags[0] if scn.tags else "general"),
                           scn.requirement, scn.tags, scn.priority, card.id if card else None,
                           term.id if term else None, ks_id, scn.origin, scn.mutation)
        done: dict[str, BoundStep] = {}
        for step in scn.steps:
            b = self.bind_step(scn, step, card, term, ks, done)
            done[step.id] = b
            bs.steps.append(b)
        return bs

    def bind_step(self, scn: Scenario, step: Step, card: CardProfile | None, term: TerminalProfile | None,
                  ks: KeySet | None, done: dict[str, BoundStep]) -> BoundStep:
        tpl: MessageTemplate | None = self.ref.template(step.template) if step.template else None
        mti = step.mti or tpl.mti
        is_chain = bool(tpl and tpl.chain != "none")
        prefix = var_name("V", scn.id, step.id)
        rprefix = var_name("R", scn.id, step.id)
        variables = dict(scn.variables)
        amount = step.amount if step.amount is not None else float(variables.get("amount", self.suite.defaults.amount))
        if is_chain and step.ref and step.amount is None and step.ref in done:
            amount = None  # copié depuis l'étape d'origine
        ref_step = done.get(step.ref) if step.ref else None
        if step.ref and not ref_step:
            raise BindError(f"{scn.id}.{step.id} : étape de référence '{step.ref}' inconnue ou postérieure")

        # Surcharges carte (mutations)
        pan = str(variables.get("pan", card.pan if card else ""))
        expiry = str(variables.get("expiry", card.expiry if card else ""))
        cvk = self.ref.key(ks.id, "cvk") if ks and ks.cvk else None

        def ref_expr(de: str) -> str:
            if not ref_step:
                return ""
            v = ref_step.field_value(de)
            if v is None:
                return ""
            if "${__" in v:  # valeur dynamique mémorisée dans une variable
                return "${" + var_name(ref_step.var_prefix, de) + "}"
            return v

        amount12 = self._amount12(amount) if amount is not None else ref_expr("4")
        magic = variables.get("magic")
        if magic:
            if magic not in self.host.magic_amounts:
                raise BindError(f"Montant magique '{magic}' non défini pour l'hôte {self.host.id}")
            amount12 = self.host.magic_amounts[magic]

        track2 = ""
        if card:
            disc = ""
            if "cvv" in variables:
                disc = "00000" + str(variables["cvv"])
            elif cvk:
                svc = "999" if card.card_type in ("emv", "contactless") else card.service_code
                disc = "00000" + cardlib.cvv(cvk, pan, expiry, svc)
            track2 = cardlib.track2(pan, expiry, card.service_code, disc)

        proc = step.fields.get("3") or (tpl.processing_code if tpl and tpl.processing_code else "000000")
        acq = term.acquirer_id if term else "0"
        orig_data = ""
        if tpl and tpl.chain == "reversal":
            if ref_step:
                orig_data = ref_step.mti + ref_expr("11") + ref_expr("7") + acq.zfill(11) + "0" * 11
            else:  # annulation orpheline : référence fictive
                orig_data = "0200" + "999999" + "0101000000" + acq.zfill(11) + "0" * 11

        keys_ctx = {}
        if ks and ks.zpk and ks.zmk:
            zpk, zmk = self.ref.key(ks.id, "zpk"), self.ref.key(ks.id, "zmk")
            keys_ctx = {"zpk_under_zmk": keylib.encrypt_under(zpk, zmk), "zpk_kcv": keylib.kcv(zpk)}

        ctx = {
            "card": {"pan": pan, "expiry": expiry, "psn": card.psn if card else "", "track2": track2,
                     "service_code": card.service_code if card else ""} if card else {},
            "terminal": term.model_dump() if term else {},
            "amount": amount12, "proc": proc,
            "entry_mode": self.entry_mode(card, term) if card and term else "000",
            "stan": "${__RandomString(6,0123456789," + var_name(prefix, "11") + ")}",
            "dt7": "${__time(MMddHHmmss," + var_name(prefix, "7") + ")}",
            "time12": "${__time(HHmmss," + var_name(prefix, "12") + ")}",
            "date13": "${__time(MMdd," + var_name(prefix, "13") + ")}",
            "rrn": "${__RandomString(12,0123456789," + var_name(prefix, "37") + ")}",
            "original_data": orig_data, "keys": keys_ctx,
            "ref": _RefCtx(ref_step, ref_expr),
        }

        # 1) champs du template (+ copie depuis l'étape d'origine)
        values: dict[str, str] = {}
        if tpl:
            for de in tpl.copy_from_ref:
                v = ref_expr(de)
                if v:
                    values[de] = v
            if tpl.chain != "none" and not ref_step:
                # Pas d'origine : on reconstruit les champs d'identification depuis les profils
                base = self.ref.template("PURCHASE").fields
                for de in tpl.copy_from_ref:
                    if de in base:
                        values[de] = self._fill(base[de], ctx)
            for de, expr in tpl.fields.items():
                values[de] = self._fill(expr, ctx)
        # 2) champs carte automatiques
        is_chip = bool(card and term and card.card_type in ("emv", "contactless") and term.terminal_kind != "ecom")
        if card and term and mti[:2] in ("01", "02") and not is_chain:
            values.setdefault("14", expiry)
            if term.terminal_kind != "ecom" and card.card_type in ("emv", "contactless", "magstripe"):
                values.setdefault("35", track2)
            if is_chip:
                values.setdefault("23", "0" + card.psn.zfill(2))
        if amount12 and "4" in values and step.amount is not None or (magic and "4" in values):
            values["4"] = amount12
        # 3) surcharges explicites de l'étape
        for de, expr in step.fields.items():
            if not de.startswith("55."):
                values[de] = self._fill(expr, ctx)
        for de in step.remove:
            values.pop(de, None)

        # 4) PIN
        need_pin = bool(card and term and not is_chain and self.needs_pin(card, term, step, amount or 0, mti))
        if need_pin:
            pin_value = step.pin or card.pin
            values["52"] = pinlib.clear_block(pin_value, pan, "iso0")
            if card.card_type == "contactless":
                values["22"] = values.get("22", "071")[:2] + "1"

        fields = [MsgField(k, v) for k, v in sorted(values.items(), key=lambda kv: int(kv[0]))]

        # 5) EMV (DE55)
        icc_tags: list[tuple[str, str]] = []
        if is_chip and mti[:2] in ("01", "02") and not is_chain and "55" not in step.remove:
            e = card.emv
            icc_tags = [
                ("9F02", amount12 or "000000000000"), ("9F03", "000000000000"),
                ("9F1A", term.country.zfill(4)), ("95", e.tvr), ("5F2A", term.currency.zfill(4)),
                ("9A", "${__time(yyMMdd,)}"), ("9C", proc[:2]),
                ("9F37", "${__RandomString(8,0123456789ABCDEF,)}"), ("82", e.aip),
                ("9F36", self._next_atc(card)), ("9F10", e.iad), ("9F27", "80"),
                ("9F33", term.capabilities), ("9F34", "1F0302" if card.card_type == "contactless" and not need_pin
                                               else e.cvm_results),
                ("9F35", term.terminal_type), ("84", e.aid), ("9F09", e.app_version),
            ]
            overrides = {k.split(".", 1)[1].upper(): self._fill(v, ctx) for k, v in step.fields.items()
                         if k.startswith("55.")}
            icc_tags = [(t, overrides.pop(t, v)) for t, v in icc_tags] + list(overrides.items())
            for i, (t, v) in enumerate(icc_tags, 1):
                fields.append(MsgField(f"55.{i}", v, t))

        # 6) crypto
        modes = set(step.crypto if step.crypto is not None else (tpl.crypto if tpl else []))
        auto = "auto" in modes
        cc = CryptoConfig(mac_algorithm=ks.mac_algorithm if ks else "retail", mac_field=self.host.mac_field,
                          pan=pan if card else "", psn=card.psn if card else "")
        if ks and "none" not in modes:
            cc.pin = need_pin and bool(ks.zpk)
            cc.mac = ("mac" in modes or auto) and bool(ks.tak) and self.host.mac_field is not None
            cc.arqc = ("arqc" in modes or auto) and bool(icc_tags) and bool(ks.imk_ac)
            cc.pin_key_var = key_var("ZPK", ks.id)
            cc.mac_key_var = "BADKEY" if variables.get("mac_key") == "bad" else key_var("TAK", ks.id)
            cc.imk_var = "BADKEY" if variables.get("imk_ac") == "bad" else key_var("IMKAC", ks.id)
        elif need_pin:
            self.warnings.append(f"{scn.id}.{step.id} : PIN en clair (aucun jeu de clés)")

        # 7) attentes
        raw_expect = dict(tpl.expect) if tpl else {}
        raw_expect.update(step.expect)
        expect, expects_timeout = self._resolve_expect(raw_expect)
        name = f"{scn.id}.{step.id} {mti} {step.description or (tpl.description if tpl else '')}".strip()
        return BoundStep(step.id, name, mti, fields, cc, expect,
                         step.timeout_ms or self.suite.defaults.timeout_ms or self.host.timeout_ms,
                         step.delay_ms, tpl.id if tpl else "", step.ref, prefix,
                         step.description or (tpl.description if tpl else ""), expects_timeout)

    def network_step(self, template: str, label: str) -> BoundStep:
        scn = Scenario(id=label, title=label, steps=[Step(id="msg", template=template)])
        return self.bind_step(scn, scn.steps[0], None, None, None, {})

    def bind(self, scenarios: list[Scenario]) -> BoundSuite:
        bound = [self.bind_scenario(s) for s in scenarios]
        setup = [self.network_step("SIGNON", "SETUP")] if self.host.signon and "SIGNON" in self.ref.templates else []
        teardown = [self.network_step("SIGNOFF", "TEARDOWN")] if self.host.signon and \
            "SIGNOFF" in self.ref.templates else []
        return BoundSuite(self.suite.suite, self.suite.host, self.host, bound, setup, teardown,
                          sorted(self.keysets_used), self.warnings)


class _RefCtx:
    """Accès {ref.N} et {ref.resp.N} dans les templates."""

    def __init__(self, step: BoundStep | None, expr):
        self._step, self._expr = step, expr
        self.resp = _RespCtx(step)

    def __getattr__(self, name: str):
        if name.isdigit():
            v = self._expr(name)
            return v if v else None
        raise AttributeError(name)


class _RespCtx:
    def __init__(self, step: BoundStep | None):
        self._step = step

    def __getattr__(self, name: str):
        if self._step and name.isdigit():
            return "${" + var_name("R", self._step.var_prefix[2:], name) + "}"
        return None


def keys_properties(ref: Referential, keysets: list[str]) -> dict[str, str]:
    """Propriétés JMeter des clés de test (fichier séparé, jamais dans le JMX)."""
    props = {"BADKEY": BAD_KEY}
    for ks_id in keysets:
        ks = ref.keyset(ks_id)
        for attr, kind in (("zpk", "ZPK"), ("tak", "TAK"), ("imk_ac", "IMKAC")):
            v = ref.key(ks_id, attr)
            if v:
                props[key_var(kind, ks_id)] = v
    return props
