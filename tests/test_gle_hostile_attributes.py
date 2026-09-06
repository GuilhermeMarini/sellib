"""What the renderer does with a GLE whose attributes are not what QuickSet writes.

A GLE arrives inside an RDB somebody handed the engineer -- an integrator, a
panel builder, the client's own archive. It is exactly as trusted as an SCD,
which is to say not at all, and `render_page` turns it into markup that goes
straight into the page the browser draws.

Two holes this pins, both in the same function and both found by reading the
escaping pass that fixed the four attributes NEXT to them:

* `port_operator/@operator_type` reaches `data-sink-mod` and `data-src-mod`
  raw. Every OTHER consumer compares it against the literals "NOT", "RTRIG"
  and "FTRIG" (`_input_pins_svg`, `_output_pins_svg`, `render_symbol`), so
  those two attributes are its only path into the output;
* `port/@index` goes through a bare `int()`, which raises `ValueError` out of
  `element_info` and takes the whole page down rather than one bad port.

Both are characterization tests in the sense the house style means: each one
names the production change that makes it fail, because a test written after
the fix would otherwise prove nothing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from sellib.gle import element_info, render_page

#: The page, with the two attributes under test left at their harmless values.
#: They are set through the ElementTree API afterwards and never interpolated
#: into this string -- a payload containing a quote would otherwise break the
#: FIXTURE, `operator_type` would come back empty, and the test would pass
#: against a renderer that escapes nothing.
_PAGE_XML = """
<page name="P">
  <element id="1" type="AND" left="10" top="10">
    <logic_element physical_instance_name="A" alias="">
      <ports><port index="0"/></ports>
      <ports><port index="0"/></ports>
    </logic_element>
  </element>
  <element id="2" type="AND" left="100" top="10">
    <logic_element physical_instance_name="B" alias="">
      <ports>
        <port index="0"><port_operator operator_type="NOT"/></port>
      </ports>
      <ports><port index="0"/></ports>
    </logic_element>
  </element>
  <connection>
    <source_port element_id="1" port_number="0"/>
    <sink_port element_id="2" port_number="0"/>
    <point x="20" y="20"/>
    <point x="100" y="20"/>
  </connection>
</page>
"""


def _page(operator_type: str = "NOT", port_index: str = "0") -> ET.Element:
    """Two AND blocks and the wire between them, with one port modifier.

    Deliberately the smallest thing `render_page` will draw: the attributes
    under test are the only interesting bytes in it.
    """
    page = ET.fromstring(_PAGE_XML)
    port = page.find(".//element[@id='2']//port")
    port.set("index", port_index)
    port.find("port_operator").set("operator_type", operator_type)
    return page


def test_a_port_operator_cannot_break_out_of_the_attribute_it_sits_in():
    """`operator_type` is written into `data-sink-mod` with no escaping.

    Fails if `render_connection` stops escaping `sink_mod`/`src_mod`: the
    payload closes the attribute and opens an event handler, and the assertion
    below finds the bare `onload=` sitting outside any quoted value.

    The check is that the SVG still PARSES and the value survives as text --
    an escaped payload is inert markup, an unescaped one is a new attribute.
    """
    payload = '" onload="alert(1)'
    svg = render_page(_page(operator_type=payload))

    # If the quote survived raw, this is no longer well-formed XML with the
    # payload as a value -- it is an `onload` attribute of its own.
    root = ET.fromstring(svg)
    poly = root.find(".//{http://www.w3.org/2000/svg}polyline")
    assert poly is not None
    assert poly.get("onload") is None
    assert poly.get("data-sink-mod") == payload


def test_a_non_numeric_port_index_does_not_take_the_page_down():
    """`int(p.get("index"))` in `element_info` is unguarded.

    Fails if that bare `int()` comes back: `element_info` raises `ValueError`,
    and because `render_page` calls it for every element the whole diagram
    stops rendering over one malformed port.
    """
    page = _page(port_index="nao-e-um-numero")
    el = page.findall(".//element")[1]

    info = element_info(el)          # must not raise
    assert info["id"] == "2"

    svg = render_page(page)          # nor must the page it is drawn on
    assert "gate-grp" in svg


@pytest.mark.parametrize("mod", ["NOT", "RTRIG", "FTRIG"])
def test_the_real_modifiers_still_reach_the_wire_unchanged(mod):
    """The escaping must not disturb the three values that actually occur.

    Fails if escaping mangles an ordinary modifier -- the client reads
    `data-sink-mod === 'NOT'` exactly, so a changed byte silently stops
    inverting the wire.
    """
    svg = render_page(_page(operator_type=mod))
    root = ET.fromstring(svg)
    poly = root.find(".//{http://www.w3.org/2000/svg}polyline")
    assert poly.get("data-sink-mod") == mod
