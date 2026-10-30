"""Runner natif : exécute une suite liée directement en Python (sans JMeter).

Il interprète les mêmes expressions JMeter que le plan généré (${__time}, ${__RandomString},
${__P}, ${VAR}) et applique la même crypto que l'ISO8583 Crypto PreProcessor. Il sert au
débogage rapide, à la validation en CI sans JMeter et à l'auto-test contre le simulateur.
Il écrit un fichier JTL CSV au format JMeter, exploité par l'analyse de résultats.
"""
from __future__ import annotations

import csv
import random
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..crypto import emv, mac as maclib, pin as pinlib
from ..engine.binder import BoundStep, BoundSuite, var_name
from ..iso.channel import ChannelSpec
from ..iso.message import IsoMessage
from ..iso.packager import GenericPackager

_EXPR = re.compile(r"\$\{([^{}]+)\}")
_JAVA_FMT = [("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"), ("DDD", "%j"), ("HH", "%H"),
             ("mm", "%M"), ("ss", "%S")]

JTL_FIELDS = ["timeStamp", "elapsed", "label", "responseCode", "responseMessage", "threadName", "dataType",
              "success", "failureMessage", "bytes", "sentBytes", "grpThreads", "allThreads", "URL", "Latency",
              "IdleTime", "Connect"]


