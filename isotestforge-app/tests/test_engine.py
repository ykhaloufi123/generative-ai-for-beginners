from lxml import etree

from isotestforge.dsl import loader
from isotestforge.engine.binder import Binder
from isotestforge.engine.expand import expand_suite
from isotestforge.engine.jmx_builder import write_outputs
from isotestforge.engine.pairwise import full_product_size, pairwise
from isotestforge.engine.validator import validate_bound, validate_jmx

PKG = "nz.co.breakpoint.jmeter.iso8583"


def test_pairwise_covers_all_pairs():
    dims = {"a": [1, 2, 3], "b": ["x", "y", "z"], "c": [True, False], "d": list(range(4))}
    rows = pairwise(dims)
    assert len(rows) < full_product_size(dims)
    names = list(dims)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            seen = {(r[names[i]], r[names[j]]) for r in rows}
            assert len(seen) == len(dims[names[i]]) * len(dims[names[j]])


def _bound(ref, workspace, name="acquiring_pos.yaml"):
    suite = loader.load(workspace / "examples/suites" / name)
    scns, rep = expand_suite(suite, ref)
    return suite, Binder(ref, suite).bind(scns), rep


def test_business_rules(ref, workspace):
    _, b, rep = _bound(ref, workspace)
    by = {s.id: s for s in b.scenarios}
    auth = by["POS-EMV-001"].steps[0]
    assert auth.field_value("22") == "051" and auth.field_value("52")          # EMV + PIN online
    assert auth.crypto.pin and auth.crypto.mac and auth.crypto.arqc
    assert any(f.tag == "9F10" for f in auth.fields)
    rev = by["POS-EMV-001"].steps[1]
    assert rev.mti == "0400" and "${V_POS_EMV_001_auth_11}" in rev.field_value("90")
    assert not by["POS-CTLS-001"].steps[0].field_value("52")                  # sous le plafond : pas de PIN
    assert by["POS-CTLS-002"].steps[0].field_value("52")                      # au-delà : PIN exigé
    assert by["POS-NEG-PIN"].steps[0].expect["39"] == ["55"]
    assert by["POS-NEG-MAC"].steps[0].crypto.mac_key_var == "BADKEY"
    assert by["POS-NEG-TIMEOUT"].steps[0].expects_timeout
    assert rep.pairwise_saved > 0


def test_validation_and_jmx_structure(ref, workspace, tmp_path):
    suite, b, _ = _bound(ref, workspace)
    assert validate_bound(b, ref).ok
    files = write_outputs(suite, ref, b, tmp_path / "out")
    assert validate_jmx(files["jmx"]).ok
    root = etree.parse(files["jmx"]).getroot()
    samplers = root.findall(f".//{PKG}.ISO8583Sampler")
    assert len(samplers) == sum(len(s.steps) for s in b.scenarios) + 2      # + sign-on / sign-off
    assert root.find(f".//{PKG}.ISO8583Config") is not None
    text = open(files["jmx"], encoding="utf-8").read()
    for k in ("ZPK", "TAK", "IMKAC"):
        assert ref.key("KS_TEST_01", {"ZPK": "zpk", "TAK": "tak", "IMKAC": "imk_ac"}[k]) not in text
    assert "ZPK_KS_TEST_01=" in open(files["keys"]).read()
    assert validate_jmx(files["load_jmx"]).ok


def test_atm_pack(ref, workspace):
    _, b, _ = _bound(ref, workspace, "atm_pack.yaml")
    assert validate_bound(b, ref).ok
    ids = {s.id for s in b.scenarios}
    assert "ATM-NEG-03" in ids and "ATM-004-01" in ids


def test_validator_catches_errors(ref, workspace):
    text = (workspace / "examples/suites/acquiring_pos.yaml").read_text()
    text = text.replace("- {id: auth, template: PURCHASE, amount: 75.50}",
                        "- {id: auth, template: PURCHASE, amount: 75.50, fields: {\"4\": \"ABC\"}}")
    suite = loader.parse(text)
    scns, _ = expand_suite(suite, ref)
    rep = validate_bound(Binder(ref, suite).bind(scns), ref)
    assert not rep.ok and any("DE4" in i.message for i in rep.issues)
