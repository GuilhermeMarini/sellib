"""A vendor `<Private>` block must not be read as if it held devices.

DIGSI's SCD export nests an `<IED uuidRef=... name=...>` cross-reference
inside `<Private><FolderDetails><FolderInfo>` for every device in the station,
ahead of the real `<IED>` in document order. The reference file carries 28
elements named `IED` for 14 devices.

`_ieds_from_root` keeps the FIRST occurrence of a repeated name, so a
descendant search resolved all 14 to the decoy: `relay_type`, `manufacturer`,
`description` and `config_version` all `None`, on every IED of that station.
The IP survived only because it comes from `<Communication>`, by a different
path -- which is what made the failure quiet rather than obvious.

A `Private` may legally contain any content at all, so this is not specific to
one vendor: it is why an element the schema allows in exactly one place must
be matched there and not by descendant name.
"""

from sellib.scl.read import ScdDocument

_DECOY_FIRST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<SCL xmlns="http://www.iec.ch/61850/2003/SCL">\n'
    '  <Private type="Siemens-SiedigFolderDetails">\n'
    '    <FolderDetails><FolderInfo>\n'
    '      <IED uuidRef="1f741f02" name="REL_A"/>\n'
    '    </FolderInfo></FolderDetails>\n'
    '  </Private>\n'
    '  <Communication><SubNetwork name="ST1">\n'
    '    <ConnectedAP iedName="REL_A" apName="S1">\n'
    '      <Address><P type="IP">192.0.2.60</P></Address>\n'
    '    </ConnectedAP>\n'
    '  </SubNetwork></Communication>\n'
    '  <IED name="REL_A" type="SEL_487E" manufacturer="SEL" desc="bay 1"\n'
    '       configVersion="001"/>\n'
    '</SCL>\n'
)


def _doc(tmp_path, text):
    path = tmp_path / "station.scd"
    path.write_text(text, encoding="utf-8")
    return ScdDocument.parse(path)


def test_a_private_ied_lookalike_does_not_shadow_the_real_one(tmp_path):
    ieds = _doc(tmp_path, _DECOY_FIRST).ieds()
    assert len(ieds) == 1
    ied = ieds[0]
    assert ied.name == "REL_A"
    assert ied.relay_type == "SEL_487E"
    assert ied.manufacturer == "SEL"
    assert ied.description == "bay 1"
    assert ied.config_version == "001"


def test_the_ip_still_resolves(tmp_path):
    """The decoy carries no address, and the real IED's IP comes from
    `<Communication>`. Reading the decoy left this field correct while every
    field beside it was `None` -- the shape that made the bug quiet."""
    assert _doc(tmp_path, _DECOY_FIRST).ieds()[0].ip == "192.0.2.60"


def test_an_ied_nested_anywhere_but_under_scl_is_not_a_device(tmp_path):
    """No `<IED>` as a direct child means no IEDs, however many the private
    blocks mention."""
    text = _DECOY_FIRST.replace(
        '  <IED name="REL_A" type="SEL_487E" manufacturer="SEL" desc="bay 1"\n'
        '       configVersion="001"/>\n', "")
    assert _doc(tmp_path, text).ieds() == []


def test_a_document_declaring_no_namespace_still_reads(tmp_path):
    """Hand-made SCDs that declare no namespace are real; the direct-child
    match must not depend on the namespace being present."""
    ieds = _doc(tmp_path, _DECOY_FIRST.replace(
        ' xmlns="http://www.iec.ch/61850/2003/SCL"', "")).ieds()
    assert [i.relay_type for i in ieds] == ["SEL_487E"]