def java_time(fmt: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    out, i = "", 0
    while i < len(fmt):
        for j, py in _JAVA_FMT:
            if fmt.startswith(j, i):
                out += now.strftime(py)
                i += len(j)
                break
        else:
            if fmt[i] == "y":
                out += now.strftime("%y")[-1]
            else:
                out += fmt[i]
            i += 1
    return out


class JMeterEval:
    def __init__(self, props: dict[str, str]):
        self.props = props
        self.vars: dict[str, str] = {}

    def _func(self, name: str, args: list[str]) -> str:
        if name == "__RandomString":
            n, chars = int(args[0]), args[1]
            v = "".join(random.choice(chars) for _ in range(n))
        elif name == "__time":
            v = java_time(args[0]) if args and args[0] else str(int(time.time() * 1000))
        elif name == "__P":
            return self.props.get(args[0], args[1] if len(args) > 1 else "")
        else:
            raise ValueError(f"Fonction JMeter non supportée par le runner natif : {name}")
        if len(args) > 2 and name == "__RandomString" and args[2]:
            self.vars[args[2]] = v
        if name == "__time" and len(args) > 1 and args[1]:
            self.vars[args[1]] = v
        return v

    def eval(self, text: str) -> str:
        def repl(m):
            inner = m.group(1)
            if inner.startswith("__"):
                name, _, rest = inner.partition("(")
                args = rest[:-1].split(",") if rest else []
                return self._func(name, args)
            return self.vars.get(inner, self.props.get(inner, m.group(0)))
        prev = None
        while prev != text and "${" in text:
            prev, text = text, _EXPR.sub(repl, text)
        return text


@dataclass
class StepResult:
    scenario: str
    step: str
    label: str
    success: bool
    response_code: str
    message: str
    elapsed_ms: int
    request: dict = field(default_factory=dict)
    response: dict | None = None
    failures: list[str] = field(default_factory=list)
    timestamp: int = 0


def check_expect(step: BoundStep, resp: IsoMessage | None) -> list[str]:
    errors = []
    if step.expects_timeout:
        return [] if resp is None else ["Réponse reçue alors qu'aucune réponse n'était attendue"]
    if resp is None:
        return ["Aucune réponse reçue (timeout)"]
    for key, vals in step.expect.items():
        v = resp.get(key)
        ok = False
        for e in vals:
            if e == "present":
                ok = ok or bool(v)
            elif e == "absent":
                ok = ok or v is None
            elif e.startswith("re:"):
                ok = ok or (v is not None and re.fullmatch(e[3:], v) is not None)
            else:
                ok = ok or v == e
        if not ok:
            errors.append(f"DE{key} = {v} (attendu {' | '.join(vals)})")
    return errors


class NativeRunner:
    def __init__(self, bound: BoundSuite, packager: GenericPackager, props: dict[str, str],
                 host: str | None = None, port: int | None = None):
        self.bound = bound
        self.pk = packager
        self.props = props
        self.host = host or bound.host.host
        self.port = port or bound.host.port
        ch = bound.host.channel
        self.channel = ChannelSpec.from_config(ch.kind, ch.header, ch.header_hex, ch.length_digits)
        self.ev = JMeterEval(props)
        self.sock: socket.socket | None = None

    def _connect(self) -> None:
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=10)

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def build_message(self, step: BoundStep) -> IsoMessage:
        msg = IsoMessage(step.mti)
        for f in step.fields:
            msg.set(f.key, self.ev.eval(f.value), f.tag or None)
        c = step.crypto
        if c.pin and msg.get(52):
            msg.fields[52] = pinlib.encrypt(msg.fields[52], self.ev.eval("${" + c.pin_key_var + "}"))
        if c.arqc and 55 in msg.tlv:
            imk = self.ev.eval("${" + c.imk_var + "}")
            icc = dict(msg.tlv[55])
            msg.tlv[55].append(("9F26", emv.compute_arqc(imk, c.pan, c.psn, icc)))
        if c.mac and c.mac_field:
            key = self.ev.eval("${" + c.mac_key_var + "}")
            msg.fields[c.mac_field] = "0" * 16
            raw = self.pk.pack(msg)
            plen = len(self.pk.pack_field(self.pk.field(c.mac_field), msg))
            msg.fields[c.mac_field] = maclib.generate(key, raw[:-plen], c.mac_algorithm)
        return msg

    def send(self, step: BoundStep) -> tuple[IsoMessage, IsoMessage | None, int]:
        self._connect()
        msg = self.build_message(step)
        raw = self.pk.pack(msg)
        t0 = time.time()
        self.sock.settimeout(step.timeout_ms / 1000)
        self.sock.sendall(self.channel.frame(raw))
        try:
            resp = self.pk.unpack(self.channel.read_sync(self.sock))
        except socket.timeout:
            resp = None
            self.close()   # une réponse tardive ne doit pas polluer l'échange suivant
        return msg, resp, int((time.time() - t0) * 1000)

    def capture(self, scenario_id: str, step: BoundStep, resp: IsoMessage | None) -> None:
        if resp is None:
            return
        prefix = var_name("R", scenario_id, step.id) + "_"
        for n, v in resp.fields.items():
            self.ev.vars[f"{prefix}{n}"] = v
        for n, lst in resp.tlv.items():
            for t, v in lst:
                self.ev.vars[f"{prefix}{n}_{t}"] = v

    def run_step(self, scenario_id: str, step: BoundStep) -> StepResult:
        if step.delay_ms:
            time.sleep(step.delay_ms / 1000)
        ts = int(time.time() * 1000)
        try:
            req, resp, ms = self.send(step)
        except Exception as e:
            self.close()
            return StepResult(scenario_id, step.id, step.name, False, "ERROR", str(e), 0, failures=[str(e)],
                              timestamp=ts)
        self.capture(scenario_id, step, resp)
        failures = check_expect(step, resp)
        code = resp.get(39) if resp else "TIMEOUT"
        from ..simulator.host import _masked
        return StepResult(scenario_id, step.id, step.name, not failures, code or "",
                          "; ".join(failures) or "OK", ms, _masked(req), _masked(resp) if resp else None,
                          failures, ts)

    def run(self, scenario_ids: set[str] | None = None) -> list[StepResult]:
        results: list[StepResult] = []
        try:
            for st in self.bound.setup_steps:
                results.append(self.run_step("SETUP", st))
            for scn in self.bound.scenarios:
                if scenario_ids and scn.id not in scenario_ids:
                    continue
                for st in scn.steps:
                    results.append(self.run_step(scn.id, st))
            for st in self.bound.teardown_steps:
                results.append(self.run_step("TEARDOWN", st))
        finally:
            self.close()
        return results


def write_jtl(results: list[StepResult], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=JTL_FIELDS)
        w.writeheader()
        for r in results:
            w.writerow({"timeStamp": r.timestamp, "elapsed": r.elapsed_ms, "label": r.label,
                        "responseCode": r.response_code, "responseMessage": r.message, "threadName": "native 1-1",
                        "dataType": "text", "success": "true" if r.success else "false",
                        "failureMessage": "; ".join(r.failures), "bytes": 0, "sentBytes": 0, "grpThreads": 1,
                        "allThreads": 1, "URL": "", "Latency": r.elapsed_ms, "IdleTime": 0, "Connect": 0})


def load_properties(path: str | Path) -> dict[str, str]:
    props = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props
