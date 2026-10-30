"""Routeur LLM (fournisseur simulé), filtre PII, agents avec repli, API."""
import os

from isotestforge.agents.agents import AnalystAgent, DesignerAgent
from isotestforge.agents.intents import Intent
from isotestforge.llm import pii
from isotestforge.llm.router import LlmRouter


def test_pii_scrub():
    txt = "PAN 4761730000000011, clé FEF1CE0E1004D610527C7C67945D0237, piste 4761730000000011=30122010000"
    clean, found = pii.scrub(txt)
    assert "4761730000000011" not in clean and "FEF1CE0E" not in clean
    assert {"PAN", "KEY", "TRACK2"} <= set(found)


class FakeRouter(LlmRouter):
    """Fournisseur simulé : 1re réponse invalide puis corrigée -> teste correction ciblée, cache et usage."""

    def __init__(self, workspace, answers):
        super().__init__(workspace / "config/llm.yaml", workspace)
        self.cfg["enabled"] = True
        self.answers = list(answers)
        self.calls = []

    @property
    def api_key(self):
        return "test"

    def _call(self, model, system_static, system_dynamic, messages, tool):
        self.calls.append((model, messages))
        return self.answers.pop(0), {"id": "tu_1"}, 1000, 200, 800


def test_designer_llm_correction_loop_and_cache(ref, workspace):
    bad = {"scenarios": [{"id": "REQ-9-I1", "title": "x", "requirement": ["REQ-9"],
                          "profiles": {"card": "INCONNUE", "terminal": "POS_ATTENDED"},
                          "steps": [{"id": "s1", "template": "PURCHASE"}]}]}
    good = {"scenarios": [{"id": "REQ-9-I1", "title": "Achat avec montant spécial", "requirement": ["REQ-9"],
                           "profiles": {"card": "VISA_EMV_PIN", "terminal": "POS_ATTENDED"},
                           "steps": [{"id": "s1", "template": "PURCHASE", "amount": 13.13,
                                      "expect": {"39": "refer"}}]}]}
    router = FakeRouter(workspace, [bad, good])
    it = Intent(id="REQ-9-I1", requirement="REQ-9", summary="Cas spécifique : appel émetteur sur montant 13,13",
                operation="other", complex=True)
    out = DesignerAgent(ref, router).run([it])
    assert out.data[0].profiles.card == "VISA_EMV_PIN"
    assert len(router.calls) == 2
    correction = router.calls[1][1][-1]["content"][0]["content"]
    assert "INCONNUE" in correction                              # seules les erreurs sont renvoyées
    # Deuxième demande identique : servie par le cache, sans appel
    out2 = DesignerAgent(ref, router).run([it])
    assert len(router.calls) == 2 and out2.traces[-1].path == "cache_exact"
    assert router.usage_report()["calls"] >= 3


def test_analyst_fallback_without_llm(ref):
    out = AnalystAgent(ref, None).run("REQ-1", "Un retrait GAB avec PIN erroné doit être refusé.")
    assert out.data[0].operation == "withdrawal" and out.data[0].negative == "wrong_pin"
    assert out.traces[0].path == "heuristic"


def test_api_smoke(workspace, monkeypatch):
    monkeypatch.setenv("ITF_HOME", str(workspace))
    monkeypatch.setenv("ITF_NO_LLM", "1")
    import importlib
    import isotestforge.api.app as appmod
    importlib.reload(appmod)
    from starlette.testclient import TestClient
    c = TestClient(appmod.app)
    assert c.get("/health").text == "ok"
    assert "ISO-<span>TestForge" in c.get("/").text
    r = c.get("/api/referential").json()
    assert r["cards"] and r["mutators"]
    reqs = c.post("/api/requirements/parse", json={"text": "REQ-7 : Achat sans contact Visa de 100 MAD."}).json()
    wf = c.post("/api/workflows", json={"name": "API", "host": "SWITCH_SIM", "requirements": reqs,
                                       "auto": True}).json()
    assert wf["status"] == "compiled", wf
    jmx = c.get(f"/api/workflows/{wf['id']}/files/API.jmx")
    assert jmx.status_code == 200 and b"ISO8583Sampler" in jmx.content
    assert c.get(f"/api/workflows/{wf['id']}/files/../../itf.sqlite").status_code == 404
    st = c.post("/api/simulator/start", json={"port": 0}).json()
    assert st.get("running"), st
    c.post("/api/simulator/stop", json={})
