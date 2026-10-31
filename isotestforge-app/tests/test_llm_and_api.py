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

    def provider_key(self, pid):
        return ("test", "env") if pid == "anthropic" else (None, "aucune")

    def _call(self, model, system_static, system_dynamic, messages, tool, max_tokens=None):
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


class MultiRouter(FakeRouter):
    """Deux fournisseurs configurés ; le premier est en panne."""

    def provider_key(self, pid):
        return ("test", "env") if pid in ("anthropic", "mistral") else (None, "aucune")

    def _call(self, model, system_static, system_dynamic, messages, tool, max_tokens=None):
        self.calls.append((model, max_tokens))
        if model.startswith("anthropic/"):
            from isotestforge.llm.router import LlmUnavailable
            raise LlmUnavailable("anthropic : API 529")
        return self.answers.pop(0), None, 100, 20, 0


def test_router_fallback_provider_and_small_prompt_downgrade(workspace):
    router = MultiRouter(workspace, [{"ok": True}])
    assert router.candidates("light") == ["anthropic/claude-haiku-5-5", "mistral/mistral-small-latest"]
    res = router.structured("scenario_design", "sys", "court", {"type": "object"}, "outil")
    # Demande courte : scenario_design (medium) démarre en light ; panne anthropic -> repli mistral
    assert [c[0] for c in router.calls] == ["anthropic/claude-haiku-5-5", "mistral/mistral-small-latest"]
    assert res.model == "mistral/mistral-small-latest" and any("repli" in x for x in res.log)
    router.calls.clear()
    router.answers = [{"ok": True}]
    router.structured("requirement_analysis", "sys", "x" * 50, {"type": "object"}, "outil")
    assert router.calls[-1] == ("mistral/mistral-small-latest", 2000)     # plafond de sortie par tâche
    # Aucun candidat en medium/heavy : repli sur le meilleur niveau inférieur disponible
    router.cfg["tiers"]["medium"] = router.cfg["tiers"]["heavy"] = ["openai/gpt-4o"]
    assert router._start_tiers("result_diagnosis", 5000) == ["light"]


def test_legacy_config_is_normalized():
    from isotestforge.llm.router import normalize_config, validate_config
    cfg = normalize_config({"provider": "openai_compatible", "base_url": "http://llm:8000/v1",
                            "api_key_env": "K", "tiers": {"light": "petit", "medium": "moyen"},
                            "routing": {"x": "light"}, "escalation": {"order": ["light", "medium"]}})
    assert cfg["tiers"]["light"] == ["default/petit"] and cfg["providers"]["default"]["api_key_env"] == "K"
    assert validate_config(cfg) == []
    cfg["tiers"]["light"].append("inconnu/m")
    assert validate_config(cfg)


def test_llm_config_api(workspace, monkeypatch):
    monkeypatch.setenv("ITF_HOME", str(workspace))
    monkeypatch.delenv("ITF_NO_LLM", raising=False)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    import isotestforge.api.app as appmod
    importlib.reload(appmod)
    from starlette.testclient import TestClient
    c = TestClient(appmod.app)
    d = c.get("/api/llm/config").json()
    assert d["status"]["available"] is False and "scenario_design" in d["tasks"]
    st = c.post("/api/llm/key", json={"provider": "mistral", "key": "secret"}).json()
    assert st["providers"]["mistral"]["key_source"] == "interface" and st["active"]["light"].startswith("mistral/")
    assert "secret" not in c.get("/api/llm/config").text                      # la clé n'est jamais renvoyée
    cfg = d["config"]
    cfg["routing"]["scenario_design"] = "light"
    assert c.put("/api/llm/config", json={"config": cfg}).json()["config"]["routing"]["scenario_design"] == "light"
    assert (workspace / "data/llm_config.yaml").exists()
    cfg["tiers"]["light"] = ["absent/modele"]
    r = c.put("/api/llm/config", json={"config": cfg})
    assert r.status_code == 422 and r.json()["errors"]
    assert c.delete("/api/llm/config").json()["status"]["source"] == "fichier"
