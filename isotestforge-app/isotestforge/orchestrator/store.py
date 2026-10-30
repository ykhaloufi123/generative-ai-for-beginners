"""Persistance (SQLite) : exigences, workflows, bibliothèque de scénarios, corrections, historique des runs."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import yaml

from ..dsl.models import Scenario
from ..llm.cache import cosine, embed


class Store:
    def __init__(self, path: str | Path = "data/itf.sqlite"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS requirements (id TEXT PRIMARY KEY, text TEXT, source TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, name TEXT, host TEXT, status TEXT,
            created REAL, updated REAL, data TEXT);
        CREATE TABLE IF NOT EXISTS library (id TEXT PRIMARY KEY, summary TEXT, operation TEXT, channel TEXT,
            scenario TEXT, vec TEXT, created REAL, uses INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS corrections (id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT,
            generated TEXT, corrected TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, workflow TEXT, suite TEXT, mode TEXT, created REAL,
            pass_rate REAL, totals TEXT, analysis TEXT);
        CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, kind TEXT, content TEXT, status TEXT, created REAL);
        """)
        self.db.commit()

    # ---------------------------------------------------------------- exigences
    def upsert_requirements(self, reqs: list[dict]) -> None:
        for r in reqs:
            self.db.execute("INSERT OR REPLACE INTO requirements VALUES (?,?,?,?)",
                            (r["id"], r["text"], r.get("source", ""), time.time()))
        self.db.commit()

    def requirements(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, text, source FROM requirements ORDER BY id")]

    # ---------------------------------------------------------------- workflows
    def create_workflow(self, name: str, host: str, data: dict) -> str:
        wid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = time.time()
        self.db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?,?)",
                        (wid, name, host, "created", now, now, json.dumps(data, ensure_ascii=False)))
        self.db.commit()
        return wid

    def get_workflow(self, wid: str) -> dict:
        r = self.db.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
        if not r:
            raise KeyError(f"Workflow inconnu : {wid}")
        d = dict(r)
        d["data"] = json.loads(d["data"])
        return d

    def update_workflow(self, wid: str, status: str | None = None, **data) -> dict:
        wf = self.get_workflow(wid)
        wf["data"].update(data)
        self.db.execute("UPDATE workflows SET status=COALESCE(?,status), updated=?, data=? WHERE id=?",
                        (status, time.time(), json.dumps(wf["data"], ensure_ascii=False), wid))
        self.db.commit()
        return self.get_workflow(wid)

    def workflows(self, limit: int = 50) -> list[dict]:
        return [{k: r[k] for k in ("id", "name", "host", "status", "created", "updated")}
                for r in self.db.execute("SELECT * FROM workflows ORDER BY created DESC LIMIT ?", (limit,))]

    # ---------------------------------------------------------------- bibliothèque (RAG)
    def add_to_library(self, scn: Scenario, summary: str, operation: str = "", channel: str = "") -> None:
        key = f"{operation}|{channel}|{summary}"
        self.db.execute("INSERT OR REPLACE INTO library VALUES (?,?,?,?,?,?,?,0)",
                        (scn.id, summary, operation, channel, scn.model_dump_json(), json.dumps(embed(key)),
                         time.time()))
        self.db.commit()

    def library_search(self, query: str, k: int = 3, threshold: float = 0.0) -> list[tuple[float, Scenario]]:
        q = embed(query)
        scored = []
        for r in self.db.execute("SELECT scenario, vec FROM library"):
            s = cosine(q, json.loads(r["vec"]))
            if s >= threshold:
                scored.append((s, Scenario.model_validate_json(r["scenario"])))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]

    def library_list(self) -> list[dict]:
        return [{"id": r["id"], "summary": r["summary"], "operation": r["operation"], "channel": r["channel"],
                 "uses": r["uses"]} for r in self.db.execute("SELECT * FROM library ORDER BY created DESC")]

    # ---------------------------------------------------------------- apprentissage
    def add_correction(self, scenario_id: str, generated: dict, corrected: dict) -> None:
        self.db.execute("INSERT INTO corrections (scenario_id, generated, corrected, created) VALUES (?,?,?,?)",
                        (scenario_id, json.dumps(generated, ensure_ascii=False),
                         json.dumps(corrected, ensure_ascii=False), time.time()))
        self.db.commit()

    def corrections(self, limit: int = 5) -> list[dict]:
        return [{"scenario_id": r["scenario_id"], "generated": json.loads(r["generated"]),
                 "corrected": json.loads(r["corrected"])}
                for r in self.db.execute("SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,))]

    # ---------------------------------------------------------------- runs
    def add_run(self, workflow: str, suite: str, mode: str, analysis: dict) -> str:
        rid = uuid.uuid4().hex[:10]
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                        (rid, workflow, suite, mode, time.time(), analysis["totals"]["pass_rate"],
                         json.dumps(analysis["totals"]), json.dumps(analysis, ensure_ascii=False)))
        self.db.commit()
        return rid

    def history(self, suite: str | None = None, limit: int = 20) -> list[dict]:
        q = "SELECT id, suite, mode, created, pass_rate, totals FROM runs"
        args: tuple = ()
        if suite:
            q += " WHERE suite=?"
            args = (suite,)
        rows = self.db.execute(q + " ORDER BY created DESC LIMIT ?", args + (limit,)).fetchall()
        return [{"id": r["id"], "suite": r["suite"], "mode": r["mode"], "created": r["created"],
                 "pass_rate": r["pass_rate"], "totals": json.loads(r["totals"]),
                 "label": time.strftime("%d/%m %H:%M", time.localtime(r["created"]))} for r in reversed(rows)]

    def last_analyses(self) -> list[dict]:
        rows = self.db.execute("SELECT suite, analysis FROM runs ORDER BY created DESC").fetchall()
        seen, out = set(), []
        for r in rows:
            if r["suite"] not in seen:
                seen.add(r["suite"])
                out.append(json.loads(r["analysis"]))
        return out

    # ---------------------------------------------------------------- propositions de l'Outilleur
    def add_proposal(self, kind: str, content: dict) -> str:
        pid = uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO proposals VALUES (?,?,?,?,?)",
                        (pid, kind, yaml.safe_dump(content, allow_unicode=True, sort_keys=False), "pending",
                         time.time()))
        self.db.commit()
        return pid

    def proposals(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM proposals ORDER BY created DESC")]


class Library:
    """Adaptateur bibliothèque pour le Concepteur (réutilisation avant tout appel LLM)."""

    def __init__(self, store: Store, threshold: float = 0.9):
        self.store, self.threshold = store, threshold

    def find(self, summary: str, operation: str, channel: str) -> Scenario | None:
        res = self.store.library_search(f"{operation}|{channel}|{summary}", k=1, threshold=self.threshold)
        return res[0][1] if res else None

    def examples(self, intents, k: int = 3) -> list[str]:
        q = " ".join(i.summary for i in intents)
        ex = [yaml.safe_dump(s.model_dump(exclude_defaults=True), allow_unicode=True, sort_keys=False)
              for _, s in self.store.library_search(q, k=k)]
        for c in self.store.corrections(limit=2):
            ex.append("Correction validée par un testeur (à imiter) :\n" +
                      yaml.safe_dump(c["corrected"], allow_unicode=True, sort_keys=False))
        return ex
