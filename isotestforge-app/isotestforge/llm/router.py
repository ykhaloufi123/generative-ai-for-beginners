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


PROVIDER_TYPES = ("anthropic", "openai_compatible")


def normalize_config(cfg: dict) -> dict:
    """Ramène l'ancien format (un seul fournisseur, tiers en chaînes) au format multi-fournisseurs."""
    cfg = dict(cfg or {})
    legacy = not cfg.get("providers")
    if legacy:
        ptype = cfg.get("provider", "anthropic")
        cfg["providers"] = {"default": {"type": ptype, "api_key_env": cfg.get("api_key_env", "ANTHROPIC_API_KEY"),
                                        "base_url": cfg.get("base_url") or ("https://api.anthropic.com"
                                                                            if ptype == "anthropic" else "")}}
    for k in ("provider", "api_key_env", "base_url"):
        cfg.pop(k, None)
    first = next(iter(cfg["providers"]))
    tiers = {}
    for tier, cands in (cfg.get("tiers") or {}).items():
        cands = [cands] if isinstance(cands, str) else list(cands or [])
        # Ancien format : les modèles n'ont pas de préfixe fournisseur
        tiers[tier] = [f"{first}/{c}" if legacy else c for c in cands]
    cfg["tiers"] = tiers
    return cfg


def validate_config(cfg: dict) -> list[str]:
    errors = []
    providers = cfg.get("providers") or {}
    if not providers:
        errors.append("aucun fournisseur défini")
    for pid, p in providers.items():
        if not pid or "/" in pid:
            errors.append(f"identifiant de fournisseur invalide : {pid!r}")
        if p.get("type") not in PROVIDER_TYPES:
            errors.append(f"{pid} : type inconnu {p.get('type')!r} (attendu : {', '.join(PROVIDER_TYPES)})")
        if p.get("type") == "openai_compatible" and not p.get("base_url"):
            errors.append(f"{pid} : base_url obligatoire")
    order = (cfg.get("escalation") or {}).get("order") or []
    for tier in order:
        if tier not in (cfg.get("tiers") or {}):
            errors.append(f"niveau {tier} présent dans l'ordre d'escalade mais non défini")
    for tier, cands in (cfg.get("tiers") or {}).items():
        for c in cands:
            if "/" not in c or c.split("/", 1)[0] not in providers or not c.split("/", 1)[1]:
                errors.append(f"niveau {tier} : candidat invalide {c!r} (format fournisseur/modèle)")
    for task, tier in (cfg.get("routing") or {}).items():
        if tier not in (cfg.get("tiers") or {}):
            errors.append(f"tâche {task} : niveau inconnu {tier!r}")
    return errors


