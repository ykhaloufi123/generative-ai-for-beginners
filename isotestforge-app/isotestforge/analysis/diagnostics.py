"""Base de diagnostics : code réponse + contexte -> cause probable (sans IA, enrichie par le testeur)."""
from __future__ import annotations

from pathlib import Path

import yaml


class DiagnosticsKB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rules: list[dict] = []
        if self.path.exists():
            self.rules = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []

    def match(self, ctx: dict) -> dict | None:
        for r in self.rules:
            ok = True
            for k, v in (r.get("when") or {}).items():
                if k == "message":
                    ok = str(v).lower() in str(ctx.get("message", "")).lower()
                else:
                    ok = str(ctx.get(k, "")) == str(v)
                if not ok:
                    break
            if ok:
                return {"id": r["id"], "cause": r["cause"], "action": r["action"], "source": "base"}
        return None

    def add(self, when: dict, cause: str, action: str) -> dict:
        """Ajout d'un diagnostic validé par le testeur (boucle d'apprentissage sans IA)."""
        rid = f"D{len(self.rules) + 1:03d}"
        rule = {"id": rid, "when": when, "cause": cause, "action": action}
        self.rules.insert(0, rule)   # les diagnostics validés sont prioritaires
        self.path.write_text(yaml.safe_dump(self.rules, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return rule
