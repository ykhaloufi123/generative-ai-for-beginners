"""Analyse des résultats (JTL JMeter ou runner natif) rapprochés du manifest de la suite."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .diagnostics import DiagnosticsKB

_LABEL = re.compile(r"^(?P<scn>[A-Za-z0-9_\-\.]+?)\.(?P<step>[A-Za-z0-9_\-]+) (?P<mti>\d{4})")


@dataclass
class StepOutcome:
    scenario: str
    step: str
    label: str
    success: bool
    got: str
    expected: list[str]
    message: str
    elapsed_ms: int
    diagnostic: dict | None = None


@dataclass
class ScenarioOutcome:
    id: str
    title: str
    requirements: list[str]
    tags: list[str]
    status: str                     # passed | failed | not_run
    steps: list[StepOutcome] = field(default_factory=list)


@dataclass
class RunAnalysis:
    suite: str
    scenarios: list[ScenarioOutcome]
    by_requirement: dict[str, str]
    totals: dict
    infra: list[StepOutcome]

    def to_dict(self) -> dict:
        return {"suite": self.suite, "totals": self.totals, "by_requirement": self.by_requirement,
                "scenarios": [{"id": s.id, "title": s.title, "requirements": s.requirements, "status": s.status,
                               "tags": s.tags, "steps": [st.__dict__ for st in s.steps]} for s in self.scenarios],
                "infra": [st.__dict__ for st in self.infra]}


def read_jtl(path: str | Path) -> list[dict]:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("<"):
        from lxml import etree
        root = etree.fromstring(text.encode())
        rows = []
        for s in root:
            rows.append({"label": s.get("lb"), "success": s.get("s"), "responseCode": s.get("rc"),
                         "responseMessage": s.get("rm"), "elapsed": s.get("t"), "timeStamp": s.get("ts"),
                         "failureMessage": " ; ".join((a.findtext("failureMessage") or "") for a in
                                                     s.findall("assertionResult"))})
        return rows
    return list(csv.DictReader(text.splitlines()))


def analyse(jtl_rows: list[dict], manifest: dict, kb: DiagnosticsKB | None = None,
            llm_diagnoser=None) -> RunAnalysis:
    scen_meta = {s["id"]: s for s in manifest.get("scenarios", [])}
    step_meta = {(s["id"], st["id"]): (s, st) for s in manifest.get("scenarios", []) for st in s.get("steps", [])}
    outcomes: dict[str, ScenarioOutcome] = {
        sid: ScenarioOutcome(sid, m["title"], m.get("requirements", []), m.get("tags", []), "not_run")
        for sid, m in scen_meta.items()}
    infra: list[StepOutcome] = []
    for row in jtl_rows:
        label = row.get("label") or ""
        m = _LABEL.match(label)
        if not m:
            continue   # Transaction Controllers, échantillons techniques
        sid, stid = m.group("scn"), m.group("step")
        meta = step_meta.get((sid, stid))
        expected = []
        if meta:
            expected = meta[1].get("expect", {}).get("39", []) or (["TIMEOUT"] if meta[1].get("expects_timeout")
                                                                    else [])
        success = str(row.get("success", "")).lower() == "true"
        got = row.get("responseCode") or ""
        msg = row.get("failureMessage") or row.get("responseMessage") or ""
        so = StepOutcome(sid, stid, label, success, got, expected, msg, int(float(row.get("elapsed") or 0)))
        if not success and kb:
            ctx = {"got": got, "expected": expected[0] if expected else "", "mti": m.group("mti"),
                   "mutation": scen_meta.get(sid, {}).get("mutation") or "", "message": msg,
                   "template": ""}
            so.diagnostic = kb.match(ctx)
            if so.diagnostic is None and llm_diagnoser:
                so.diagnostic = llm_diagnoser(ctx)
        if sid in outcomes:
            outcomes[sid].steps.append(so)
        else:
            infra.append(so)
    for o in outcomes.values():
        if o.steps:
            o.status = "passed" if all(s.success for s in o.steps) else "failed"
    statuses: dict[str, set[str]] = {}
    for o in outcomes.values():
        for r in o.requirements:
            statuses.setdefault(r, set()).add(o.status)
    by_req: dict[str, str] = {}
    for r, st in statuses.items():
        if "failed" in st:
            by_req[r] = "failed"
        elif st == {"passed"}:
            by_req[r] = "passed"
        elif st == {"not_run"}:
            by_req[r] = "not_run"
        else:
            by_req[r] = "partial"
    vals = list(outcomes.values())
    totals = {"scenarios": len(vals), "passed": sum(o.status == "passed" for o in vals),
              "failed": sum(o.status == "failed" for o in vals), "not_run": sum(o.status == "not_run" for o in vals),
              "steps": sum(len(o.steps) for o in vals), "infra_failures": sum(not s.success for s in infra),
              "avg_ms": round(sum(s.elapsed_ms for o in vals for s in o.steps) /
                              max(1, sum(len(o.steps) for o in vals)), 1)}
    totals["pass_rate"] = round(100 * totals["passed"] / max(1, totals["passed"] + totals["failed"]), 1)
    return RunAnalysis(manifest.get("suite", ""), vals, by_req, totals, infra)


def load_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
