"""Vecteurs de référence publics et propriétés aller-retour."""
from isotestforge.crypto import card, emv, keys, mac, pin

KEY = "0123456789ABCDEFFEDCBA9876543210"


def test_kcv_reference_vector():
    assert keys.kcv(KEY) == "08D7B4"


def test_cvv_reference_vector():
    # Vecteur classique : PAN 4123456789012345, exp 8701, SC 101 -> 561
    assert card.cvv(KEY, "4123456789012345", "8701", "101") == "561"


def test_luhn():
    assert card.luhn_valid("4111111111111111")
    assert not card.luhn_valid("4111111111111112")
    assert card.luhn_digit("476173000000001") == "1"


def test_pinblock_iso0_and_roundtrip():
    pb = pin.clear_block("1234", "4111111111111111")
    assert pb == "041225EEEEEEEEEE"
    enc = pin.encrypt(pb, KEY)
    assert pin.verify(enc, KEY, "4111111111111111", "1234")
    assert not pin.verify(enc, KEY, "4111111111111111", "9999")


def test_pinblock_iso3_extract():
    pb = pin.clear_block("56789", "4761730000000011", "iso3")
    assert pin.extract_pin(pb, "4761730000000011") == "56789"


def test_retail_mac_is_deterministic_and_key_sensitive():
    m1 = mac.generate(KEY, b"message")
    assert m1 == mac.generate(KEY, b"message") and len(m1) == 16
    assert m1 != mac.generate("FEDCBA98765432100123456789ABCDEF", b"message")


def test_arqc_cvn10_and_cvn18_verify():
    icc = {"9F02": "000000001000", "9F03": "000000000000", "9F1A": "0504", "95": "0000000000",
           "5F2A": "0504", "9A": "261030", "9C": "00", "9F37": "12345678", "82": "5C00", "9F36": "0001"}
    for iad in ("06010A03A00000", "06011203A00000"):
        d = dict(icc, **{"9F10": iad})
        d["9F26"] = emv.compute_arqc(KEY, "4761730000000011", "01", d)
        assert emv.verify_arqc(KEY, "4761730000000011", "01", d)
        d["9F36"] = "0002"
        assert not emv.verify_arqc(KEY, "4761730000000011", "01", d)


def test_key_under_kek_roundtrip():
    zpk = keys.generate()
    assert keys.decrypt_under(keys.encrypt_under(zpk, KEY), KEY) == zpk
