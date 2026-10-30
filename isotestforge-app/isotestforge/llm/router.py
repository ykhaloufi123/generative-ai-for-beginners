"""Routeur LLM : choix du modèle par tâche, sortie structurée, caches, budget, escalade, batch.

Principes de coût :
  - sortie structurée forcée (tool use) -> pas de relance pour format invalide ;
  - préfixe système stable marqué pour le cache de prompt -> tokens répétés facturés au tarif réduit ;
  - cache exact puis sémantique -> une demande déjà traitée ne coûte rien ;
  - correction ciblée (seules les erreurs du validateur sont renvoyées) puis escalade de modèle ;
  - plafonds de tokens par suite et par jour.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import requests
import yaml

from .cache import LlmCache
from .pii import guard


class LlmUnavailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass
class LlmResult:
    data: dict
    model: str = ""
    source: str = "llm"            # llm | cache_exact | cache_semantic
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    similarity: float | None = None
    log: list[str] = field(default_factory=list)


class LlmRouter:
    def __init__(self, config_path: str | Path = "config/llm.yaml", base_dir: str | Path = "."):
        self.base = Path(base_dir)
        p = Path(config_path)
        if not p.is_absolute():
            p = self.base / p
        self.cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {"enabled": False}
        cache_db = self.base / self.cfg.get("cache", {}).get("db", "data/cache/llm_cache.sqlite")
        self.cache = LlmCache(cache_db)
        self.cache.db.execute("""CREATE TABLE IF NOT EXISTS usage (ts REAL, day TEXT, task TEXT, suite TEXT,
            model TEXT, input INTEGER, output INTEGER, cache_read INTEGER, cost REAL, source TEXT)""")
        self.cache.db.commit()

    # ------------------------------------------------------------------ état
    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.cfg.get("api_key_env", "ANTHROPIC_API_KEY"))

    @property
    def available(self) -> bool:
        return bool(self.cfg.get("enabled")) and bool(self.api_key)

    def _tokens_used(self, suite: str | None = None) -> tuple[int, int]:
        day = date.today().isoformat()
        d = self.cache.db.execute("SELECT COALESCE(SUM(input+output),0) FROM usage WHERE day=?", (day,)).fetchone()[0]
        s = self.cache.db.execute("SELECT COALESCE(SUM(input+output),0) FROM usage WHERE suite=?",
                                  (suite or "",)).fetchone()[0] if suite else 0
        return d, s

    def _check_budget(self, suite: str | None) -> None:
        b = self.cfg.get("budget", {})
        day, s = self._tokens_used(suite)
        if day >= b.get("max_tokens_per_day", 10 ** 12):
            raise BudgetExceeded(f"Plafond journalier atteint ({day} tokens)")
        if suite and s >= b.get("max_tokens_per_suite", 10 ** 12):
            raise BudgetExceeded(f"Plafond de la suite {suite} atteint ({s} tokens)")

    def _cost(self, model: str, inp: int, out: int, cache_read: int) -> float:
        p = self.cfg.get("pricing", {}).get(model, {})
        return (inp * p.get("input", 0) + out * p.get("output", 0) + cache_read * p.get("cache_read", 0)) / 1e6

    def _record(self, task, suite, model, inp, out, cr, cost, source):
        self.cache.db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (time.time(), date.today().isoformat(), task, suite or "", model, inp, out, cr,
                               cost, source))
        self.cache.db.commit()

    def usage_report(self) -> dict:
        rows = self.cache.db.execute("""SELECT task, model, source, COUNT(*), SUM(input), SUM(output),
            SUM(cache_read), ROUND(SUM(cost),4) FROM usage GROUP BY task, model, source""").fetchall()
        tot = self.cache.db.execute("SELECT COUNT(*), COALESCE(SUM(input+output),0), COALESCE(SUM(cost),0), "
                                    "SUM(source!='llm') FROM usage").fetchone()
        return {"calls": tot[0], "tokens": tot[1], "cost_usd": round(tot[2] or 0, 4),
                "cache_hit_rate": round(100 * (tot[3] or 0) / max(1, tot[0]), 1), "cache": self.cache.stats(),
                "detail": [dict(zip(["task", "model", "source", "calls", "input", "output", "cache_read", "cost"], r))
                           for r in rows]}

    # ------------------------------------------------------------------ fournisseurs
    def _call(self, model: str, system_static: str, system_dynamic: str, messages: list[dict], tool: dict) -> tuple:
        timeout = self.cfg.get("timeout_s", 120)
        max_tokens = self.cfg.get("max_output_tokens", 8000)
        if self.cfg.get("provider", "anthropic") == "anthropic":
            system = [{"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}}]
            if system_dynamic:
                system.append({"type": "text", "text": system_dynamic})
            body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages,
                    "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]}}
            r = requests.post(self.cfg.get("base_url", "https://api.anthropic.com").rstrip("/") + "/v1/messages",
                              headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"}, json=body, timeout=timeout)
            if r.status_code >= 400:
                raise LlmUnavailable(f"API {r.status_code} : {r.text[:300]}")
            j = r.json()
            block = next((c for c in j.get("content", []) if c.get("type") == "tool_use"), None)
            if not block:
                raise LlmUnavailable("Réponse sans sortie structurée")
            u = j.get("usage", {})
            return (block["input"], block, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                    u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0))
        # Fournisseur compatible OpenAI (modèle interne, vLLM, Ollama...)
        sysmsg = system_static + ("\n\n" + system_dynamic if system_dynamic else "")
        oa_messages = [{"role": "system", "content": sysmsg}]
        for m in messages:
            content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
            oa_messages.append({"role": m["role"], "content": content})
        body = {"model": model, "messages": oa_messages, "max_tokens": max_tokens,
                "tools": [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                            "parameters": tool["input_schema"]}}],
                "tool_choice": {"type": "function", "function": {"name": tool["name"]}}}
        r = requests.post(self.cfg["base_url"].rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {self.api_key}"}, json=body, timeout=timeout)
        if r.status_code >= 400:
            raise LlmUnavailable(f"API {r.status_code} : {r.text[:300]}")
        j = r.json()
        call = j["choices"][0]["message"]["tool_calls"][0]["function"]
        u = j.get("usage", {})
        return json.loads(call["arguments"]), None, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0

    # ------------------------------------------------------------------ API principale
    def structured(self, task: str, system_static: str, user: str, schema: dict, tool_name: str,
                   validate: Callable[[dict], list[str]] | None = None, system_dynamic: str = "",
                   context_version: str = "", suite: str | None = None,
                   semantic_query: str | None = None) -> LlmResult:
        user = guard(user)
        ccfg = self.cfg.get("cache", {})
        key = LlmCache.key(task, system_dynamic + "\n" + user, context_version)
        if ccfg.get("exact", True):
            hit = self.cache.get_exact(key)
            if hit is not None and (not validate or not validate(hit)):
                self._record(task, suite, "-", 0, 0, 0, 0.0, "cache_exact")
                return LlmResult(hit, source="cache_exact")
        if ccfg.get("semantic", True) and semantic_query:
            sh = self.cache.get_semantic(task, semantic_query + "|" + context_version,
                                         ccfg.get("semantic_threshold", 0.92))
            if sh and (not validate or not validate(sh[0])):
                self._record(task, suite, "-", 0, 0, 0, 0.0, "cache_semantic")
                return LlmResult(sh[0], source="cache_semantic", similarity=round(sh[1], 3))
        if not self.available:
            raise LlmUnavailable("LLM désactivé ou clé API absente")

        tool = {"name": tool_name, "description": f"Renvoie le résultat de la tâche {task} au format exigé.",
                "input_schema": schema}
        order = self.cfg.get("escalation", {}).get("order", ["light", "medium", "heavy"])
        start = self.cfg.get("routing", {}).get(task, "medium")
        tiers = order[order.index(start):] if start in order else order
        per_tier = self.cfg.get("escalation", {}).get("max_attempts_per_tier", 2)
        res = LlmResult({})
        last_errors: list[str] = []
        for tier in tiers:
            model = self.cfg["tiers"][tier]
            messages = [{"role": "user", "content": user}]
            for _ in range(per_tier):
                self._check_budget(suite)
                res.attempts += 1
                data, block, inp, out, cr = self._call(model, system_static, system_dynamic, messages, tool)
                cost = self._cost(model, inp, out, cr)
                res.input_tokens += inp
                res.output_tokens += out
                res.cache_read_tokens += cr
                res.cost_usd += cost
                self._record(task, suite, model, inp, out, cr, cost, "llm")
                errors = validate(data) if validate else []
                if not errors:
                    res.data, res.model = data, model
                    self.cache.put(key, task, (semantic_query or user) + "|" + context_version, data, model)
                    return res
                last_errors = errors
                res.log.append(f"{model} : {len(errors)} erreur(s) de validation")
                # Correction ciblée : on renvoie uniquement les erreurs, pas tout le contexte
                if block is not None:
                    messages = messages[:1] + [
                        {"role": "assistant", "content": [{"type": "tool_use", "id": block["id"], "name": tool_name,
                                                           "input": data}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block["id"],
                                                      "is_error": True,
                                                      "content": "Corrige uniquement ces erreurs :\n- " +
                                                                 "\n- ".join(errors[:30])}]}]
                else:
                    messages = messages[:1] + [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                                               {"role": "user", "content": "Corrige uniquement ces erreurs :\n- " +
                                                                           "\n- ".join(errors[:30])}]
            res.log.append(f"Escalade après {per_tier} tentatives sur {model}")
        raise LlmUnavailable("Validation impossible après escalade : " + "; ".join(last_errors[:5]))

    # ------------------------------------------------------------------ batch (régression nocturne)
    def submit_batch(self, items: list[dict]) -> str:
        """items : [{custom_id, task, system_static, user, schema, tool_name}] -> identifiant de batch."""
        if self.cfg.get("provider", "anthropic") != "anthropic":
            raise LlmUnavailable("Batch disponible uniquement avec le fournisseur anthropic")
        reqs = []
        for it in items:
            model = self.cfg["tiers"][self.cfg.get("routing", {}).get(it["task"], "medium")]
            reqs.append({"custom_id": it["custom_id"], "params": {
                "model": model, "max_tokens": self.cfg.get("max_output_tokens", 8000),
                "system": [{"type": "text", "text": it["system_static"], "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": guard(it["user"])}],
                "tools": [{"name": it["tool_name"], "description": "Résultat structuré",
                           "input_schema": it["schema"]}],
                "tool_choice": {"type": "tool", "name": it["tool_name"]}}})
        r = requests.post(self.cfg["base_url"].rstrip("/") + "/v1/messages/batches",
                          headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                          json={"requests": reqs}, timeout=60)
        r.raise_for_status()
        return r.json()["id"]

    def batch_results(self, batch_id: str) -> dict | None:
        h = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        base = self.cfg["base_url"].rstrip("/")
        st = requests.get(f"{base}/v1/messages/batches/{batch_id}", headers=h, timeout=30).json()
        if st.get("processing_status") != "ended":
            return None
        out = {}
        for line in requests.get(st["results_url"], headers=h, timeout=60).text.splitlines():
            d = json.loads(line)
            msg = d.get("result", {}).get("message", {})
            block = next((c for c in msg.get("content", []) if c.get("type") == "tool_use"), None)
            out[d["custom_id"]] = block["input"] if block else None
        return out
