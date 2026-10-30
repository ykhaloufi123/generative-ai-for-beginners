"""Builder JMX déterministe pour le plugin jmeter-iso8583 (nz.co.breakpoint.jmeter.iso8583).

Le même BoundSuite produit toujours le même XML (ordre stable), donc les plans sont
comparables dans git. Les clés ne sont jamais écrites dans le JMX : seulement des propriétés
${__P(...)} alimentées au lancement par le fichier keys.properties (ou Vault).
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from lxml import etree

from ..crypto import pin as pinlib
from ..dsl.models import Suite
from ..referential.repository import Referential
from .binder import BoundStep, BoundSuite, keys_properties, var_name
from ..crypto import mac as maclib

PKG = "nz.co.breakpoint.jmeter.iso8583"
JMETER_VERSION = "5.6.3"


# --------------------------------------------------------------------------- primitives XML
def _sp(parent, name: str, value) -> etree._Element:
    e = etree.SubElement(parent, "stringProp", name=name)
    e.text = "" if value is None else str(value)
    return e


def _bp(parent, name: str, value: bool) -> None:
    etree.SubElement(parent, "boolProp", name=name).text = "true" if value else "false"


def _ip(parent, name: str, value: int) -> None:
    etree.SubElement(parent, "intProp", name=name).text = str(value)


def _lp(parent, name: str, value: int) -> None:
    etree.SubElement(parent, "longProp", name=name).text = str(value)


def _node(tree, tag: str, testname: str, guiclass: str, testclass: str | None = None,
          enabled: bool = True) -> etree._Element:
    el = etree.SubElement(tree, tag, guiclass=guiclass, testclass=testclass or tag, testname=testname,
                          enabled="true" if enabled else "false")
    return el


def _children(tree) -> etree._Element:
    return etree.SubElement(tree, "hashTree")


def _loop(parent, loops: int = 1) -> None:
    lc = etree.SubElement(parent, "elementProp", name="ThreadGroup.main_controller", elementType="LoopController",
                          guiclass="LoopControlPanel", testclass="LoopController", testname="Loop Controller")
    _sp(lc, "LoopController.loops", loops)
    _bp(lc, "LoopController.continue_forever", False)


def _thread_group(tree, kind: str, name: str, threads: int = 1, ramp: int = 1, on_error: str = "continue",
                  loops: int = 1, duration: int | None = None) -> etree._Element:
    gui = {"ThreadGroup": "ThreadGroupGui", "SetupThreadGroup": "SetupThreadGroupGui",
           "PostThreadGroup": "PostThreadGroupGui"}[kind]
    tg = _node(tree, kind, name, gui)
    _ip(tg, "ThreadGroup.num_threads", threads)
    _ip(tg, "ThreadGroup.ramp_time", ramp)
    _bp(tg, "ThreadGroup.same_user_on_next_iteration", True)
    _sp(tg, "ThreadGroup.on_sample_error", on_error)
    _loop(tg, -1 if duration else loops)
    if duration:
        _bp(tg, "ThreadGroup.scheduler", True)
        _sp(tg, "ThreadGroup.duration", duration)
        _sp(tg, "ThreadGroup.delay", 0)
    return tg


def _comment(el, text: str) -> None:
    if text:
        _sp(el, "TestPlan.comments", text)


def _jsr223(tree, kind: str, name: str, script: str, comment: str = "") -> None:
    el = _node(tree, kind, name, "TestBeanGUI")
    _sp(el, "cacheKey", "true")
    _sp(el, "filename", "")
    _sp(el, "parameters", "")
    _sp(el, "script", script)
    _sp(el, "scriptLanguage", "groovy")
    _comment(el, comment)
    _children(tree)


def _groovy_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


# --------------------------------------------------------------------------- éléments du plugin
def _iso_config(tree, suite: BoundSuite) -> None:
    h = suite.host
    el = _node(tree, f"{PKG}.ISO8583Config", f"ISO8583 Config - {h.id}", "TestBeanGUI", f"{PKG}.ISO8583Config")
    _sp(el, "host", "${__P(ITF_HOST," + h.host + ")}")
    _sp(el, "port", "${__P(ITF_PORT," + str(h.port) + ")}")
    _sp(el, "packager", "${__P(ITF_PACKAGER,packager.xml)}")
    _sp(el, "classname", h.channel.kind)
    _sp(el, "header", h.channel.header)
    _sp(el, "keystore", "")
    _sp(el, "storePassword", "")
    _sp(el, "keyPassword", "")
    cc = etree.SubElement(el, "collectionProp", name="channelConfig")
    if h.channel.kind == "ASCIIChannel" and h.channel.length_digits != 4:
        item = etree.SubElement(cc, "elementProp", name="length-digits", elementType=f"{PKG}.ChannelConfigItem",
                                testname="length-digits")
        _sp(item, "value", h.channel.length_digits)
    _bp(el, "reuseConnection", True)
    _sp(el, "maxConnections", "")
    mk = etree.SubElement(el, "collectionProp", name="muxKeyConfig")
    if h.mux_key:
        item = etree.SubElement(mk, "elementProp", name="", elementType=f"{PKG}.MuxKeyConfigItem", testname="")
        _sp(item, "value", h.mux_key)
    _sp(el, "mtiMapping", "")
    _sp(el, "configKey", "")
    _sp(el, "connectionSelection", "connectionSelection.LAST")
    _sp(el, "requestListener", "")
    _comment(el, f"Hôte {h.id} — spec {h.spec_version}. Surcharger avec -JITF_HOST / -JITF_PORT / -JITF_PACKAGER.")
    _children(tree)


def _sampler(tree, step: BoundStep, comment: str = "") -> etree._Element:
    el = _node(tree, f"{PKG}.ISO8583Sampler", step.name, "TestBeanGUI", f"{PKG}.ISO8583Sampler")
    coll = etree.SubElement(el, "collectionProp", name="fields")

    def add(key, value, tag="", cmt=""):
        f = etree.SubElement(coll, "elementProp", name=key, elementType=f"{PKG}.MessageField", testname=key)
        _sp(f, "content", value)
        _sp(f, "tag", tag)
        if cmt:
            _sp(f, "TestPlan.comments", cmt)

    add("0", step.mti, cmt="MTI")
    for f in step.fields:
        add(f.key, f.value, f.tag, f.comment)
    _sp(el, "timeout", step.timeout_ms)
    codes = step.expect.get("39")
    if step.expects_timeout or not codes or any(c in ("present", "absent") or c.startswith("re:") for c in codes):
        _sp(el, "responseCodeField", "")
        _sp(el, "successResponseCode", "")
    else:
        _sp(el, "responseCodeField", "39")
        _sp(el, "successResponseCode", ",".join(codes))
    _sp(el, "header", "")
    _sp(el, "trailer", "")
    _sp(el, "configKey", "")
    _comment(el, comment or step.description)
    return el


def _crypto(tree, step: BoundStep) -> None:
    c = step.crypto
    if not (c.pin or c.mac or c.arqc):
        return
    el = _node(tree, f"{PKG}.ISO8583Crypto", "ISO8583 Crypto PreProcessor", "TestBeanGUI", f"{PKG}.ISO8583Crypto")
    _sp(el, "macAlgorithm", maclib.PLUGIN_ALGORITHMS.get(c.mac_algorithm, "") if c.mac else "")
    _sp(el, "macKey", "${" + c.mac_key_var + "}" if c.mac else "")
    _sp(el, "pinField", "52" if c.pin else "")
    _sp(el, "pinKey", "${" + c.pin_key_var + "}" if c.pin else "")
    _sp(el, "arqcField", "")
    _sp(el, "imkac", "${" + c.imk_var + "}" if c.arqc else "")
    _sp(el, "pan", c.pan if c.arqc else "")
    _sp(el, "psn", c.psn.zfill(2)[-2:] if c.arqc else "")
    _sp(el, "txnData", "")
    _sp(el, "iccField", "55" if c.arqc else "")
    _sp(el, "ksnField", "")
    _sp(el, "macField", str(c.mac_field) if c.mac and c.mac_field else "")
    _children(tree)


CAPTURE_SCRIPT = """// Mémorise les champs de la réponse pour les étapes chaînées : R_<scénario>_<étape>_<DE>
def r = sampler.getResponse()
def p = %(prefix)s
if (r != null) {
    for (int i = 0; i <= r.getMaxField(); i++) {
        if (!r.hasField(i)) continue
        def c = r.getComponent(i)
        if (c instanceof org.jpos.iso.ISOMsg) {
            c.getChildren().values().each { s ->
                try {
                    def tag = s.respondsTo('getTag') ? s.getTag() : String.valueOf(s.getKey())
                    def v = (s.getValue() instanceof byte[]) ? org.jpos.iso.ISOUtil.hexString(s.getValue()) : String.valueOf(s.getValue())
                    vars.put(p + i + '_' + tag, v)
                } catch (Exception e) { log.debug('Sous-champ ignoré : ' + e) }
            }
        } else {
            vars.put(p + i, r.getString(i))
        }
    }
}
%(timeout)s"""

TIMEOUT_SCRIPT = """// Absence de réponse ATTENDUE (cas de timeout) : on inverse le verdict
if (r == null) {
    prev.setSuccessful(true)
    prev.setResponseCode('TIMEOUT')
    prev.setResponseMessage('Absence de réponse conforme à l\\'attendu')
} else {
    prev.setSuccessful(false)
    prev.setResponseMessage('Réponse reçue alors qu\\'aucune réponse n\\'était attendue')
}"""

ASSERT_SCRIPT = """// Attendus ISO-TestForge : %(desc)s
def r = sampler.getResponse()
def expected = %(expected)s
def errors = []
def fieldValue = { String key ->
    if (r == null) return null
    if (key.contains('.')) {
        def parts = key.split('\\\\.')
        int n = parts[0] as int
        if (!r.hasField(n)) return null
        def c = r.getComponent(n)
        def found = null
        if (c instanceof org.jpos.iso.ISOMsg) {
            c.getChildren().values().each { s ->
                def tag = s.respondsTo('getTag') ? s.getTag() : String.valueOf(s.getKey())
                if (tag?.equalsIgnoreCase(parts[1])) {
                    found = (s.getValue() instanceof byte[]) ? org.jpos.iso.ISOUtil.hexString(s.getValue()) : String.valueOf(s.getValue())
                }
            }
        }
        return found
    }
    int n = key as int
    return r.hasField(n) ? r.getString(n) : null
}
if (r == null) {
    errors << 'Aucune réponse reçue'
} else {
    expected.each { key, vals ->
        def v = fieldValue(key)
        def ok = vals.any { e ->
            if (e == 'present') return v != null && v != ''
            if (e == 'absent') return v == null
            if (e.startsWith('re:')) return v != null && (v ==~ e.substring(3))
            return v == e
        }
        if (!ok) errors << ("DE" + key + " = " + v + " (attendu " + vals.join(' | ') + ")")
    }
}
if (errors) {
    AssertionResult.setFailure(true)
    AssertionResult.setFailureMessage(errors.join(' ; '))
}"""


def _capture(tree, step: BoundStep) -> None:
    prefix = var_name("R", step.var_prefix[2:]) + "_"
    script = CAPTURE_SCRIPT % {"prefix": _groovy_str(prefix), "timeout": TIMEOUT_SCRIPT if step.expects_timeout else ""}
    _jsr223(tree, "JSR223PostProcessor", "Capture réponse", script.strip())


def _assertion(tree, step: BoundStep) -> None:
    extra = {k: v for k, v in step.expect.items()
             if k != "39" or any(c in ("present", "absent") or c.startswith("re:") for c in v)}
    if not extra or step.expects_timeout:
        return
    expected = "[" + ", ".join(f"{_groovy_str(k)}: [{', '.join(_groovy_str(x) for x in v)}]"
                               for k, v in extra.items()) + "]"
    desc = ", ".join(f"DE{k}={'|'.join(v)}" for k, v in extra.items())
    script = ASSERT_SCRIPT % {"expected": expected, "desc": desc}
    _jsr223(tree, "JSR223Assertion", f"Attendus : {desc}", script)


def _add_step(tree, step: BoundStep, comment: str = "") -> None:
    if step.delay_ms:
        t = _node(tree, "ConstantTimer", f"Attente {step.delay_ms} ms", "ConstantTimerGui")
        _sp(t, "ConstantTimer.delay", step.delay_ms)
        _children(tree)
    _sampler(tree, step, comment)
    sub = _children(tree)
    _crypto(sub, step)
    _capture(sub, step)
    _assertion(sub, step)


# --------------------------------------------------------------------------- plan complet
def _test_plan(root_tree, name: str, comment: str, variables: dict[str, str]) -> etree._Element:
    tp = _node(root_tree, "TestPlan", name, "TestPlanGui")
    _comment(tp, comment)
    _bp(tp, "TestPlan.functional_mode", False)
    _bp(tp, "TestPlan.tearDown_on_shutdown", True)
    _bp(tp, "TestPlan.serialize_threadgroups", True)   # groupes exécutés l'un après l'autre
    udv = etree.SubElement(tp, "elementProp", name="TestPlan.user_defined_variables", elementType="Arguments",
                           guiclass="ArgumentsPanel", testclass="Arguments", testname="User Defined Variables")
    coll = etree.SubElement(udv, "collectionProp", name="Arguments.arguments")
    for k, v in variables.items():
        a = etree.SubElement(coll, "elementProp", name=k, elementType="Argument")
        _sp(a, "Argument.name", k)
        _sp(a, "Argument.value", v)
        _sp(a, "Argument.metadata", "=")
    return tp


def build_functional(bound: BoundSuite) -> etree._ElementTree:
    root = etree.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter=JMETER_VERSION)
    top = _children(root)
    key_vars = {"BADKEY": "${__P(BADKEY,)}"}
    for ks in bound.keysets:
        for kind in ("ZPK", "TAK", "IMKAC"):
            n = var_name(kind, ks)
            key_vars[n] = "${__P(" + n + ",)}"
    reqs = sorted({r for s in bound.scenarios for r in s.requirements})
    _test_plan(top, bound.suite,
               f"Généré par ISO-TestForge. Hôte {bound.host_id}. {len(bound.scenarios)} scénarios. "
               f"Exigences couvertes : {', '.join(reqs) or '-'}. Clés : fournir keys.properties (-q).", key_vars)
    tree = _children(top)
    _iso_config(tree, bound)

    if bound.setup_steps:
        _thread_group(tree, "SetupThreadGroup", "setUp - gestion réseau", on_error="stoptest")
        sub = _children(tree)
        for s in bound.setup_steps:
            _add_step(sub, s)

    groups: dict[str, list] = {}
    for s in bound.scenarios:
        groups.setdefault(s.group, []).append(s)
    for gname, scns in groups.items():
        _thread_group(tree, "ThreadGroup", f"Groupe {gname}", on_error="continue")
        gtree = _children(tree)
        for scn in scns:
            label = f"{scn.id} [{', '.join(scn.requirements)}] {scn.title}" if scn.requirements else \
                f"{scn.id} {scn.title}"
            tc = _node(gtree, "TransactionController", label, "TransactionControllerGui")
            _bp(tc, "TransactionController.parent", False)
            _bp(tc, "TransactionController.includeTimers", False)
            _comment(tc, json.dumps({"requirements": scn.requirements, "tags": scn.tags, "priority": scn.priority,
                                     "card": scn.card_id, "terminal": scn.terminal_id, "origin": scn.origin,
                                     "mutation": scn.mutation}, ensure_ascii=False))
            ctree = _children(gtree)
            for step in scn.steps:
                _add_step(ctree, step)

    if bound.teardown_steps:
        _thread_group(tree, "PostThreadGroup", "tearDown - gestion réseau")
        sub = _children(tree)
        for s in bound.teardown_steps:
            _add_step(sub, s)
    return etree.ElementTree(root)


def build_load(suite: Suite, ref: Referential, bound: BoundSuite, out_dir: Path) -> etree._ElementTree:
    """Plan de charge : même référentiel, données injectées par CSV, débit piloté (TPS)."""
    lp = suite.load
    assert lp is not None
    cards = [ref.card(c) for c in (lp.cards or [c.id for c in ref.cards.values() if c.status == "active"])]
    terms = [ref.terminal(t) for t in (lp.terminals or [t.id for t in ref.terminals.values()
                                                       if t.terminal_kind == "pos"])]
    rows = []
    for i, c in enumerate(cards):
        t = terms[i % len(terms)]
        rows.append({"PAN": c.pan, "EXP": c.expiry, "PSN": c.psn.zfill(2), "TRACK2": c.track2(),
                     "PINBLOCK": pinlib.clear_block(c.pin, c.pan) if c.cvm == "online_pin" else "",
                     "ATC": c.emv.atc, "IAD": c.emv.iad, "AIP": c.emv.aip, "TID": t.tid, "MID": t.mid,
                     "MCC": t.mcc, "CUR": t.currency, "ENTRY": "051" if c.card_type == "emv" else "901",
                     "AMOUNT": str(int(lp.amounts[i % len(lp.amounts)] * 100)).zfill(12)})
    with open(out_dir / "load_data.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    root = etree.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter=JMETER_VERSION)
    top = _children(root)
    ks = bound.keysets[0] if bound.keysets else None
    kv = {"BADKEY": "${__P(BADKEY,)}"}
    if ks:
        for kind in ("ZPK", "TAK", "IMKAC"):
            kv[var_name(kind, ks)] = "${__P(" + var_name(kind, ks) + ",)}"
    _test_plan(top, f"{suite.suite} - charge", f"Profil de charge : {lp.tps} TPS, {lp.threads} threads, "
               f"{lp.duration_s} s.", kv)
    tree = _children(top)
    _iso_config(tree, bound)
    csvel = _node(tree, "CSVDataSet", "Données de charge", "TestBeanGUI")
    for k, v in (("filename", "load_data.csv"), ("fileEncoding", "UTF-8"), ("variableNames", ""),
                 ("delimiter", ","), ("shareMode", "shareMode.all")):
        _sp(csvel, k, v)
    _bp(csvel, "ignoreFirstLine", False)
    _bp(csvel, "quotedData", False)
    _bp(csvel, "recycle", True)
    _bp(csvel, "stopThread", False)
    _children(tree)
    _thread_group(tree, "ThreadGroup", "Charge", threads=lp.threads, ramp=lp.ramp_up_s, duration=lp.duration_s)
    tg = _children(tree)
    timer = _node(tg, "ConstantThroughputTimer", "Débit cible", "TestBeanGUI")
    dp = etree.SubElement(timer, "doubleProp")
    etree.SubElement(dp, "name").text = "throughput"
    etree.SubElement(dp, "value").text = str(float(lp.tps * 60))
    etree.SubElement(dp, "savedValue").text = "0.0"
    _ip(timer, "calcMode", 4)  # débit partagé entre tous les threads actifs
    _children(tg)

    from .binder import BoundStep, CryptoConfig, MsgField
    fields = [MsgField("2", "${PAN}"), MsgField("3", "000000"), MsgField("4", "${AMOUNT}"),
              MsgField("7", "${__time(MMddHHmmss,)}"), MsgField("11", "${__RandomString(6,0123456789,)}"),
              MsgField("12", "${__time(HHmmss,)}"), MsgField("13", "${__time(MMdd,)}"), MsgField("14", "${EXP}"),
              MsgField("18", "${MCC}"), MsgField("22", "${ENTRY}"), MsgField("25", "00"),
              MsgField("35", "${TRACK2}"), MsgField("37", "${__RandomString(12,0123456789,)}"),
              MsgField("41", "${TID}"), MsgField("42", "${MID}"), MsgField("49", "${CUR}"),
              MsgField("52", "${PINBLOCK}")]
    cc = CryptoConfig(pin=bool(ks), mac=bool(ks) and bound.host.mac_field is not None, arqc=False,
                      pin_key_var=var_name("ZPK", ks) if ks else "", mac_key_var=var_name("TAK", ks) if ks else "",
                      mac_field=bound.host.mac_field)
    step = BoundStep("load", "Charge - achat ${TID}", "0200", fields, cc, {"39": ["00"]}, bound.host.timeout_ms,
                     var_prefix="V_LOAD")
    _sampler(tg, step, "Les cartes sans PIN laissent DE52 vide (le préprocesseur ignore alors le chiffrement).")
    sub = _children(tg)
    _crypto(sub, step)
    return etree.ElementTree(root)


def write_outputs(suite: Suite, ref: Referential, bound: BoundSuite, out_dir: str | Path,
                  scenarios_meta: list[dict] | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    tree = build_functional(bound)
    jmx = out / f"{suite.suite}.jmx"
    tree.write(str(jmx), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    files["jmx"] = str(jmx)
    if suite.load:
        lt = build_load(suite, ref, bound, out)
        lj = out / f"{suite.suite}-load.jmx"
        lt.write(str(lj), xml_declaration=True, encoding="UTF-8", pretty_print=True)
        files["load_jmx"] = str(lj)
        files["load_data"] = str(out / "load_data.csv")
    # packager à côté du plan, clés dans un fichier séparé
    shutil.copy(ref.packager_path(suite.host), out / "packager.xml")
    files["packager"] = str(out / "packager.xml")
    props = keys_properties(ref, bound.keysets)
    kp = out / "keys.properties"
    kp.write_text("# Clés de TEST — ne pas versionner. Utilisation : jmeter -n -t plan.jmx -q keys.properties\n" +
                  "\n".join(f"{k}={v}" for k, v in props.items()) + "\n")
    files["keys"] = str(kp)
    manifest = {
        "suite": suite.suite, "host": suite.host, "referential_version": ref.version(),
        "scenarios": [{"id": s.id, "title": s.title, "requirements": s.requirements, "tags": s.tags,
                       "group": s.group, "origin": s.origin, "mutation": s.mutation, "card": s.card_id,
                       "terminal": s.terminal_id,
                       "steps": [{"id": st.id, "sampler": st.name, "mti": st.mti, "template": st.template, "expect": st.expect,
                                  "expects_timeout": st.expects_timeout} for st in s.steps]}
                      for s in bound.scenarios],
        "warnings": bound.warnings,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    files["manifest"] = str(out / "manifest.json")
    return files
