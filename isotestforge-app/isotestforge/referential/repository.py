"""Chargement et persistance du référentiel (fichiers YAML versionnables dans git)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from ..crypto import card as cardlib
from ..iso.packager import GenericPackager
from .models import BinRange, CardProfile, HostSpec, KeySet, MessageTemplate, TerminalProfile


class ReferentialError(Exception):
    pass


def resolve_secret(value: str | None) -> str | None:
    """Résout 'env:NOM' et 'vault:chemin#cle' (Vault KV v2, VAULT_ADDR + VAULT_TOKEN)."""
    if not value:
        return value
    if value.startswith("env:"):
        return os.environ.get(value[4:])
    if value.startswith("vault:"):
        import requests
        path, _, key = value[6:].partition("#")
        addr, token = os.environ.get("VAULT_ADDR"), os.environ.get("VAULT_TOKEN")
        if not (addr and token):
            raise ReferentialError("VAULT_ADDR / VAULT_TOKEN non définis")
        r = requests.get(f"{addr}/v1/{path}", headers={"X-Vault-Token": token}, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        data = data.get("data", data)
        return data[key]
    return value


class Referential:
    FILES = {"cards": "cards.yaml", "terminals": "terminals.yaml", "keysets": "keysets.yaml",
             "hosts": "hosts.yaml", "templates": "templates.yaml", "bins": "bin_whitelist.yaml"}

    def __init__(self, root: str | Path, base_dir: str | Path | None = None):
        self.root = Path(root)
        self.base_dir = Path(base_dir) if base_dir else self.root.parent.parent
        self.cards: dict[str, CardProfile] = {}
        self.terminals: dict[str, TerminalProfile] = {}
        self.keysets: dict[str, KeySet] = {}
        self.hosts: dict[str, HostSpec] = {}
        self.templates: dict[str, MessageTemplate] = {}
        self.bins: list[BinRange] = []
        self._packagers: dict[str, GenericPackager] = {}
        self.reload()

    # ------------------------------------------------------------------
    def _load(self, name: str) -> list[dict]:
        p = self.root / self.FILES[name]
        if not p.exists():
            return []
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        return data if isinstance(data, list) else data.get(name, [])

    def reload(self) -> None:
        self.bins = [BinRange(**b) for b in self._load("bins")]
        self.cards = {}
        for c in self._load("cards"):
            cp = CardProfile(**c)
            self.check_test_pan(cp.pan)
            self.cards[cp.id] = cp
        self.terminals = {t["id"]: TerminalProfile(**t) for t in self._load("terminals")}
        self.keysets = {k["id"]: KeySet(**k) for k in self._load("keysets")}
        self.hosts = {h["id"]: HostSpec(**h) for h in self._load("hosts")}
        self.templates = {t["id"]: MessageTemplate(**t) for t in self._load("templates")}
        self._packagers.clear()

    def check_test_pan(self, pan: str) -> None:
        """Garde-fou PCI : seuls les PAN appartenant à une plage de test déclarée sont acceptés."""
        if self.bins and not any(pan.startswith(b.prefix) for b in self.bins):
            raise ReferentialError(
                f"PAN {cardlib.mask_pan(pan)} hors des plages de test autorisées (bin_whitelist.yaml)")

    # ------------------------------------------------------------------
    def card(self, cid: str) -> CardProfile:
        if cid not in self.cards:
            raise ReferentialError(f"Profil carte inconnu : {cid}")
        return self.cards[cid]

    def terminal(self, tid: str) -> TerminalProfile:
        if tid not in self.terminals:
            raise ReferentialError(f"Profil terminal inconnu : {tid}")
        return self.terminals[tid]

    def keyset(self, kid: str) -> KeySet:
        if kid not in self.keysets:
            raise ReferentialError(f"Jeu de clés inconnu : {kid}")
        return self.keysets[kid]

    def host(self, hid: str) -> HostSpec:
        if hid not in self.hosts:
            raise ReferentialError(f"Spec hôte inconnue : {hid}")
        return self.hosts[hid]

    def template(self, tid: str) -> MessageTemplate:
        if tid not in self.templates:
            raise ReferentialError(f"Template inconnu : {tid}")
        return self.templates[tid]

    def key(self, keyset_id: str, name: str) -> str | None:
        return resolve_secret(getattr(self.keyset(keyset_id), name))

    def packager(self, host_id: str) -> GenericPackager:
        h = self.host(host_id)
        if h.packager not in self._packagers:
            p = Path(h.packager)
            if not p.is_absolute():
                p = self.base_dir / p
            self._packagers[h.packager] = GenericPackager.load(p)
        return self._packagers[h.packager]

    def packager_path(self, host_id: str) -> Path:
        p = Path(self.host(host_id).packager)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def cards_by_tag(self, *tags: str) -> list[CardProfile]:
        return [c for c in self.cards.values() if all(t in c.tags or t == c.card_type or t == c.scheme.lower()
                                                      or t == c.status for t in tags)]

    def version(self) -> str:
        h = hashlib.sha256()
        for f in sorted(self.FILES.values()):
            p = self.root / f
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()[:12]

    # ------------------------------------------------------------------ écriture (UI / outils)
    def save(self, name: str, items: list[dict]) -> None:
        p = self.root / self.FILES[name]
        p.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.reload()

    def add_card(self, card: CardProfile) -> None:
        self.check_test_pan(card.pan)
        items = [c.model_dump(exclude_defaults=False) for c in self.cards.values() if c.id != card.id]
        items.append(card.model_dump())
        self.save("cards", items)

    def summary(self) -> dict:
        return {
            "version": self.version(),
            "cards": [{"id": c.id, "scheme": c.scheme, "type": c.card_type, "pan": cardlib.mask_pan(c.pan),
                       "status": c.status, "cvm": c.cvm, "tags": c.tags} for c in self.cards.values()],
            "terminals": [{"id": t.id, "tid": t.tid, "kind": t.terminal_kind, "mcc": t.mcc}
                          for t in self.terminals.values()],
            "keysets": [{"id": k.id, "kcv": k.kcvs(resolve_secret)} for k in self.keysets.values()],
            "hosts": [{"id": h.id, "host": h.host, "port": h.port, "channel": h.channel.kind,
                       "spec_version": h.spec_version} for h in self.hosts.values()],
            "templates": [{"id": t.id, "mti": t.mti, "description": t.description}
                          for t in self.templates.values()],
        }
