"""Agents IA : Analyste, Concepteur, Diagnostic, Outilleur — tous avec repli sans IA.

Chaque agent :
  1. tente d'abord le chemin déterministe (heuristiques, bibliothèque, base de connaissances) ;
  2. n'appelle le LLM que pour le reste, avec un contexte minimal (identifiants, pas de données) ;
  3. valide la sortie (schéma + référentiel + dry-run du binder) et renvoie les erreurs exactes au LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from ..dsl.models import Scenario, Suite, scenarios_json_schema
from ..engine import mutators
from ..engine.binder import Binder, BindError
from ..engine.expand import expand_suite
from ..llm.router import BudgetExceeded, LlmResult, LlmRouter, LlmUnavailable
from ..referential.models import MessageTemplate
from ..referential.repository import Referential, ReferentialError
from . import heuristics
from .intents import AnalysisResult, Intent, analysis_schema
from .prompts import ANALYST_SYSTEM, DESIGNER_SYSTEM, DIAGNOSER_SYSTEM, TOOLSMITH_SYSTEM


@dataclass
class AgentTrace:
    agent: str
    path: str                    # heuristic | llm | cache_exact | cache_semantic | library
    detail: str = ""
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class AgentOutput:
    data: object
    traces: list[AgentTrace] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def _trace(agent: str, r: LlmResult, detail: str = "") -> AgentTrace:
    return AgentTrace(agent, r.source, detail or f"{r.model} x{r.attempts}" if r.model else detail,
                      r.input_tokens + r.output_tokens, round(r.cost_usd, 5))


def referential_context(ref: Referential) -> str:
    """Contexte variable minimal : identifiants et attributs non sensibles uniquement."""
    cards = "; ".join(f"{c.id}({c.scheme},{c.card_type},{c.cvm},{c.status})" for c in ref.cards.values())
    terms = "; ".join(f"{t.id}({t.terminal_kind},mcc {t.mcc},plafond sans contact {t.contactless_cvm_limit})"
                      for t in ref.terminals.values())
    tpls = "; ".join(f"{t.id}={t.mti} {t.description}" for t in ref.templates.values())
    return (f"Profils carte : {cards}\nProfils terminal : {terms}\nTemplates : {tpls}\n"
            f"Mutateurs :\n{mutators.catalog_for_prompt()}")


# ============================================================================ Analyste
class AnalystAgent:
    def __init__(self, ref: Referential, router: LlmRouter | None):
        self.ref, self.router = ref, router

    def run(self, req_id: str, text: str, force_llm: bool = False, suite: str | None = None) -> AgentOutput:
        h = heuristics.analyse(req_id, text)
        needs_llm = force_llm or any(i.complex for i in h.intents)
        if not needs_llm:
            return AgentOutput(h.intents, [AgentTrace("analyste", "heuristic", f"{len(h.intents)} intention(s)")],
                               h.questions)
        if not self.router:
            return AgentOutput(h.intents, [AgentTrace("analyste", "heuristic", "LLM non configuré")], h.questions)

        def validate(d: dict) -> list[str]:
            try:
                res = AnalysisResult.model_validate(d)
            except ValidationError as e:
                return [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            return [f"intention {i.id} : mutateur inconnu {i.negative}" for i in res.intents
                    if i.negative and i.negative not in mutators.CATALOG]

        user = (f"Exigence {req_id} :\n{text}\n\nCatalogue des mutateurs :\n{mutators.catalog_for_prompt()}\n"
                f"Préfixe les identifiants d'intention par {req_id}-I.")
        try:
            r = self.router.structured("requirement_analysis", ANALYST_SYSTEM, user, analysis_schema(),
                                       "enregistrer_intentions", validate, suite=suite,
                                       context_version=self.ref.version(), semantic_query=text)
        except (LlmUnavailable, BudgetExceeded) as e:
            return AgentOutput(h.intents, [AgentTrace("analyste", "heuristic", f"repli : {e}")],
                               h.questions + [f"{req_id} : analyse IA indisponible ({e})"])
        res = AnalysisResult.model_validate(r.data)
        return AgentOutput(res.intents, [_trace("analyste", r)], res.questions)


# ============================================================================ Concepteur
class DesignerAgent:
    def __init__(self, ref: Referential, router: LlmRouter | None, library=None, host_id: str | None = None):
        self.ref, self.router, self.library = ref, router, library
        self.host_id = host_id or next(iter(ref.hosts))

    def _check(self, scenarios: list[Scenario]) -> list[str]:
        """Validation complète par le moteur réel : expansion + liaison (dry-run)."""
        errors: list[str] = []
        for s in scenarios:
            for attr, getter in (("card", self.ref.cards), ("terminal", self.ref.terminals)):
                v = getattr(s.profiles, attr)
                if v and v not in getter:
                    errors.append(f"{s.id} : profil {attr} inconnu '{v}' (choisir parmi {sorted(getter)})")
            for st in s.steps:
                if st.template and st.template not in self.ref.templates:
                    errors.append(f"{s.id}.{st.id} : template inconnu '{st.template}'")
            if s.mutation and s.mutation not in mutators.CATALOG:
                errors.append(f"{s.id} : mutateur inconnu '{s.mutation}'")
        if errors:
            return errors
        try:
            suite = Suite(suite="CHECK", host=self.host_id, scenarios=scenarios)
            exp, _ = expand_suite(suite, self.ref)
            Binder(self.ref, suite).bind(exp)
        except (ValueError, BindError, ReferentialError, ValidationError) as e:
            errors.append(str(e)[:400])
        return errors

    def run(self, intents: list[Intent], suite: str | None = None) -> AgentOutput:
        out: list[Scenario] = []
        traces: list[AgentTrace] = []
        rest: list[Intent] = []
        for it in intents:
            lib = self.library.find(it.summary, it.operation, it.channel) if self.library else None
            if lib:
                s = lib.model_copy(deep=True)
                s.id, s.requirement, s.origin = it.id, [it.requirement], "library"
                if not self._check([s]):
                    out.append(s)
                    traces.append(AgentTrace("concepteur", "library", f"{it.id} <- {lib.id}"))
                    continue
            s = heuristics.design(it, self.ref)
            if s and not self._check([s]):
                out.append(s)
                traces.append(AgentTrace("concepteur", "heuristic", it.id))
            else:
                rest.append(it)
        if not rest:
            return AgentOutput(out, traces)
        if not self.router:
            traces.append(AgentTrace("concepteur", "heuristic", f"{len(rest)} intention(s) non traitée(s) sans LLM"))
            return AgentOutput(out, traces, [f"{i.id} : à concevoir manuellement ({i.summary})" for i in rest])

        def validate(d: dict) -> list[str]:
            try:
                scns = [Scenario.model_validate(x) for x in d.get("scenarios", [])]
            except ValidationError as e:
                return [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()][:20]
            if not scns:
                return ["aucun scénario produit"]
            return self._check(scns)

        examples = self.library.examples(rest, k=3) if self.library else []
        user = ("Intentions à concevoir :\n" + json.dumps([i.model_dump(exclude_defaults=True) for i in rest],
                                                         ensure_ascii=False) +
                ("\n\nExemples validés de la bibliothèque :\n" + "\n".join(examples) if examples else ""))
        try:
            r = self.router.structured("scenario_design", DESIGNER_SYSTEM, user, scenarios_json_schema(),
                                       "enregistrer_scenarios", validate, system_dynamic=referential_context(self.ref),
                                       context_version=self.ref.version(), suite=suite,
                                       semantic_query=" | ".join(i.summary for i in rest))
        except (LlmUnavailable, BudgetExceeded) as e:
            traces.append(AgentTrace("concepteur", "heuristic", f"repli : {e}"))
            return AgentOutput(out, traces, [f"{i.id} : conception IA indisponible ({i.summary})" for i in rest])
        for x in r.data["scenarios"]:
            s = Scenario.model_validate(x)
            s.origin = "ai"
            out.append(s)
        traces.append(_trace("concepteur", r))
        return AgentOutput(out, traces)


# ============================================================================ Diagnostic
class DiagnoserAgent:
    def __init__(self, router: LlmRouter | None):
        self.router = router

    def __call__(self, ctx: dict) -> dict | None:
        if not self.router or not self.router.available:
            return None
        schema = {"type": "object", "properties": {"cause": {"type": "string"}, "action": {"type": "string"}},
                  "required": ["cause", "action"]}
        try:
            r = self.router.structured("result_diagnosis", DIAGNOSER_SYSTEM, json.dumps(ctx, ensure_ascii=False),
                                       schema, "diagnostic", semantic_query=f"{ctx.get('got')}|{ctx.get('expected')}|"
                                                                            f"{ctx.get('mti')}|{ctx.get('message')}")
        except (LlmUnavailable, BudgetExceeded):
            return None
        return {"id": "IA", **r.data, "source": "ia (à valider)"}


# ============================================================================ Outilleur
class ToolsmithAgent:
    """Propose un template réutilisable pour des intentions récurrentes non couvertes (« compiler une fois »)."""

    def __init__(self, ref: Referential, router: LlmRouter | None):
        self.ref, self.router = ref, router

    def propose(self, intents: list[Intent]) -> MessageTemplate | None:
        if not self.router or not self.router.available or not intents:
            return None

        def validate(d: dict) -> list[str]:
            try:
                t = MessageTemplate.model_validate(d)
            except ValidationError as e:
                return [str(x["msg"]) for x in e.errors()]
            return [f"identifiant {t.id} déjà utilisé"] if t.id in self.ref.templates else []

        user = "Intentions non couvertes :\n" + "\n".join(f"- {i.summary}" for i in intents) + \
               "\n\nTemplates existants :\n" + "\n".join(f"- {t.id}: {t.mti} {t.description}"
                                                         for t in self.ref.templates.values())
        try:
            r = self.router.structured("template_proposal", TOOLSMITH_SYSTEM, user, MessageTemplate.model_json_schema(),
                                       "proposer_template", validate)
        except (LlmUnavailable, BudgetExceeded):
            return None
        return MessageTemplate.model_validate(r.data)
