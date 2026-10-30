"""Orchestrateur : workflow besoin -> intentions -> DSL -> JMX -> exécution -> analyse.

Deux validations humaines (intentions, DSL final) ; en mode auto (CI) elles sont passées.
Chaque étape écrit son artefact dans data/workflows/<id>/ : un workflow interrompu reprend
là où il s'est arrêté, sans rappeler le LLM.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from ..agents.agents import AnalystAgent, DesignerAgent, DiagnoserAgent, ToolsmithAgent
from ..agents.intents import Intent
from ..analysis import coverage as covmod
from ..analysis.diagnostics import DiagnosticsKB
from ..analysis.report import write_excel, write_html
from ..analysis.results import analyse, read_jtl
from ..dsl import loader
from ..dsl.models import Defaults, Scenario, Suite
from ..engine.binder import Binder, keys_properties
from ..engine.expand import expand_suite
from ..engine.jmx_builder import write_outputs
from ..engine.validator import validate_bound, validate_jmx
from ..llm.router import LlmRouter
from ..referential.repository import Referential
from ..runner import jmeter as jmeter_runner
from ..runner.native import NativeRunner, write_jtl
from .store import Library, Store

log = logging.getLogger("itf.pipeline")

STATUSES = ["created", "intents_review", "designed", "dsl_review", "compiled", "executed", "done", "error"]


class Pipeline:
    def __init__(self, base_dir: str | Path = ".", use_llm: bool = True):
        self.base = Path(base_dir).resolve()
        self.ref = Referential(self.base / "data/referential", self.base)
        self.store = Store(self.base / "data/itf.sqlite")
        self.router = LlmRouter(self.base / "config/llm.yaml", self.base) if use_llm else None
        self.kb = DiagnosticsKB(self.base / "data/knowledge/diagnostics.yaml")
        self.library = Library(self.store)

    def wdir(self, wid: str) -> Path:
        p = self.base / "data/workflows" / wid
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------------ 1. démarrage + analyse
    def start(self, name: str, host: str, requirements: list[dict], auto: bool = False) -> str:
        self.ref.host(host)
        self.store.upsert_requirements(requirements)
        wid = self.store.create_workflow(name, host, {"requirements": requirements, "auto": auto, "traces": []})
        (self.wdir(wid) / "requirements.json").write_text(json.dumps(requirements, indent=2, ensure_ascii=False))
        return wid

    def analyse(self, wid: str, force_llm: bool = False) -> dict:
        wf = self.store.get_workflow(wid)
        agent = AnalystAgent(self.ref, self.router)
        intents, questions, traces = [], [], []
        for r in wf["data"]["requirements"]:
            out = agent.run(r["id"], r["text"], force_llm=force_llm, suite=wf["name"])
            intents += [i.model_dump() for i in out.data]
            questions += out.questions
            traces += [t.__dict__ for t in out.traces]
        (self.wdir(wid) / "intents.json").write_text(json.dumps(intents, indent=2, ensure_ascii=False))
        wf = self.store.update_workflow(wid, "intents_review", intents=intents, questions=questions,
                                        traces=wf["data"]["traces"] + traces)
        if wf["data"].get("auto"):
            return self.approve_intents(wid, intents)
        return wf

    def approve_intents(self, wid: str, intents: list[dict] | None = None) -> dict:
        wf = self.store.get_workflow(wid)
        intents = intents if intents is not None else wf["data"]["intents"]
        self.store.update_workflow(wid, None, intents=[Intent.model_validate(i).model_dump() for i in intents],
                                   intents_approved=True)
        return self.design(wid)

    # ------------------------------------------------------------------ 2. conception
    def design(self, wid: str) -> dict:
        wf = self.store.get_workflow(wid)
        intents = [Intent.model_validate(i) for i in wf["data"]["intents"]]
        agent = DesignerAgent(self.ref, self.router, self.library, wf["host"])
        out = agent.run(intents, suite=wf["name"])
        scenarios: list[Scenario] = out.data
        unmapped = [i for i in intents if not any(i.id == s.id for s in scenarios)]
        if unmapped and self.router and self.router.available:
            tpl = ToolsmithAgent(self.ref, self.router).propose(unmapped)
            if tpl:
                self.store.add_proposal("template", tpl.model_dump())
        suite = Suite(suite=wf["name"], host=wf["host"], description="Généré par ISO-TestForge",
                      defaults=Defaults(), scenarios=scenarios)
        text = loader.dump(suite)
        d = self.wdir(wid)
        (d / "suite.generated.yaml").write_text(text, encoding="utf-8")
        traces = wf["data"]["traces"] + [t.__dict__ for t in out.traces]
        wf = self.store.update_workflow(wid, "designed", dsl=text, dsl_generated=text,
                                        questions=wf["data"].get("questions", []) + out.questions, traces=traces)
        return self.check(wid)

    # ------------------------------------------------------------------ 3. expansion, liaison, validation
    def check(self, wid: str, dsl_text: str | None = None) -> dict:
        wf = self.store.get_workflow(wid)
        text = dsl_text or wf["data"]["dsl"]
        suite = loader.parse(text)
        scns, rep = expand_suite(suite, self.ref)
        bound = Binder(self.ref, suite).bind(scns)
        val = validate_bound(bound, self.ref)
        preview = [{"id": s.id, "title": s.title, "requirements": s.requirements, "card": s.card_id,
                    "terminal": s.terminal_id, "mutation": s.mutation, "origin": s.origin,
                    "steps": [{"id": st.id, "mti": st.mti, "fields": st.field_map, "expect": st.expect,
                               "crypto": {k: v for k, v in st.crypto.__dict__.items() if k in ("pin", "mac", "arqc")}}
                              for st in s.steps]} for s in bound.scenarios]
        wf = self.store.update_workflow(wid, "dsl_review", dsl=text, validation=val.to_dict(),
                                        expansion=rep.__dict__, preview=preview)
        if wf["data"].get("auto"):
            return self.approve_dsl(wid)
        return wf

    def approve_dsl(self, wid: str, dsl_text: str | None = None) -> dict:
        wf = self.store.get_workflow(wid)
        text = dsl_text or wf["data"]["dsl"]
        suite = loader.parse(text)
        # Mode apprentissage : toute correction humaine du DSL généré devient un exemple
        try:
            gen = {s.id: s for s in loader.parse(wf["data"].get("dsl_generated", text)).scenarios}
            for s in suite.scenarios:
                g = gen.get(s.id)
                if g and g.model_dump() != s.model_dump():
                    self.store.add_correction(s.id, g.model_dump(exclude_defaults=True),
                                              s.model_dump(exclude_defaults=True))
        except Exception:
            pass
        (self.wdir(wid) / "suite.yaml").write_text(text, encoding="utf-8")
        intents = {i["id"]: i for i in wf["data"].get("intents", [])}
        for s in suite.scenarios:
            it = intents.get(s.id)
            if it and not s.mutation and s.steps:
                self.store.add_to_library(s, it["summary"], it["operation"], it["channel"])
        self.store.update_workflow(wid, None, dsl=text, dsl_approved=True)
        return self.compile(wid)

    # ------------------------------------------------------------------ 4. compilation JMX
    def compile(self, wid: str) -> dict:
        wf = self.store.get_workflow(wid)
        suite = loader.parse(wf["data"]["dsl"])
        scns, rep = expand_suite(suite, self.ref)
        bound = Binder(self.ref, suite).bind(scns)
        val = validate_bound(bound, self.ref)
        if not val.ok:
            return self.store.update_workflow(wid, "dsl_review", validation=val.to_dict(),
                                              error="Validation en échec : corriger le DSL")
        files = write_outputs(suite, self.ref, bound, self.wdir(wid) / "out")
        jv = validate_jmx(files["jmx"])
        return self.store.update_workflow(wid, "compiled", files=files, jmx_validation=jv.to_dict(),
                                          validation=val.to_dict())

    # ------------------------------------------------------------------ 5. exécution + analyse
    def execute(self, wid: str, mode: str = "native", host: str | None = None, port: int | None = None) -> dict:
        wf = self.store.get_workflow(wid)
        suite = loader.parse(wf["data"]["dsl"])
        out = self.wdir(wid) / "out"
        if wf["status"] not in ("compiled", "executed", "done"):
            self.compile(wid)
        if mode == "jmeter":
            res = jmeter_runner.run(out / f"{suite.suite}.jmx", out, host, port)
            jtl = res["jtl"]
            if not Path(jtl).exists():
                return self.store.update_workflow(wid, "error", error="JMeter n'a pas produit de résultats",
                                                  jmeter=res)
        else:
            scns, _ = expand_suite(suite, self.ref)
            bound = Binder(self.ref, suite).bind(scns)
            runner = NativeRunner(bound, self.ref.packager(suite.host), keys_properties(self.ref, bound.keysets),
                                  host, port)
            results = runner.run()
            jtl = str(out / "results.jtl")
            write_jtl(results, jtl)
            (out / "exchanges.json").write_text(json.dumps([r.__dict__ for r in results], indent=1,
                                                           ensure_ascii=False, default=str))
        return self.analyse_results(wid, jtl, mode)

    def analyse_results(self, wid: str, jtl: str, mode: str = "jmeter") -> dict:
        wf = self.store.get_workflow(wid)
        out = self.wdir(wid) / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        diag = DiagnoserAgent(self.router) if self.router else None
        an = analyse(read_jtl(jtl), manifest, self.kb, diag)
        ad = an.to_dict()
        self.store.add_run(wid, manifest["suite"], mode, ad)
        cov = covmod.compute([manifest], self.ref, wf["data"]["requirements"], [ad], wf["host"]).to_dict()
        hist = self.store.history(manifest["suite"])
        write_html(an, self.wdir(wid) / "report.html", coverage=cov, history=hist)
        write_excel(an, self.wdir(wid) / "report.xlsx", coverage=cov)
        (self.wdir(wid) / "analysis.json").write_text(json.dumps(ad, indent=2, ensure_ascii=False))
        return self.store.update_workflow(wid, "done", analysis=ad, coverage=cov,
                                          report=str(self.wdir(wid) / "report.html"),
                                          report_xlsx=str(self.wdir(wid) / "report.xlsx"))

    # ------------------------------------------------------------------ tout-en-un (CI)
    def run_all(self, name: str, host: str, requirements: list[dict], execute: str | None = "native",
                exec_host: str | None = None, exec_port: int | None = None) -> dict:
        wid = self.start(name, host, requirements, auto=True)
        wf = self.analyse(wid)
        if execute and wf["status"] == "compiled":
            wf = self.execute(wid, execute, exec_host, exec_port)
        return wf

    def suite_from_file(self, path: str | Path, auto: bool = True) -> str:
        """Démarre un workflow depuis une suite DSL existante (écrite à la main ou importée)."""
        suite = loader.load(path)
        reqs = sorted({r for s in suite.scenarios for r in s.requirement})
        wid = self.start(suite.suite, suite.host, [{"id": r, "text": "(suite importée)", "source": str(path)}
                                                   for r in reqs], auto=auto)
        text = Path(path).read_text(encoding="utf-8")
        self.store.update_workflow(wid, "designed", dsl=text, dsl_generated=text)
        self.check(wid)
        return wid

    def cost_summary(self, wid: str) -> dict:
        wf = self.store.get_workflow(wid)
        traces = wf["data"].get("traces", [])
        n = len(traces)
        no_ai = sum(t["path"] in ("heuristic", "library", "cache_exact", "cache_semantic") for t in traces)
        return {"agent_calls": n, "without_llm": no_ai, "share_without_llm": round(100 * no_ai / max(1, n), 1),
                "tokens": sum(t.get("tokens", 0) for t in traces),
                "cost_usd": round(sum(t.get("cost_usd", 0) for t in traces), 5)}
