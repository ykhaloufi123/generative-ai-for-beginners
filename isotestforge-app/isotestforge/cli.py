"""Ligne de commande ISO-TestForge : itf <commande>. Codes retour non nuls en cas d'échec (CI)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

BASE = Path(os.environ.get("ITF_HOME", Path(__file__).resolve().parents[1]))


def _ref():
    from .referential.repository import Referential
    return Referential(BASE / "data/referential", BASE)


def _bind(suite_path: str):
    from .dsl import loader
    from .engine.binder import Binder
    from .engine.expand import expand_suite
    ref = _ref()
    suite = loader.load(suite_path)
    scns, rep = expand_suite(suite, ref)
    bound = Binder(ref, suite).bind(scns)
    return ref, suite, bound, rep


def cmd_generate(a) -> int:
    from .engine.jmx_builder import write_outputs
    from .engine.validator import validate_bound, validate_jmx
    ref, suite, bound, rep = _bind(a.suite)
    val = validate_bound(bound, ref)
    print(f"Expansion : {rep.input_scenarios} -> {rep.output_scenarios} scénarios "
          f"(pairwise : {rep.pairwise_saved} évités){'; ignorés : ' + ', '.join(rep.skipped) if rep.skipped else ''}")
    for i in val.issues:
        print(f"  [{i.level}] {i.where} : {i.message}")
    if not val.ok and not a.force:
        print("Validation en échec : JMX non généré (--force pour passer outre)")
        return 2
    out = a.out or str(BASE / "out" / suite.suite)
    files = write_outputs(suite, ref, bound, out)
    jv = validate_jmx(files["jmx"])
    print(f"JMX {'valide' if jv.ok else 'INVALIDE'} : {files['jmx']}")
    for k, v in files.items():
        if k != "jmx":
            print(f"  {k:10} {v}")
    print(f"Lancement : cd {out} && jmeter -n -t {Path(files['jmx']).name} -q keys.properties -l results.jtl")
    return 0 if jv.ok else 2


def cmd_validate(a) -> int:
    from .engine.validator import validate_bound, validate_jmx
    if a.path.endswith(".jmx"):
        rep = validate_jmx(a.path)
    else:
        ref, _, bound, _ = _bind(a.path)
        rep = validate_bound(bound, ref)
    print(json.dumps(rep.to_dict(), indent=2, ensure_ascii=False))
    return 0 if rep.ok else 2


def cmd_run(a) -> int:
    from .orchestrator.pipeline import Pipeline
    p = Pipeline(BASE, use_llm=not a.no_llm)
    wid = p.suite_from_file(a.suite, auto=False)
    wf = p.approve_dsl(wid)
    if wf["status"] != "compiled":
        print("Compilation impossible :", wf["data"].get("error"), json.dumps(wf["data"].get("validation"), indent=1))
        return 2
    wf = p.execute(wid, a.mode, a.host, a.port)
    return _print_result(wf)


def _print_result(wf) -> int:
    d = wf["data"]
    if wf["status"] == "error":
        print("Erreur :", d.get("error"))
        return 3
    an = d["analysis"]
    for s in an["scenarios"]:
        mark = {"passed": "OK ", "failed": "KO ", "not_run": "-- "}[s["status"]]
        print(f"{mark} {s['id']:28} {s['title'][:70]}")
        for st in s["steps"]:
            if not st["success"]:
                print(f"       {st['step']} : {st['message']}")
                if st.get("diagnostic"):
                    print(f"       -> {st['diagnostic']['cause']} | {st['diagnostic']['action']}")
    t = an["totals"]
    print(f"\n{t['passed']}/{t['passed'] + t['failed']} scénarios OK ({t['pass_rate']} %), couverture "
          f"{d['coverage']['score']}/100\nRapport : {d['report']}\nExcel   : {d['report_xlsx']}")
    return 0 if t["failed"] == 0 else 1


def cmd_pipeline(a) -> int:
    from .orchestrator.pipeline import Pipeline
    from .tools.requirements_import import from_file, from_jira
    p = Pipeline(BASE, use_llm=not a.no_llm)
    reqs = from_jira(a.source[5:]) if a.source.startswith("jira:") else from_file(a.source)
    print(f"{len(reqs)} besoin(s) : {', '.join(r['id'] for r in reqs)}")
    wf = p.run_all(a.name, a.host, reqs, a.execute if a.execute != "none" else None, a.exec_host, a.exec_port)
    d = wf["data"]
    for q in d.get("questions", []):
        print("  ?", q)
    print("Coût :", p.cost_summary(wf["id"]))
    if wf["status"] == "dsl_review":
        print("Validation en échec :", json.dumps(d.get("validation"), indent=1, ensure_ascii=False))
        return 2
    if wf["status"] == "compiled":
        print("JMX :", d["files"]["jmx"])
        return 0
    return _print_result(wf)


def cmd_serve(a) -> int:
    import uvicorn
    if a.no_llm:
        os.environ["ITF_NO_LLM"] = "1"
    uvicorn.run("isotestforge.api.app:app", host=a.host, port=a.port, log_level="info")
    return 0


def cmd_simulator(a) -> int:
    from .simulator.host import HostSimulator, SimConfig
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = SimConfig.load(a.config or BASE / "config/simulator.yaml")
    if a.port:
        cfg.port = a.port
    if cfg.trace_file and not Path(cfg.trace_file).is_absolute():
        cfg.trace_file = str(BASE / cfg.trace_file)
    sim = HostSimulator(_ref(), cfg)
    try:
        asyncio.run(sim.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_decode(a) -> int:
    from .tools import decoder
    text = Path(a.message[1:]).read_text() if a.message.startswith("@") else a.message
    ref = _ref()
    d = decoder.decode(text, ref.packager(a.host or next(iter(ref.hosts))), mask=not a.clear)
    print(decoder.to_text(d))
    return 0


def cmd_traces(a) -> int:
    from .dsl import loader
    from .tools.trace_import import import_traces
    ref = _ref()
    suite, stats = import_traces(a.file, ref, a.host, a.name, ref.packager(a.host), a.requirement)
    out = a.out or f"{a.name}.yaml"
    loader.save(suite, out)
    print(json.dumps(stats, ensure_ascii=False), "->", out)
    return 0


def cmd_spec_diff(a) -> int:
    from .iso.packager import GenericPackager
    from .tools.spec_diff import diff_packagers
    ch = diff_packagers(GenericPackager.load(a.old), GenericPackager.load(a.new))
    for c in ch:
        print(f"DE{c['de']:>3} {c['change']:9} {c['detail']}")
    print(f"{len(ch)} différence(s)")
    return 0


def cmd_cards(a) -> int:
    from .engine import cardgen
    ref = _ref()
    cards = cardgen.generate(a.bin, a.count, a.type, a.expiry, pin=a.pin, keyset=a.keyset, cvn=a.cvn,
                             start_seq=a.start)
    for c in cards:
        ks = c.keyset
        info = cardgen.card_report(c, ref.key(ks, "cvk") if ks else None, ref.key(ks, "pvk") if ks else None)
        print(json.dumps(info, ensure_ascii=False))
        if a.save:
            ref.add_card(c)
    if a.save:
        print(f"{len(cards)} carte(s) ajoutée(s) au référentiel")
    return 0


def cmd_keys(a) -> int:
    from .crypto import keys
    if a.action == "kcv":
        print(keys.kcv(a.value))
    elif a.action == "generate":
        k = keys.generate(int(a.value or 128))
        print(k, "KCV", keys.kcv(k))
    elif a.action == "list":
        ref = _ref()
        from .referential.repository import resolve_secret
        for ks in ref.keysets.values():
            print(ks.id, ks.kcvs(resolve_secret))
    return 0


def cmd_negatives(a) -> int:
    from .dsl import loader
    from .engine.expand import auto_negatives
    ref = _ref()
    suite = loader.load(a.suite)
    base = next(s for s in suite.scenarios if s.id == a.scenario)
    base.profiles.card = base.profiles.card or suite.defaults.card
    base.profiles.terminal = base.profiles.terminal or suite.defaults.terminal
    negs = auto_negatives(base, ref)
    print(yaml.safe_dump({"scenarios": [n.model_dump(exclude_defaults=True) for n in negs]}, allow_unicode=True,
                         sort_keys=False))
    return 0


def cmd_coverage(a) -> int:
    from .analysis.coverage import compute
    from .orchestrator.store import Store
    st = Store(BASE / "data/itf.sqlite")
    mans = [json.loads(p.read_text()) for p in (BASE / "data/workflows").glob("*/out/manifest.json")]
    if a.manifest:
        mans.append(json.loads(Path(a.manifest).read_text()))
    rep = compute(mans, _ref(), st.requirements(), st.last_analyses())
    print(json.dumps({k: v for k, v in rep.to_dict().items() if k != "suggestions"}, indent=1, ensure_ascii=False))
    return 0


def cmd_usage(a) -> int:
    from .llm.router import LlmRouter
    print(json.dumps(LlmRouter(BASE / "config/llm.yaml", BASE).usage_report(), indent=1, ensure_ascii=False))
    return 0


def cmd_schema(a) -> int:
    from .dsl.models import json_schema
    print(json.dumps(json_schema(), indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="itf", description="ISO-TestForge — génération de tests ISO 8583 pour JMeter")
    sp = ap.add_subparsers(dest="cmd", required=True)

    g = sp.add_parser("generate", help="Suite DSL -> JMX + CSV + keys.properties + manifest")
    g.add_argument("suite")
    g.add_argument("-o", "--out")
    g.add_argument("--force", action="store_true")
    g.set_defaults(fn=cmd_generate)

    v = sp.add_parser("validate", help="Valider une suite DSL ou un JMX")
    v.add_argument("path")
    v.set_defaults(fn=cmd_validate)

    r = sp.add_parser("run", help="Générer, exécuter et analyser une suite")
    r.add_argument("suite")
    r.add_argument("--mode", choices=["native", "jmeter"], default="native")
    r.add_argument("--host")
    r.add_argument("--port", type=int)
    r.add_argument("--no-llm", action="store_true")
    r.set_defaults(fn=cmd_run)

    p = sp.add_parser("pipeline", help="Besoins -> intentions -> DSL -> JMX [-> exécution] (mode automatique)")
    p.add_argument("source", help="fichier de besoins (.md .txt .docx .pdf .xlsx) ou jira:<JQL>")
    p.add_argument("--name", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--execute", choices=["native", "jmeter", "none"], default="none")
    p.add_argument("--exec-host")
    p.add_argument("--exec-port", type=int)
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(fn=cmd_pipeline)

    s = sp.add_parser("serve", help="Interface web + API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_serve)

    sim = sp.add_parser("simulator", help="Simulateur hôte ISO 8583")
    sim.add_argument("--port", type=int)
    sim.add_argument("--config")
    sim.set_defaults(fn=cmd_simulator)

    d = sp.add_parser("decode", help="Décoder un message (texte, hexa, XML jPOS ou @fichier)")
    d.add_argument("message")
    d.add_argument("--host")
    d.add_argument("--clear", action="store_true", help="ne pas masquer PAN / piste")
    d.set_defaults(fn=cmd_decode)

    t = sp.add_parser("traces", help="Traces -> suite DSL de régression")
    t.add_argument("file")
    t.add_argument("--host", required=True)
    t.add_argument("--name", default="TRACE_REGRESSION")
    t.add_argument("--requirement")
    t.add_argument("-o", "--out")
    t.set_defaults(fn=cmd_traces)

    sd = sp.add_parser("spec-diff", help="Différences entre deux packagers")
    sd.add_argument("old")
    sd.add_argument("new")
    sd.set_defaults(fn=cmd_spec_diff)

    c = sp.add_parser("cards", help="Générer des cartes de test")
    c.add_argument("--bin", required=True)
    c.add_argument("--count", type=int, default=1)
    c.add_argument("--type", default="emv", choices=["emv", "contactless", "magstripe", "ecom", "manual"])
    c.add_argument("--expiry", default="3012")
    c.add_argument("--pin", default="1234")
    c.add_argument("--keyset")
    c.add_argument("--cvn", type=int, default=10)
    c.add_argument("--start", type=int, default=100)
    c.add_argument("--save", action="store_true")
    c.set_defaults(fn=cmd_cards)

    k = sp.add_parser("keys", help="Outils clés de test : kcv <clé> | generate [bits] | list")
    k.add_argument("action", choices=["kcv", "generate", "list"])
    k.add_argument("value", nargs="?")
    k.set_defaults(fn=cmd_keys)

    n = sp.add_parser("negatives", help="Cas négatifs automatiques pour un scénario nominal")
    n.add_argument("suite")
    n.add_argument("scenario")
    n.set_defaults(fn=cmd_negatives)

    cv = sp.add_parser("coverage", help="Couverture et trous")
    cv.add_argument("--manifest")
    cv.set_defaults(fn=cmd_coverage)

    sp.add_parser("usage", help="Consommation LLM").set_defaults(fn=cmd_usage)
    sp.add_parser("schema", help="JSON Schema du DSL").set_defaults(fn=cmd_schema)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