class LlmRouter:
    def __init__(self, config_path: str | Path = "config/llm.yaml", base_dir: str | Path = "."):
        self.base = Path(base_dir)
        p = Path(config_path)
        self.config_path = p if p.is_absolute() else self.base / p
        self.override_path = self.base / "data/llm_config.yaml"
        self.keys_path = self.base / "data/llm_keys.json"
        self.reload()
        cache_db = self.base / self.cfg.get("cache", {}).get("db", "data/cache/llm_cache.sqlite")
        self.cache = LlmCache(cache_db)
        self.cache.db.execute("""CREATE TABLE IF NOT EXISTS usage (ts REAL, day TEXT, task TEXT, suite TEXT,
            model TEXT, input INTEGER, output INTEGER, cache_read INTEGER, cost REAL, source TEXT)""")
        self.cache.db.commit()

    # ------------------------------------------------------------------ configuration
    def reload(self) -> None:
        src = self.override_path if self.override_path.exists() else self.config_path
        raw = yaml.safe_load(src.read_text(encoding="utf-8")) if src.exists() else {"enabled": False}
        self.cfg = normalize_config(raw)
        self.config_source = "interface" if src == self.override_path else "fichier"
        try:
            self._stored_keys = json.loads(self.keys_path.read_text()) if self.keys_path.exists() else {}
        except ValueError:
            self._stored_keys = {}

    def save_config(self, cfg: dict) -> list[str]:
        cfg = normalize_config(cfg)
        errors = validate_config(cfg)
        if errors:
            return errors
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.reload()
        return []

    def reset_config(self) -> None:
        self.override_path.unlink(missing_ok=True)
        self.reload()

    def set_key(self, provider: str, key: str) -> None:
        """Clé saisie dans l'interface (fichier local, jamais renvoyée) ; vide = suppression."""
        if key:
            self._stored_keys[provider] = key
        else:
            self._stored_keys.pop(provider, None)
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        self.keys_path.write_text(json.dumps(self._stored_keys))
        os.chmod(self.keys_path, 0o600)

    # ------------------------------------------------------------------ état
    def provider_key(self, pid: str) -> tuple[str | None, str]:
        p = self.cfg["providers"].get(pid, {})
        env = p.get("api_key_env")
        if env and os.environ.get(env):
            return os.environ[env], "env"
        if self._stored_keys.get(pid):
            return self._stored_keys[pid], "interface"
        return None, "aucune"

    def provider_ready(self, pid: str) -> bool:
        p = self.cfg["providers"].get(pid)
        if not p or p.get("enabled") is False:
            return False
        return p.get("needs_key") is False or bool(self.provider_key(pid)[0])

    @property
    def api_key(self) -> str | None:
        """Clé du premier fournisseur prêt (compatibilité)."""
        for pid in self.cfg["providers"]:
            if self.provider_ready(pid):
                return self.provider_key(pid)[0]
        return None

    def candidates(self, tier: str) -> list[str]:
        return [c for c in self.cfg.get("tiers", {}).get(tier, []) if self.provider_ready(c.split("/", 1)[0])]

    @property
    def available(self) -> bool:
        return bool(self.cfg.get("enabled")) and any(self.candidates(t) for t in self.cfg.get("tiers", {}))

    def status(self) -> dict:
        providers = {}
        for pid, p in self.cfg["providers"].items():
            _, source = self.provider_key(pid)
            providers[pid] = {"ready": self.provider_ready(pid), "key_source": source,
                              "key_env": p.get("api_key_env"), "needs_key": p.get("needs_key") is not False}
        return {"enabled": bool(self.cfg.get("enabled")), "available": self.available, "source": self.config_source,
                "providers": providers, "active": {t: (self.candidates(t) or [None])[0] for t in self.cfg["tiers"]}}

    def _start_tiers(self, task: str, prompt_chars: int) -> list[str]:
        esc = self.cfg.get("escalation", {})
        order = esc.get("order", ["light", "medium", "heavy"])
        start = self.cfg.get("routing", {}).get(task, "medium")
        i = order.index(start) if start in order else 0
        if i > 0 and esc.get("downgrade_small_prompts") and prompt_chars < esc.get("small_prompt_chars", 1000):
            i -= 1
        if not any(self.candidates(t) for t in order[i:]):
            # Aucun fournisseur aux niveaux supérieurs : le meilleur niveau inférieur disponible plutôt que rien
            lower = [t for t in reversed(order[:i]) if self.candidates(t)]
            return lower[:1]
        return order[i:]

    def _max_tokens(self, task: str) -> int:
        return (self.cfg.get("max_output_tokens_by_task") or {}).get(task) or self.cfg.get("max_output_tokens", 8000)

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
        pricing = self.cfg.get("pricing", {})
        p = pricing.get(model) or pricing.get(model.split("/", 1)[-1], {})
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
    def _call(self, model: str, system_static: str, system_dynamic: str, messages: list[dict], tool: dict,
              max_tokens: int | None = None) -> tuple:
        """model = « fournisseur/modèle »."""
        pid, name = model.split("/", 1)
        p = self.cfg["providers"][pid]
        key = self.provider_key(pid)[0] or ""
        timeout = self.cfg.get("timeout_s", 120)
        max_tokens = max_tokens or self.cfg.get("max_output_tokens", 8000)
        try:
            if p["type"] == "anthropic":
                system = [{"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}}]
                if system_dynamic:
                    system.append({"type": "text", "text": system_dynamic})
                body = {"model": name, "max_tokens": max_tokens, "system": system, "messages": messages,
                        "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]}}
                r = requests.post(p.get("base_url", "https://api.anthropic.com").rstrip("/") + "/v1/messages",
                                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                           "content-type": "application/json"}, json=body, timeout=timeout)
                if r.status_code >= 400:
                    raise LlmUnavailable(f"{pid} : API {r.status_code} : {r.text[:300]}")
                j = r.json()
                block = next((c for c in j.get("content", []) if c.get("type") == "tool_use"), None)
                if not block:
                    raise LlmUnavailable(f"{pid} : réponse sans sortie structurée")
                u = j.get("usage", {})
                return (block["input"], block, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                        u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0))
            # Fournisseur compatible OpenAI (OpenAI, Mistral, Groq, vLLM, Ollama...)
            sysmsg = system_static + ("\n\n" + system_dynamic if system_dynamic else "")
            oa_messages = [{"role": "system", "content": sysmsg}]
            for m in messages:
                content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"],
                                                                                        ensure_ascii=False)
                oa_messages.append({"role": m["role"], "content": content})
            body = {"model": name, "messages": oa_messages, "max_tokens": max_tokens,
                    "tools": [{"type": "function", "function": {"name": tool["name"],
                                                                "description": tool["description"],
                                                                "parameters": tool["input_schema"]}}],
                    "tool_choice": {"type": "function", "function": {"name": tool["name"]}}}
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = requests.post(p["base_url"].rstrip("/") + "/chat/completions", headers=headers, json=body,
                              timeout=timeout)
            if r.status_code >= 400:
                raise LlmUnavailable(f"{pid} : API {r.status_code} : {r.text[:300]}")
            j = r.json()
            try:
                call = j["choices"][0]["message"]["tool_calls"][0]["function"]
                data = json.loads(call["arguments"])
            except (KeyError, IndexError, TypeError, ValueError):
                raise LlmUnavailable(f"{pid} : réponse sans sortie structurée")
            u = j.get("usage", {})
            return data, None, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0
        except requests.RequestException as e:
            raise LlmUnavailable(f"{pid} : {type(e).__name__}")

    def test(self, model: str) -> dict:
        """Appel minimal (quelques tokens) pour vérifier un candidat « fournisseur/modèle »."""
        tool = {"name": "ping", "description": "Confirme la connexion.",
                "input_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}
        t0 = time.time()
        data, _, inp, out, cr = self._call(model, "Réponds uniquement via l'outil ping avec ok=true.", "",
                                           [{"role": "user", "content": "ping"}], tool, max_tokens=50)
        cost = self._cost(model, inp, out, cr)
        self._record("test", None, model, inp, out, cr, cost, "llm")
        return {"model": model, "ok": bool(data.get("ok")), "latency_s": round(time.time() - t0, 2),
                "tokens": inp + out}

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
            raise LlmUnavailable("LLM désactivé ou aucun fournisseur configuré")

        tool = {"name": tool_name, "description": f"Renvoie le résultat de la tâche {task} au format exigé.",
                "input_schema": schema}
        per_tier = self.cfg.get("escalation", {}).get("max_attempts_per_tier", 2)
        max_tokens = self._max_tokens(task)
        res = LlmResult({})
        last_errors: list[str] = []
        for tier in self._start_tiers(task, len(user) + len(system_dynamic)):
            cands = self.candidates(tier)
            if not cands:
                res.log.append(f"niveau {tier} : aucun fournisseur configuré")
                continue
            ci, attempts = 0, 0
            messages = [{"role": "user", "content": user}]
            while attempts < per_tier and ci < len(cands):
                model = cands[ci]
                self._check_budget(suite)
                try:
                    data, block, inp, out, cr = self._call(model, system_static, system_dynamic, messages, tool,
                                                           max_tokens=max_tokens)
                except LlmUnavailable as e:
                    # Panne ou quota : repli sur le candidat suivant du même niveau, conversation repartie de zéro
                    res.log.append(f"repli : {e}")
                    ci += 1
                    messages = [{"role": "user", "content": user}]
                    continue
                attempts += 1
                res.attempts += 1
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
            res.log.append(f"Escalade après le niveau {tier}")
        if not last_errors:
            raise LlmUnavailable("Aucun fournisseur n'a répondu : " + "; ".join(res.log[-3:]))
        raise LlmUnavailable("Validation impossible après escalade : " + "; ".join(last_errors[:5]))

    # ------------------------------------------------------------------ batch (régression nocturne)
    def submit_batch(self, items: list[dict]) -> str:
        """items : [{custom_id, task, system_static, user, schema, tool_name}] -> identifiant de batch."""
        reqs, pid = [], None
        for it in items:
            tier = self.cfg.get("routing", {}).get(it["task"], "medium")
            model = next((c for c in self.candidates(tier)
                          if self.cfg["providers"][c.split("/", 1)[0]]["type"] == "anthropic"), None)
            if not model:
                raise LlmUnavailable("Batch disponible uniquement avec un fournisseur anthropic configuré")
            pid, model = model.split("/", 1)
            reqs.append({"custom_id": it["custom_id"], "params": {
                "model": model, "max_tokens": self.cfg.get("max_output_tokens", 8000),
                "system": [{"type": "text", "text": it["system_static"], "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": guard(it["user"])}],
                "tools": [{"name": it["tool_name"], "description": "Résultat structuré",
                           "input_schema": it["schema"]}],
                "tool_choice": {"type": "tool", "name": it["tool_name"]}}})
        r = requests.post(self.cfg["providers"][pid].get("base_url", "https://api.anthropic.com").rstrip("/") +
                          "/v1/messages/batches",
                          headers={"x-api-key": self.provider_key(pid)[0], "anthropic-version": "2023-06-01"},
                          json={"requests": reqs}, timeout=60)
        r.raise_for_status()
        return f"{pid}/{r.json()['id']}"

    def batch_results(self, batch_id: str) -> dict | None:
        pid, batch_id = batch_id.split("/", 1)
        h = {"x-api-key": self.provider_key(pid)[0], "anthropic-version": "2023-06-01"}
        base = self.cfg["providers"][pid].get("base_url", "https://api.anthropic.com").rstrip("/")
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
