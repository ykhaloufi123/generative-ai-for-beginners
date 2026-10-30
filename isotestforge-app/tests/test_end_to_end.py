"""Bout en bout : besoins -> intentions -> DSL -> JMX -> exécution sur simulateur -> rapport."""
import json

from isotestforge.dsl import loader
from isotestforge.engine.binder import Binder, keys_properties
from isotestforge.engine.expand import expand_suite
from isotestforge.orchestrator.pipeline import Pipeline
from isotestforge.runner.native import NativeRunner
from isotestforge.tools import decoder, spec_diff, trace_import
from isotestforge.tools.requirements_import import from_file


def test_example_suite_against_simulator(ref, workspace, simulator):
    sim, port = simulator
    suite = loader.load(workspace / "examples/suites/acquiring_pos.yaml")
    scns, _ = expand_suite(suite, ref)
    b = Binder(ref, suite).bind(scns)
    res = NativeRunner(b, ref.packager("SWITCH_SIM"), keys_properties(ref, b.keysets), "127.0.0.1", port).run()
    failed = [(r.label, r.message) for r in res if not r.success]
    assert not failed, failed


def test_atm_pack_against_simulator(ref, workspace, simulator):
    sim, port = simulator
    suite = loader.load(workspace / "examples/suites/atm_pack.yaml")
    scns, _ = expand_suite(suite, ref)
    b = Binder(ref, suite).bind(scns)
    res = NativeRunner(b, ref.packager("SWITCH_SIM"), keys_properties(ref, b.keysets), "127.0.0.1", port).run()
    failed = [(r.label, r.message) for r in res if not r.success]
    assert not failed, failed


def test_pipeline_without_llm(workspace, simulator):
    sim, port = simulator
    p = Pipeline(workspace, use_llm=False)
    reqs = from_file(workspace / "examples/requirements/acquisition_tpe.md")
    assert [r["id"] for r in reqs][0] == "REQ-100"
    wf = p.run_all("TPE", "SWITCH_SIM", reqs, "native", "127.0.0.1", port)
    assert wf["status"] == "done", wf["data"].get("error")
    t = wf["data"]["analysis"]["totals"]
    assert t["failed"] == 0 and t["passed"] >= 9
    assert p.cost_summary(wf["id"])["share_without_llm"] == 100.0
    assert (workspace / "data/workflows" / wf["id"] / "report.html").exists()
    assert (workspace / "data/workflows" / wf["id"] / "report.xlsx").exists()
    req_status = wf["data"]["analysis"]["by_requirement"]
    assert req_status["REQ-101"] == "passed"


def test_human_review_and_learning(workspace):
    p = Pipeline(workspace, use_llm=False)
    wid = p.start("REVIEW", "SWITCH_SIM", [{"id": "REQ-1", "text": "Achat EMV Visa avec PIN online."}])
    wf = p.analyse(wid)
    assert wf["status"] == "intents_review"
    wf = p.approve_intents(wid)
    assert wf["status"] == "dsl_review"
    dsl = wf["data"]["dsl"].replace("template: PURCHASE", "template: PURCHASE\n    amount: 42.0")
    wf = p.approve_dsl(wid, dsl)
    assert wf["status"] == "compiled"
    assert p.store.corrections()        # la correction humaine est mémorisée
    assert p.store.library_list()       # le scénario validé enrichit la bibliothèque


def test_decoder_traces_and_spec_diff(ref, workspace, tmp_path, simulator):
    sim, port = simulator
    suite = loader.load(workspace / "examples/suites/acquiring_pos.yaml")
    scns, _ = expand_suite(suite, ref)
    b = Binder(ref, suite).bind(scns[:2])
    runner = NativeRunner(b, ref.packager("SWITCH_SIM"), keys_properties(ref, b.keysets), "127.0.0.1", port)
    msg = runner.build_message(b.scenarios[0].steps[0])
    raw = ref.packager("SWITCH_SIM").pack(msg)
    d = decoder.decode(raw.decode("latin-1"), ref.packager("SWITCH_SIM"))
    assert d["mti"] == "0200" and "*" in [r for r in d["fields"] if r["de"] == "2"][0]["value"]
    d2 = decoder.decode(raw.hex(), ref.packager("SWITCH_SIM"))
    assert d2["mti"] == "0200"

    trace = tmp_path / "t.jsonl"
    res = runner.run()
    trace.write_text("\n".join(json.dumps({"request": {"mti": r.request["mti"], "fields": {
        **r.request["fields"], "2": "4111111111111111"}}, "response": r.response}) for r in res
        if r.request.get("mti", "").startswith(("02", "04"))))
    imported, stats = trace_import.import_traces(trace, ref, "SWITCH_SIM")
    assert stats["scenarios"] >= 1
    assert "4111111111111111" not in loader.dump(imported)        # aucun PAN réel conservé

    pk_old = ref.packager("SWITCH_SIM")
    import copy
    pk_new = copy.deepcopy(pk_old)
    pk_new.fields[43].length = 99
    ch = spec_diff.diff_packagers(pk_old, pk_new)
    assert ch and ch[0]["de"] == 43
