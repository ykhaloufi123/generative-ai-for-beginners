import pytest

from isotestforge.iso import tlv
from isotestforge.iso.channel import ChannelSpec
from isotestforge.iso.message import IsoMessage
from isotestforge.iso.packager import GenericPackager, PackagerError


def _msg():
    m = IsoMessage("0200")
    for k, v in {2: "4761730000000011", 3: "000000", 4: "15000", 11: "000123", 22: "051",
                 35: "4761730000000011=30122010000000", 41: "TPE00001", 43: "SHOP CASABLANCA",
                 52: "0123456789ABCDEF", 90: "0" * 42, 128: "AB" * 8}.items():
        m.set(k, v)
    m.set("55.1", "000000015000", "9F02")
    m.set("55.2", "0011", "9F36")
    return m


@pytest.mark.parametrize("name", ["iso87ascii", "iso87binary"])
def test_pack_unpack_roundtrip(ref, name):
    pk = GenericPackager.load(ref.base_dir / f"data/packagers/{name}.xml")
    m = _msg()
    u = pk.unpack(pk.pack(m))
    assert u.mti == "0200"
    assert u.fields[4] == "000000015000"
    assert u.fields[43] == "SHOP CASABLANCA"
    assert u.fields[128] == "AB" * 8
    assert u.tag(55, "9F36") == "0011"
    assert 90 in u.fields


def test_format_checks(ref):
    pk = ref.packager("SWITCH_SIM")
    assert pk.check_value(4, "12AB") is not None
    assert pk.check_value(52, "XYZ") is not None
    assert pk.check_value(2, "${PAN}") is None
    with pytest.raises(PackagerError):
        pk.pack(IsoMessage("0200", {200: "x"}))


def test_tlv_roundtrip_multibyte_tags():
    items = [("9F02", "000000001000"), ("82", "5C00"), ("9F10", "06010A03A00000")]
    assert tlv.decode(tlv.encode(items)) == items


def test_ascii_channel_framing():
    ch = ChannelSpec.from_config("ASCIIChannel", "HDR")
    f = ch.frame(b"0800")
    assert f == b"0007HDR0800"
