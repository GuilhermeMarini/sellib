"""A document that declares a DTD is refused, on every path SELLIB reads XML.

The attack is entity expansion, "billion laughs": a `<!DOCTYPE` block defines
text shortcuts that reference each other, so each level multiplies. The file
stays tiny -- it stores only the definitions -- and the expansion happens in
memory, during the parse, with no half-way for the parser to stop at.

Measured against this project's own stack before the fix: 317 bytes became
1,000,000 characters in 7 ms, a factor of 3155. Two more levels of the same
file is 100 million. That parser sits behind an upload endpoint that accepts
200 MB, and the files come from outside -- a client's integrator, a vendor
export -- so "the file is trusted" was never true.

Refusing the DOCTYPE removes the class rather than mitigating it: entities can
only be declared there. It costs nothing because none of these formats uses a
DTD -- IEC 61850 validates against an XSD referenced by namespace. Measured
over every SCL file reachable from this project: 696 files (345 factory ICDs
plus 351 substation SCD/ICD/CID), 0 with a DOCTYPE.

**The refusal itself is `py61850.scl`'s** -- one copy, for every consumer of
an SCL file -- and its own edge cases are tested there: a DOCTYPE behind half
a megabyte of comment, in memory and streamed from a file, and a malformed
prolog left to the real parser. sellib once carried a second copy, and
siemenslib a third; they had already drifted in the message they raised.

What is asserted HERE is the part py61850 cannot know about: that sellib's
OWN two readers of untrusted XML -- neither of which reads an SCL file -- call
it at all. A GLE arrives inside an RDB somebody uploaded and a DNP profile
comes out of a vendor zip they chose to import, and each is one forgotten call
away from being the hole this closes.
"""

from __future__ import annotations

import pytest
from py61850.scl import DtdNotAllowed

from sellib.dnp_profile import parse as parse_dnp_profile
from sellib.gle import parse_gle


def test_a_gle_declaring_a_dtd_is_refused(tmp_path):
    """A GLE arrives inside an RDB somebody uploaded, so it is no more trusted
    than an SCD."""
    f = tmp_path / "GL1.gle"
    f.write_bytes(b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE editor ['
                  b'<!ENTITY a "AA">]><editor><page name="P"/></editor>')
    with pytest.raises(DtdNotAllowed):
        parse_gle(f)


def test_an_ordinary_gle_still_parses(tmp_path):
    f = tmp_path / "GL1.gle"
    f.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<editor version="1.0"><page name="P"><elements /></page>'
                  b"</editor>")
    assert parse_gle(f).find("page").get("name") == "P"


def test_the_gle_check_reads_the_bytes_the_parse_will_read(tmp_path):
    """A GLE is latin-1 and its declaration is rewritten before parsing. The
    scan runs on the POST-swap bytes, so a DOCTYPE cannot hide in the
    difference between what was checked and what was parsed."""
    f = tmp_path / "GL1.gle"
    f.write_bytes('<?xml version="1.0" encoding="utf-8"?>'
                  '<!DOCTYPE editor [<!ENTITY a "AA">]>'
                  '<editor><page name="Proteção"/></editor>'
                  .encode("latin-1"))
    with pytest.raises(DtdNotAllowed):
        parse_gle(f)


def test_a_dnp_profile_declaring_a_dtd_is_refused():
    """The profile comes out of a vendor zip the user chose to import."""
    with pytest.raises(DtdNotAllowed):
        parse_dnp_profile(b'<?xml version="1.0"?><!DOCTYPE d ['
                          b'<!ENTITY a "AA">]><d/>')


def test_the_dnp_refusal_is_not_swallowed_as_a_parse_error():
    """`_parse_xml` wraps `ET.ParseError` into `DnpProfileError`. The DTD
    refusal must come out as itself: a caller distinguishing "this file is
    malformed" from "this file is hostile" cannot do it if the second is
    reported as the first."""
    with pytest.raises(DtdNotAllowed):
        parse_dnp_profile(b'<?xml version="1.0"?><!DOCTYPE d ['
                          b'<!ENTITY a "AA">]><d>')
